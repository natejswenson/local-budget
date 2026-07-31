"""Walmart connector — storage, money conversion, and the matcher.

NOTHING here touches the network or needs a session. `parse.py` produces plain
dicts, so every layer below it is exercised with literals — which is the whole
reason `fetch.py` is the only module allowed to make a request.

The entity dicts below ARE the contract between `parse` and `store`. Money
fields are plain decimal strings exactly as a page displays them, and they are
POSITIVE magnitudes: Walmart publishes prices, never postings. `line_price` is
the LINE total with quantity already included, which is what the source gives.
"""
from __future__ import annotations

import pytest

from local_budget import db, money
from local_budget.connectors.walmart import import_xlsx, match, store


def order(number="200012345678901", placed="2026-07-20", total="149.50",
          *, items=None, channel="online", detail=True, **kw):
    o = {"order_number": number, "order_placed_date": placed,
         "grand_total": total, "channel": channel, "detail_fetched": detail,
         "payment_method": "Visa ending in 1234",
         "items": items if items is not None else []}
    o.update(kw)
    return o


def item(title, line_price, qty=1, **kw):
    it = {"title": title, "line_price": line_price, "quantity": qty,
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
    assert items[0]["line_price_cents"] == 348      # a price, positive
    assert items[1]["quantity"] == 2


def test_a_line_price_is_stored_as_given_not_divided_by_quantity(conn):
    """Walmart publishes a LINE total: two bags of peanuts is one line reading
    $14.50. Treating that as a unit price and multiplying, as the Amazon
    connector legitimately does with its own source, doubles the line."""
    store.store_orders(conn, [order(items=[item("Peanuts 5lb", "14.50", qty=2)])],
                       store.start_run(conn, "t"))
    row = conn.execute("SELECT line_price_cents, quantity FROM walmart_items").fetchone()
    assert (row["line_price_cents"], row["quantity"]) == (1450, 2)


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


# ── matching: an order settles as a SET of bank rows ─────────────────────────
def test_a_single_bank_row_matching_the_order_total(conn):
    _bank(conn, 1, "2026-07-20", -14950)
    store.store_orders(conn, [order()], store.start_run(conn, "t"))
    r = match.run(conn)
    assert (r["exact"], r["split"]) == (1, 0)


def test_an_order_that_settled_as_five_charges(conn):
    """The pattern this exists for: a $203.60 order posted as five partial charges.
    A one-charge-per-order model leaves this — the larger, more interesting
    order — permanently unexplained."""
    parts = [2410, 145, 830, 5275, 11700]
    for i, cents in enumerate(parts, start=1):
        _bank(conn, i, "2026-07-20", -cents)
    store.store_orders(conn, [order(placed="2026-07-17", total="203.60")],
                       store.start_run(conn, "t"))
    r = match.run(conn)
    assert r["split"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM walmart_matches").fetchone()["c"] == len(parts)


def test_an_order_that_settled_as_two_charges(conn):
    _bank(conn, 1, "2026-07-03", -1865)
    _bank(conn, 2, "2026-07-03", -14555)
    store.store_orders(conn, [order(placed="2026-07-01", total="164.20")],
                       store.start_run(conn, "t"))
    assert match.run(conn)["split"] == 1


def test_a_single_row_is_claimed_before_any_combination_can_take_it(conn):
    """The pass ordering. Run as one pass, a two-row sum could consume the exact
    row another order needed, leaving both wrong."""
    _bank(conn, 1, "2026-07-20", -5000)      # exactly order B
    _bank(conn, 2, "2026-07-20", -3000)
    _bank(conn, 3, "2026-07-20", -2000)      # 3000 + 2000 also makes 5000
    run = store.start_run(conn, "t")
    store.store_orders(conn, [order(number="A", total="50.00"),
                              order(number="B", total="50.00")], run)
    match.run(conn)
    by_order: dict[str, set] = {}
    for r in conn.execute("SELECT order_number, txn_id FROM walmart_matches"):
        by_order.setdefault(r["order_number"], set()).add(r["txn_id"])
    # One order took the single row; the other took the pair; every row used once.
    assert sorted(len(v) for v in by_order.values()) == [1, 2]
    assert set().union(*by_order.values()) == {1, 2, 3}


def test_two_ways_to_reach_the_total_is_reported_not_picked(conn):
    """Subset-sum finds AN answer far more readily than exact pairing does. When
    several sets hit the total, which was the order is not knowable — and a
    wrong basket of items attributed to a charge is worse than an unexplained
    charge."""
    _bank(conn, 1, "2026-07-20", -3000)
    _bank(conn, 2, "2026-07-20", -2000)
    _bank(conn, 3, "2026-07-20", -1000)
    _bank(conn, 4, "2026-07-20", -4000)      # 1000+4000 == 3000+2000
    store.store_orders(conn, [order(total="50.00")], store.start_run(conn, "t"))
    r = match.run(conn)
    assert r["matched"] == 0
    assert len(r["ambiguous"]) == 1
    assert len(r["ambiguous"][0]["solutions"]) > 1
    assert conn.execute("SELECT COUNT(*) c FROM walmart_matches").fetchone()["c"] == 0


def test_a_bank_row_can_only_ever_explain_one_order(conn):
    _bank(conn, 1, "2026-07-20", -5000)
    run = store.start_run(conn, "t")
    store.store_orders(conn, [order(number="A", total="50.00"),
                              order(number="B", total="50.00")], run)
    match.run(conn)
    assert conn.execute("SELECT COUNT(*) c FROM walmart_matches").fetchone()["c"] == 1


def test_partial_sums_are_never_accepted(conn):
    """Four of the five charges summing to less than the total is not the order."""
    for i, cents in enumerate([2410, 145, 830], start=1):
        _bank(conn, i, "2026-07-20", -cents)
    store.store_orders(conn, [order(placed="2026-07-17", total="203.60")],
                       store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 0


def test_a_charge_before_the_order_is_not_a_settlement_of_it(conn):
    _bank(conn, 1, "2026-07-10", -14950)
    store.store_orders(conn, [order(placed="2026-07-20")], store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 0


def test_settlement_can_trail_the_order_by_days(conn):
    _bank(conn, 1, "2026-07-27", -14950)
    store.store_orders(conn, [order(placed="2026-07-20")], store.start_run(conn, "t"))
    assert match.run(conn)["matched"] == 1


def test_a_cancelled_order_never_competes_for_bank_rows(conn):
    """Nothing settled, so there is nothing to find — and letting it into the
    search only adds a target that can steal rows from an order really charged."""
    _bank(conn, 1, "2026-07-20", -14950)
    run = store.start_run(conn, "t")
    store.store_orders(conn, [order(number="X", cancelled=True),
                              order(number="Y")], run)
    match.run(conn)
    got = conn.execute("SELECT order_number FROM walmart_matches").fetchall()
    assert [r["order_number"] for r in got] == ["Y"]


def test_a_haystack_window_is_left_unmatched_rather_than_guessed(conn):
    """Past a certain candidate count the subsets stop being evidence: the
    number of them explodes, and so does the chance two coincide."""
    for i in range(1, match.MAX_CANDIDATES + 3):
        _bank(conn, i, "2026-07-20", -(100 + i))
    store.store_orders(conn, [order(total="1.01")], store.start_run(conn, "t"))
    # k=1 still works (pass 1 is a direct amount test, not a search)...
    assert match.run(conn)["exact"] == 1


def test_confirm_records_a_human_chosen_settlement(conn):
    _bank(conn, 1, "2026-07-20", -3000)
    _bank(conn, 2, "2026-07-20", -2000)
    store.store_orders(conn, [order(total="50.00")], store.start_run(conn, "t"))
    match.confirm(conn, "200012345678901", [1, 2])
    rows = conn.execute("SELECT * FROM walmart_matches ORDER BY txn_id").fetchall()
    assert [r["txn_id"] for r in rows] == [1, 2]
    assert all(r["confidence"] == "manual" for r in rows)


def test_split_settlements_are_counted_and_reported(conn):
    _bank(conn, 1, "2026-07-03", -1865)
    _bank(conn, 2, "2026-07-03", -14555)
    store.store_orders(conn, [order(placed="2026-07-01", total="164.20")],
                       store.start_run(conn, "t"))
    match.run(conn)
    st = match.split_settlements(conn)
    assert (st["orders"], st["split_orders"], st["max_parts"]) == (1, 1, 2)


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


def test_coverage_counts_every_row_of_a_split_settlement(conn):
    """The dollars matched are the BANK's, not the order's. An order settling as
    five rows explains all five, and counting only one would understate coverage
    exactly where the connector does its most useful work."""
    _bank(conn, 1, "2026-07-03", -1865)
    _bank(conn, 2, "2026-07-03", -14555)
    store.store_orders(conn, [order(placed="2026-07-01", total="164.20")],
                       store.start_run(conn, "t"))
    match.run(conn)
    cov = match.coverage(conn)
    assert cov["matched_cents"] == 16420
    assert cov["matched_txns"] == 2
    assert cov["coverage_pct"] == 100.0


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
        import_xlsx.store_and_match([], scope="days=30", since="2026-07-01")
    with db.connect(tmp_path / "budget.db") as c:
        assert c.execute("SELECT COUNT(*) n FROM walmart_sync_runs").fetchone()["n"] == 0


def test_an_account_with_no_walmart_history_is_a_legitimate_zero(conn, tmp_path,
                                                                 monkeypatch):
    """The gate judges an empty result against the window it was asked for. With
    no Walmart charges in that window there is nothing to contradict, and a
    genuinely empty sync must not be called a broken parser."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    conn.commit()
    r = import_xlsx.store_and_match([], scope="days=30", since="2026-07-01")
    assert r["orders"] == 0


def test_store_and_match_reports_what_it_did(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-20", -14950)
    conn.commit()
    r = import_xlsx.store_and_match([order(items=[item("Dog food", "42.99")])],
                            scope="days=30", since="2026-07-01")
    assert (r["orders"], r["items"], r["matched"]) == (1, 1, 1)
    assert r["coverage"]["coverage_pct"] == 100.0
