"""Walmart connector — storage, money conversion, and the matcher.

NOTHING here touches the network or needs a session. `parse.py` produces plain
dicts, so every layer below it is exercised with literals — which is the whole
reason `fetch.py` is the only module allowed to make a request.

The entity dicts below ARE the contract between `parse` and `store`. Money
fields are plain decimal strings exactly as a page displays them; order and item
amounts are POSITIVE magnitudes; charge amounts are LEDGER-SIGNED. The same
convention split as the Amazon connector, and for the same reason: prices are
not postings.
"""
from __future__ import annotations

import pytest

from local_budget import db, money
from local_budget.connectors.walmart import match, store, sync


def order(number="200012345678901", placed="2026-07-20", total="149.50",
          *, items=None, charges=None, channel="online", detail=True, **kw):
    o = {"order_number": number, "order_placed_date": placed,
         "grand_total": total, "channel": channel, "detail_fetched": detail,
         "payment_method": "Visa ending in 1234",
         "items": items if items is not None else [], "charges": charges or []}
    o.update(kw)
    return o


def item(title, unit_price, qty=1, **kw):
    it = {"title": title, "unit_price": unit_price, "quantity": qty,
          "product_id": "123456789", "seller": "Walmart.com"}
    it.update(kw)
    return it


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1, 'Test', 'csv')")
        yield c


def _bank(c, txn_id, dt, cents, merchant="WALMART.COM"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, imported_at) "
        "VALUES (?,1,?,?,?, 'posted', ?, '2026-07-01')",
        (txn_id, f"fit{txn_id}", dt, cents, merchant))


# ── money conversion ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,cents", [
    ("19.99", 1999), ("0.10", 10), ("0.07", 7), ("$1,234.56", 123456),
    ("-$50.00", -5000), ("0", 0), (19.99, 1999), (None, None), ("", None),
])
def test_to_cents_handles_what_a_page_displays(raw, cents):
    assert store.to_cents(raw) == cents


def test_to_cents_raises_on_sub_cent_precision_rather_than_rounding(conn):
    """money.py's doctrine, inherited rather than re-litigated: a value we
    cannot represent exactly is malformed, not something to round quietly."""
    with pytest.raises(money.AmountParseError):
        store.to_cents("19.999")


def test_to_cents_lenient_mode_returns_none_instead_of_raising():
    """Item prices are descriptive — scaled by splits.allocate() before anything
    is attributed — so one unreadable price must not fail an entire backfill."""
    assert store.to_cents("about $20", strict=False) is None


# ── storage ──────────────────────────────────────────────────────────────────
def test_store_orders_writes_items_and_is_idempotent(conn):
    o = order(items=[item("Great Value milk", "3.48"),
                     item("HDMI cable", "11.49", qty=2)], item_count=2)
    run = store.start_run(conn, "days=30")
    assert store.store_orders(conn, [o], run)["orders"] == 1
    assert store.store_orders(conn, [o], run)["orders"] == 1          # re-sync
    assert conn.execute("SELECT COUNT(*) c FROM walmart_orders").fetchone()["c"] == 1
    items = conn.execute("SELECT * FROM walmart_items ORDER BY line_no").fetchall()
    assert len(items) == 2, "re-sync must replace items, not duplicate them"
    assert items[0]["unit_price_cents"] == 348      # a price, positive
    assert items[1]["quantity"] == 2


def test_store_orders_skips_a_row_with_no_order_number(conn):
    """An unparseable row is not a fact. Storing it under a NULL key would
    create a phantom order that later re-syncs keep colliding with."""
    run = store.start_run(conn, "days=30")
    assert store.store_orders(conn, [order(number="")], run)["orders"] == 0


def test_a_list_only_pass_never_deletes_items_a_detail_fetch_stored(conn):
    """The regression this exists for: a rolling sync reads the LIST page, which
    carries no items, and writing that through would wipe the lines backfill
    spent a request each to collect."""
    run = store.start_run(conn, "backfill")
    store.store_orders(conn, [order(items=[item("Dog food", "42.99")])], run)
    store.store_orders(conn, [order(items=[], detail=False)], run)   # list-only
    rows = conn.execute("SELECT title FROM walmart_items").fetchall()
    assert [r["title"] for r in rows] == ["Dog food"]


def test_detail_fetched_is_raised_but_never_lowered(conn):
    run = store.start_run(conn, "t")
    store.store_orders(conn, [order(detail=True)], run)
    store.store_orders(conn, [order(detail=False)], run)
    assert conn.execute(
        "SELECT detail_fetched d FROM walmart_orders").fetchone()["d"] == 1


# ── charges: the reconciliation key Walmart does not publish ─────────────────
def test_a_charge_is_synthesized_from_the_order_total_and_flagged_derived(conn):
    """Walmart publishes orders, not charges. Without this the order is
    invisible to the matcher entirely."""
    store.store_orders(conn, [order(total="149.50")], store.start_run(conn, "t"))
    c = conn.execute("SELECT * FROM walmart_charges").fetchone()
    assert c["amount_cents"] == -14950, "a charge is ledger-signed: outflow negative"
    assert c["derived"] == 1
    assert c["charged_date"] == "2026-07-20"


def test_an_observed_charge_supersedes_the_derived_one(conn):
    """Otherwise the same order is counted twice — the derived row collides with
    none of the real ones on the natural key, so it simply survives alongside."""
    run = store.start_run(conn, "t")
    store.store_orders(conn, [order()], run)
    store.store_orders(conn, [order(charges=[
        {"charged_date": "2026-07-21", "amount": "-100.00"},
        {"charged_date": "2026-07-23", "amount": "-49.50"},
    ])], run)
    rows = conn.execute(
        "SELECT amount_cents, derived FROM walmart_charges "
        "ORDER BY charged_date").fetchall()
    assert [r["amount_cents"] for r in rows] == [-10000, -4950]
    assert all(r["derived"] == 0 for r in rows)


def test_superseding_a_derived_charge_drops_its_match(conn):
    """The match was made against an inferred date. Leaving it behind would
    orphan a row pointing at a charge that no longer exists."""
    _bank(conn, 1, "2026-07-20", -14950)
    run = store.start_run(conn, "t")
    store.store_orders(conn, [order()], run)
    match.run(conn)
    assert conn.execute("SELECT COUNT(*) c FROM walmart_matches").fetchone()["c"] == 1
    store.store_orders(conn, [order(charges=[
        {"charged_date": "2026-07-20", "amount": "-149.50"}])], run)
    assert conn.execute(
        "SELECT COUNT(*) c FROM walmart_matches WHERE walmart_charge_id NOT IN "
        "(SELECT walmart_charge_id FROM walmart_charges)").fetchone()["c"] == 0


def test_a_cancelled_order_gets_no_synthesized_charge(conn):
    store.store_orders(conn, [order(cancelled=True)], store.start_run(conn, "t"))
    assert conn.execute("SELECT COUNT(*) c FROM walmart_charges").fetchone()["c"] == 0


def test_a_refund_is_never_synthesized(conn):
    """An order's refund total says a refund happened, not when it settled. A
    charge invented on the wrong date matches the wrong bank row or sits
    unmatched forever looking like a parser bug."""
    store.store_orders(conn, [order(refund_total="20.00")],
                       store.start_run(conn, "t"))
    assert conn.execute(
        "SELECT COUNT(*) c FROM walmart_charges WHERE is_refund=1").fetchone()["c"] == 0


def test_an_observed_refund_is_stored_positive(conn):
    store.store_orders(conn, [order(charges=[
        {"charged_date": "2026-07-25", "amount": "20.00", "is_refund": True}])],
        store.start_run(conn, "t"))
    c = conn.execute("SELECT * FROM walmart_charges WHERE is_refund=1").fetchone()
    assert c["amount_cents"] == 2000


# ── matching ─────────────────────────────────────────────────────────────────
def test_exact_same_day_match(conn):
    _bank(conn, 1, "2026-07-20", -14950)
    store.store_orders(conn, [order()], store.start_run(conn, "t"))
    r = match.run(conn)
    assert (r["exact"], r["windowed"]) == (1, 0)


def test_windowed_match_within_three_days(conn):
    _bank(conn, 1, "2026-07-22", -14950)
    store.store_orders(conn, [order()], store.start_run(conn, "t"))
    assert match.run(conn)["windowed"] == 1


def test_a_charge_outside_the_window_is_left_alone(conn):
    _bank(conn, 1, "2026-07-30", -14950)
    store.store_orders(conn, [order()], store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 0


def test_same_day_claims_its_row_before_a_windowed_match_can_take_it(conn):
    """The ordering invariant. One combined pass lets the ±3-day charge steal
    the row the same-day charge needed, leaving BOTH wrong."""
    _bank(conn, 1, "2026-07-20", -5000)
    _bank(conn, 2, "2026-07-23", -5000)
    run = store.start_run(conn, "t")
    store.store_orders(conn, [
        order(number="A", placed="2026-07-23", total="50.00"),
        order(number="B", placed="2026-07-20", total="50.00"),
    ], run)
    match.run(conn)
    pairs = {r["order_number"]: r["txn_id"] for r in conn.execute(
        "SELECT c.order_number, m.txn_id FROM walmart_matches m "
        "JOIN walmart_charges c USING (walmart_charge_id)")}
    assert pairs == {"A": 2, "B": 1}


def test_ambiguity_is_reported_not_guessed(conn):
    """Two identical Walmart charges days apart is routine. A wrong match
    attributes the wrong basket of items and quietly misleads everything after."""
    _bank(conn, 1, "2026-07-21", -5000)
    _bank(conn, 2, "2026-07-22", -5000)
    store.store_orders(conn, [order(number="A", total="50.00")],
                       store.start_run(conn, "t"))
    r = match.run(conn)
    assert r["matched"] == 0
    assert len(r["ambiguous"]) == 1
    assert {c["txn_id"] for c in r["ambiguous"][0]["candidates"]} == {1, 2}


def test_confirm_records_a_human_chosen_match(conn):
    _bank(conn, 1, "2026-07-21", -5000)
    _bank(conn, 2, "2026-07-22", -5000)
    store.store_orders(conn, [order(total="50.00")], store.start_run(conn, "t"))
    cid = conn.execute("SELECT walmart_charge_id i FROM walmart_charges").fetchone()["i"]
    match.confirm(conn, cid, 2)
    row = conn.execute("SELECT * FROM walmart_matches").fetchone()
    assert (row["txn_id"], row["confidence"]) == (2, "manual")


# ── channel: the rule the Amazon matcher has no need for ─────────────────────
def test_an_online_order_never_matches_an_in_store_charge(conn):
    """Same amount, same day, different place. Amount+date alone would attach a
    grocery pickup's item list to a Supercenter run and look right doing it."""
    _bank(conn, 1, "2026-07-20", -14950, merchant="WM SUPERCENTER FARGO")
    store.store_orders(conn, [order(channel="online")], store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 0


def test_an_in_store_order_matches_an_in_store_charge(conn):
    _bank(conn, 1, "2026-07-20", -14950, merchant="WAL MART SUPER")
    store.store_orders(conn, [order(channel="in-store")], store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 1


def test_an_unknown_channel_falls_back_to_every_walmart_pattern(conn):
    """Refusing to match what we cannot classify would drop real
    reconciliations; this is still amount-exact and date-bounded."""
    _bank(conn, 1, "2026-07-20", -14950, merchant="WM SUPERCENTER FARGO")
    store.store_orders(conn, [order(channel=None)], store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 1


@pytest.mark.parametrize("merchant", [
    "WALMART.COM", "WALMART.C 702 SW", "WAL MART SUPER", "WM SUPERC WAL",
    "WM SUPERCENTER DETROIT", "WAL MART FARGO",
])
def test_every_real_walmart_merchant_string_is_covered(conn, merchant):
    """These are the actual merchant_norm values in the ledger. A pattern that
    silently stops covering one of them shows up as a coverage drop nobody can
    explain."""
    _bank(conn, 1, "2026-07-20", -1000, merchant=merchant)
    assert match.coverage(conn)["total_cents"] == 1000


def test_sams_club_is_not_counted_as_walmart(conn):
    """It is a separate site with a separate login, so walmart.com order history
    structurally cannot explain it. Counting it would report ~9% of "Walmart"
    spend as unexplained forever."""
    _bank(conn, 1, "2026-07-20", -5000, merchant="SAMS CLUB SAM'S")
    assert match.coverage(conn)["total_cents"] == 0


# ── coverage, derivation, horizon ────────────────────────────────────────────
def test_coverage_is_measured_in_dollars_and_split_by_channel(conn):
    """Online and in-store are different stories with different fixes; one
    number averaging both obscures which is which."""
    _bank(conn, 1, "2026-07-20", -10000)                              # matched
    _bank(conn, 2, "2026-07-20", -30000, merchant="WAL MART FARGO")   # unmatched
    store.store_orders(conn, [order(total="100.00")], store.start_run(conn, "t"))
    match.run(conn)
    cov = match.coverage(conn)
    assert cov["total_cents"] == 40000
    assert cov["matched_cents"] == 10000
    assert cov["coverage_pct"] == 25.0
    assert cov["channels"]["online"]["coverage_pct"] == 100.0
    assert cov["channels"]["in-store"]["coverage_pct"] == 0.0


def test_derived_share_reports_how_much_rests_on_an_inference(conn):
    """Quoting coverage without this presents an inference with the same
    confidence as an observation."""
    _bank(conn, 1, "2026-07-20", -10000)
    _bank(conn, 2, "2026-07-21", -5000)
    run = store.start_run(conn, "t")
    store.store_orders(conn, [
        order(number="A", total="100.00"),                       # derived
        order(number="B", placed="2026-07-21", total="50.00",
              charges=[{"charged_date": "2026-07-21", "amount": "-50.00"}]),
    ], run)
    match.run(conn)
    d = match.coverage(conn)["derived"]
    assert (d["matched"], d["derived"]) == (2, 1)
    assert d["derived_pct"] == 50.0
    assert d["derived_cents"] == 10000


def test_horizon_names_the_charges_that_predate_any_record(conn):
    """Without it a low percentage reads as bad data when it is a window
    problem."""
    _bank(conn, 1, "2024-03-01", -8000)
    _bank(conn, 2, "2026-07-20", -14950)
    store.store_orders(conn, [order()], store.start_run(conn, "t"))
    hz = match.horizon(conn)
    assert hz["earliest"] == "2026-07-20"
    assert (hz["pre_count"], hz["pre_cents"]) == (1, 8000)


def test_breakdown_counts_a_split_shipment_item_once(conn):
    """One order, two charges. Joining items through each counts the same
    product twice — a silent double-count that inflates every total downstream."""
    _bank(conn, 1, "2026-07-20", -10000)
    _bank(conn, 2, "2026-07-22", -4950)
    store.store_orders(conn, [order(items=[item("Patio umbrella", "149.50")],
                                    charges=[
        {"charged_date": "2026-07-20", "amount": "-100.00"},
        {"charged_date": "2026-07-22", "amount": "-49.50"}])],
        store.start_run(conn, "t"))
    match.run(conn)
    rows = match.breakdown(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Patio umbrella"


# ── the anti-vacuity gate ────────────────────────────────────────────────────
def test_zero_orders_aborts_when_the_ledger_says_there_were_charges(conn):
    """A broken parser must never look like a quiet month — and there is no
    upstream library here whose test suite would notice first."""
    _bank(conn, 1, "2026-07-20", -14950)
    with pytest.raises(store.SyncAborted):
        store.assert_not_vacuous(conn, orders=0, scope_has_known_charges=True)


def test_zero_orders_is_fine_when_the_ledger_agrees(conn):
    store.assert_not_vacuous(conn, orders=0, scope_has_known_charges=False)


def test_store_and_match_writes_nothing_when_it_aborts(conn, tmp_path, monkeypatch):
    """Not just "raises" — the point is that no run row, no order and no
    half-written state survive it."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-20", -14950)
    conn.commit()
    with pytest.raises(store.SyncAborted):
        sync.store_and_match([], scope="days=30", since="2026-07-01")
    with db.connect(tmp_path / "budget.db") as c:
        assert c.execute("SELECT COUNT(*) n FROM walmart_sync_runs").fetchone()["n"] == 0


def test_an_account_with_no_walmart_history_is_a_legitimate_zero(conn, tmp_path,
                                                                 monkeypatch):
    """The gate judges an empty result against the window it was asked for. With
    no Walmart charges in that window there is nothing to contradict, and a
    genuinely empty sync must not be called a broken parser."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    conn.commit()
    r = sync.store_and_match([], scope="days=30", since="2026-07-01")
    assert r["orders"] == 0


def test_store_and_match_reports_what_it_did(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-20", -14950)
    conn.commit()
    r = sync.store_and_match([order(items=[item("Dog food", "42.99")])],
                            scope="days=30", since="2026-07-01")
    assert (r["orders"], r["charges"], r["matched"]) == (1, 1, 1)
    assert r["coverage"]["coverage_pct"] == 100.0
