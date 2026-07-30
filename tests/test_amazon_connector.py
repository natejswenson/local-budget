"""Amazon connector — storage, money conversion, and the matcher.

NOTHING here touches the network or needs credentials. Fakes stand in for the
library's entity objects, which is the whole reason `fetch.py` is the only
module allowed to make a request.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest

from local_budget import db
from local_budget.connectors.amazon import match, store


# ── fakes matching the amazon-orders entity surface ──────────────────────────
# Conventions here are the REAL ones, verified against live parser output in
# test_amazon_contract.py — Order money is a POSITIVE magnitude, Transaction
# money is ledger-signed (negative = charge), and Item.seller is an OBJECT.
# An earlier version of these fakes had all three wrong and every test passed.
@dataclass
class FakeSeller:
    """`Item.seller` is a Seller entity, not a string. sqlite3 refuses it."""
    name: str


@dataclass
class FakeItem:
    title: str
    price: float                       # POSITIVE — what the item cost
    asin: str = "B000TEST"
    quantity: int | None = None        # real parser returns None on 1-qty lines
    seller: FakeSeller = field(default_factory=lambda: FakeSeller("Amazon.com"))
    condition: str | None = "New"


@dataclass
class FakeOrder:
    order_number: str
    order_placed_date: date
    grand_total: float                 # POSITIVE — not ledger-signed
    items: list = field(default_factory=list)
    subtotal: float | None = None
    estimated_tax: float | None = None
    shipping_total: float | None = None
    refund_total: float | None = None
    payment_method: str | None = "Visa ****1234"
    item_count: int | None = None
    cancelled: bool = False


@dataclass
class FakeTxn:
    completed_date: date
    grand_total: float          # NEGATIVE for a charge (library convention)
    order_number: str | None = None
    payment_method: str = "Visa ****1234"
    seller: str = "Amazon.com"

    @property
    def is_refund(self) -> bool:
        return self.grand_total > 0


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1, 'Test', 'csv')")
        yield c


def _bank(c, txn_id, dt, cents, merchant="AMAZON MKTPL AMZN.COM"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, imported_at) "
        "VALUES (?,1,?,?,?, 'posted', ?, '2026-07-01')",
        (txn_id, f"fit{txn_id}", dt, cents, merchant))


# ── money conversion ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,cents", [
    (19.99, 1999), (0.1, 10), (0.07, 7), (123.45, 12345),
    ("$1,234.56", 123456), ("-50.00", -5000), (0, 0),
    (None, None), ("", None),
])
def test_to_cents_never_loses_a_penny_to_float(raw, cents):
    """`int(19.99 * 100)` is 1998 in binary floating point. Every one of these
    is a value that a naive float path gets wrong or nearly wrong."""
    assert store.to_cents(raw) == cents


def test_to_cents_rounds_half_up_not_half_to_even():
    assert store.to_cents("0.005") == 1        # banker's rounding would give 0
    assert store.to_cents("0.015") == 2        # ...and 2 here, coincidentally


# ── storage ──────────────────────────────────────────────────────────────────
def test_store_orders_writes_items_and_is_idempotent(conn):
    o = FakeOrder("111-A", date(2026, 7, 20), 149.50, item_count=2, items=[
        FakeItem("Dog food 24lb", 42.99),
        FakeItem("HDMI cable", 11.49, quantity=2),
    ])
    run = store.start_run(conn, "days=30")
    assert store.store_orders(conn, [o], run) == 1
    assert store.store_orders(conn, [o], run) == 1        # re-sync
    assert conn.execute("SELECT COUNT(*) c FROM amazon_orders").fetchone()["c"] == 1
    items = conn.execute("SELECT * FROM amazon_items ORDER BY line_no").fetchall()
    assert len(items) == 2, "re-sync must replace items, not duplicate them"
    assert items[0]["title"] == "Dog food 24lb"
    assert items[0]["unit_price_cents"] == 4299           # price, not a posting
    assert items[0]["quantity"] == 1                      # None normalised
    assert items[1]["quantity"] == 2


def test_seller_object_is_flattened_to_its_name(conn):
    """sqlite3 rejects the Seller entity outright; `str()` on it would store
    "Seller: Amazon.com". Only `.name` is the data."""
    o = FakeOrder("111-S", date(2026, 7, 20), 10.0,
                  items=[FakeItem("Thing", 10.0, seller=FakeSeller("Acme LLC"))])
    store.store_orders(conn, [o], store.start_run(conn, "t"))
    assert conn.execute("SELECT seller FROM amazon_items").fetchone()["seller"] == "Acme LLC"


def test_store_orders_skips_a_row_with_no_order_number(conn):
    """An unparseable row is not a fact. Storing it under a NULL key would
    create a phantom order that later re-syncs keep colliding with."""
    run = store.start_run(conn, "days=30")
    bad = FakeOrder("", date(2026, 7, 1), -10.0)
    assert store.store_orders(conn, [bad], run) == 0


def test_store_transactions_dedupes_on_identity_not_row_order(conn):
    run = store.start_run(conn, "days=30")
    t = FakeTxn(date(2026, 7, 20), -149.50, "111-A")
    store.store_transactions(conn, [t], run)
    store.store_transactions(conn, [t], run)              # overlapping window
    assert conn.execute("SELECT COUNT(*) c FROM amazon_transactions").fetchone()["c"] == 1


def test_refund_sign_survives_storage(conn):
    run = store.start_run(conn, "days=30")
    store.store_transactions(conn, [FakeTxn(date(2026, 7, 9), 25.00, "111-R")], run)
    row = conn.execute("SELECT * FROM amazon_transactions").fetchone()
    assert row["grand_total_cents"] == 2500 and row["is_refund"] == 1


# ── the anti-vacuity gate ────────────────────────────────────────────────────
def test_empty_sync_aborts_when_the_ledger_knows_better(conn):
    """The failure this exists to catch: Amazon redesigns a page, the parser
    yields nothing, and the sync cheerfully reports success forever."""
    with pytest.raises(store.SyncAborted, match="parser is almost certainly broken"):
        store.assert_not_vacuous(conn, orders=0, txns=0, scope_has_known_charges=True)


def test_empty_sync_is_fine_when_there_genuinely_are_no_charges(conn):
    store.assert_not_vacuous(conn, orders=0, txns=0, scope_has_known_charges=False)


# ── the matcher ──────────────────────────────────────────────────────────────
def _az(conn, dt, cents, order="111-A"):
    store.store_transactions(conn, [FakeTxn(dt, cents / 100, order)],
                             store.start_run(conn, "t"))


def test_exact_match_same_day_same_amount(conn):
    _bank(conn, 900, "2026-07-20", -14950)
    _az(conn, date(2026, 7, 20), -14950)
    r = match.run(conn)
    assert r["exact"] == 1 and r["windowed"] == 0 and not r["ambiguous"]


def test_windowed_match_when_settlement_lags(conn):
    _bank(conn, 901, "2026-07-22", -14950)
    _az(conn, date(2026, 7, 20), -14950)
    r = match.run(conn)
    assert r["windowed"] == 1


def test_beyond_the_window_is_not_matched(conn):
    _bank(conn, 902, "2026-07-28", -14950)
    _az(conn, date(2026, 7, 20), -14950)
    assert match.run(conn)["matched"] == 0


def test_duplicate_amounts_are_left_for_a_human(conn):
    """Two identical Amazon charges days apart is routine. Guessing would
    attribute the wrong basket of items to a charge — worse than no answer."""
    _bank(conn, 903, "2026-07-19", -999)
    _bank(conn, 904, "2026-07-21", -999)
    _az(conn, date(2026, 7, 20), -999)
    r = match.run(conn)
    assert r["matched"] == 0
    assert len(r["ambiguous"]) == 1
    assert {c["txn_id"] for c in r["ambiguous"][0]["candidates"]} == {903, 904}


def test_same_day_exact_wins_the_row_a_windowed_match_would_have_stolen(conn):
    """Ordering regression. With one combined pass, the 07-19 Amazon charge can
    claim the 07-20 bank row, leaving the 07-20 charge orphaned AND the 07-19
    one mismatched. Both wrong, from a single pass."""
    _bank(conn, 905, "2026-07-20", -5000)
    _az(conn, date(2026, 7, 20), -5000, order="SAME-DAY")
    _az(conn, date(2026, 7, 19), -5000, order="DAY-BEFORE")
    r = match.run(conn)
    assert r["exact"] == 1
    got = conn.execute(
        "SELECT a.order_number FROM amazon_matches m "
        "JOIN amazon_transactions a USING(amazon_txn_id)").fetchone()
    assert got["order_number"] == "SAME-DAY"


def test_a_bank_row_is_never_matched_twice(conn):
    _bank(conn, 906, "2026-07-20", -1000)
    _az(conn, date(2026, 7, 20), -1000, order="A")
    match.run(conn)
    _az(conn, date(2026, 7, 21), -1000, order="B")
    match.run(conn)
    assert conn.execute("SELECT COUNT(*) c FROM amazon_matches").fetchone()["c"] == 1


def test_non_amazon_merchants_are_never_candidates(conn):
    _bank(conn, 907, "2026-07-20", -14950, merchant="WAL MART DETROIT")
    _az(conn, date(2026, 7, 20), -14950)
    assert match.run(conn)["matched"] == 0


def test_split_shipments_match_as_separate_charges(conn):
    """One order, two boxes, two charges — the case order totals cannot handle
    and the reason matching goes through Amazon's transaction list."""
    _bank(conn, 908, "2026-07-20", -4299)
    _bank(conn, 909, "2026-07-23", -11500)
    _az(conn, date(2026, 7, 20), -4299, order="111-SPLIT")
    _az(conn, date(2026, 7, 23), -11500, order="111-SPLIT")
    assert match.run(conn)["matched"] == 2


# ── coverage + breakdown ─────────────────────────────────────────────────────
def test_coverage_is_measured_in_dollars_not_row_count(conn):
    """Nine small matches and one big miss is not 90% of anything useful.

    Amounts are distinct on purpose — nine IDENTICAL charges would all be
    ambiguous and match zero, which is a different (also correct) behaviour
    covered by test_duplicate_amounts_are_left_for_a_human.
    """
    for i in range(9):
        cents = -(600 + i * 10)                     # 6.00 .. 6.80, sum 57.60
        _bank(conn, 910 + i, "2026-07-05", cents)
        _az(conn, date(2026, 7, 5), cents, order=f"S{i}")
    _bank(conn, 950, "2026-07-06", -40000)          # the big one, unmatched
    match.run(conn)
    cov = match.coverage(conn, "2026-07")
    assert cov["matched_txns"] == 9 and cov["total_txns"] == 10
    assert cov["matched_cents"] == 5760 and cov["total_cents"] == 45760
    assert cov["coverage_pct"] == 12.6              # by dollars, not the 90% by count


def test_breakdown_joins_bank_charge_to_items(conn):
    run = store.start_run(conn, "t")
    store.store_orders(conn, [FakeOrder("111-A", date(2026, 7, 20), -149.50, items=[
        FakeItem("Dog food 24lb", -42.99), FakeItem("HDMI cable", -11.49)])], run)
    _bank(conn, 960, "2026-07-20", -14950)
    _az(conn, date(2026, 7, 20), -14950, order="111-A")
    match.run(conn)
    rows = match.breakdown(conn, "2026-07")
    assert [r["title"] for r in rows] == ["HDMI cable", "Dog food 24lb"]
    assert all(r["posted_date"] == "2026-07-20" for r in rows)


def test_breakdown_is_empty_not_wrong_when_nothing_matched(conn):
    _bank(conn, 970, "2026-07-20", -14950)
    assert match.breakdown(conn, "2026-07") == []


# ── captured-session auth (the passkey path) ─────────────────────────────────
def _isolate_amazon_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    for k in ("AMAZON_USERNAME", "AMAZON_PASSWORD", "AMAZON_OTP_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_a_captured_session_makes_credentials_unnecessary(tmp_path, monkeypatch):
    """The whole point of the passkey path: no password exists to store, so a
    valid cookie jar has to be sufficient on its own."""
    from local_budget.connectors.amazon import session as az
    _isolate_amazon_dir(tmp_path, monkeypatch)
    az.cookie_path().write_text('{"x-main": "abc123", "session-id": "1"}')
    assert az.stored_session_looks_valid() is True
    assert az.credentials(required=False) == (None, None, None)


def test_no_session_and_no_password_says_what_to_do(tmp_path, monkeypatch):
    from local_budget.connectors.amazon import session as az
    _isolate_amazon_dir(tmp_path, monkeypatch)
    assert az.stored_session_looks_valid() is False
    with pytest.raises(az.AmazonAuthError, match="budget amazon login"):
        az.credentials(required=True)


@pytest.mark.parametrize("jar,valid", [
    ('{"x-main": "abc"}', True),
    ('{"session-id": "1"}', False),      # logged out / partial
    ('not json', False),
    ('{}', False),
])
def test_only_the_x_main_cookie_counts_as_authenticated(tmp_path, monkeypatch, jar, valid):
    """`x-main` is the single cookie amazon-orders treats as proof. A jar
    without it would sail past a naive exists() check and then fail mid-sync."""
    from local_budget.connectors.amazon import session as az
    _isolate_amazon_dir(tmp_path, monkeypatch)
    az.cookie_path().write_text(jar)
    assert az.stored_session_looks_valid() is valid


def test_cookie_jar_keeps_only_amazon_cookies_and_is_0600(tmp_path, monkeypatch):
    """The jar is a flat name->value map with no domain scoping, so a
    third-party cookie of the same name would silently shadow a real one."""
    from local_budget.connectors.amazon import browser_login
    _isolate_amazon_dir(tmp_path, monkeypatch)
    n = browser_login._write_jar([
        {"name": "x-main", "value": "keep", "domain": ".amazon.com"},
        {"name": "at-main", "value": "keep2", "domain": "www.amazon.com"},
        {"name": "x-main", "value": "EVIL", "domain": ".tracker.example"},
    ])
    import json as _json
    jar = _json.loads(browser_login.cookie_path().read_text())
    assert n == 2 and jar["x-main"] == "keep" and "at-main" in jar
    assert oct(browser_login.cookie_path().stat().st_mode)[-3:] == "600"


def test_capturing_a_session_without_x_main_is_refused(tmp_path, monkeypatch):
    from local_budget.connectors.amazon import browser_login
    from local_budget.connectors.amazon.session import AmazonAuthError
    _isolate_amazon_dir(tmp_path, monkeypatch)
    with pytest.raises(AmazonAuthError, match="not authenticated"):
        browser_login._write_jar([
            {"name": "session-id", "value": "1", "domain": ".amazon.com"}])


def test_coverage_returns_positive_magnitudes(conn):
    """Spend is stored as negative cents but reported as a positive magnitude.
    Getting this backwards renders spend as a negative, which reads as a
    refund — it shipped that way in the agent tool once."""
    _bank(conn, 980, "2026-07-20", -14950)
    cov = match.coverage(conn, "2026-07")
    assert cov["total_cents"] == 14950
    assert cov["matched_cents"] == 0
