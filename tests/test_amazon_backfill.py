"""Multi-year backfill — resumability, per-year gating, and the horizon.

Every test mocks at the `fetch` boundary; nothing here touches the network or
needs credentials. The cases that matter are the ones where a long job goes
wrong halfway, because that is the normal outcome for a several-hundred-request
scrape, not the exceptional one.
"""
from __future__ import annotations

from datetime import date

import pytest

from local_budget import db
from local_budget.connectors.amazon import backfill, match, store
from local_budget.connectors.amazon.session import AmazonAuthError


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


def _charge(c, txn_id, dt, cents=-1000, merchant="AMAZON MKTPL AMZN.COM"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, imported_at) "
        "VALUES (?,1,?,?,?, 'posted', ?, 'x')",
        (txn_id, f"f{txn_id}", dt, cents, merchant))
    c.commit()          # run_backfill opens its own connections


def _run(c, scope, status="success"):
    c.execute("INSERT INTO amazon_sync_runs (started_at, completed_at, status, scope) "
              "VALUES ('t','t',?,?)", (status, scope))
    c.commit()


# ── scope derivation ─────────────────────────────────────────────────────────
def test_year_range_comes_from_the_ledger_not_a_guess(conn):
    _charge(conn, 1, "2024-06-10")
    _charge(conn, 2, "2026-07-20")
    _charge(conn, 3, "2023-01-01", merchant="WAL MART")   # not Amazon
    assert backfill.year_range(conn) == (2024, 2026)


def test_year_range_is_none_when_there_is_nothing_to_reconcile(conn):
    _charge(conn, 1, "2025-01-01", merchant="WAL MART")
    assert backfill.year_range(conn) is None


def test_plan_reports_nothing_to_do_rather_than_failing(conn):
    p = backfill.plan(conn)
    assert p["years"] == [] and "no Amazon charges" in p["reason"]


# ── resumability ─────────────────────────────────────────────────────────────
def test_completed_years_are_read_from_the_existing_run_ledger(conn):
    _run(conn, "year=2024")
    _run(conn, "year=2025", status="failed")     # failures do NOT count as done
    _run(conn, "days=60")                        # a window sync is not a year
    _run(conn, "year=notanumber")                # tolerated, not crashed on
    assert backfill.completed_years(conn) == {2024}


def test_resume_skips_finished_years_and_keeps_the_rest(conn):
    for i, y in enumerate(("2024", "2025", "2026"), start=1):
        _charge(conn, i, f"{y}-05-05")
    _run(conn, "year=2024")
    p = backfill.plan(conn, resume=True)
    assert p["years"] == [2025, 2026] and p["skipped"] == [2024]


def test_no_resume_refetches_everything(conn):
    for i, y in enumerate(("2024", "2025"), start=1):
        _charge(conn, i, f"{y}-05-05")
    _run(conn, "year=2024")
    p = backfill.plan(conn, resume=False)
    assert p["years"] == [2024, 2025] and p["skipped"] == []


def test_transactions_window_reaches_the_start_of_the_range(conn):
    _charge(conn, 1, "2024-06-10")
    p = backfill.plan(conn)
    # days-back must cover from today to 2024-01-01, with slack
    assert p["days"] >= (date.today() - date(2024, 1, 1)).days


# ── retry policy ─────────────────────────────────────────────────────────────
def test_transient_failure_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(backfill, "BACKOFF_SECONDS", (0, 0))
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("connection reset")
        return "ok"
    assert backfill._with_retry(flaky, what="x") == "ok"
    assert calls["n"] == 3


def test_auth_error_is_never_retried(monkeypatch):
    """The credential will not become valid on the third attempt, and hammering
    a challenge is how a soft block becomes a hard one."""
    monkeypatch.setattr(backfill, "BACKOFF_SECONDS", (0, 0))
    calls = {"n": 0}

    def denied():
        calls["n"] += 1
        raise AmazonAuthError("session expired")
    with pytest.raises(AmazonAuthError):
        backfill._with_retry(denied, what="x")
    assert calls["n"] == 1, "auth failure must not be retried"


def test_bot_challenge_is_never_retried(monkeypatch):
    monkeypatch.setattr(backfill, "BACKOFF_SECONDS", (0, 0))
    calls = {"n": 0}

    def challenged():
        calls["n"] += 1
        raise RuntimeError("Captcha challenge encountered")
    with pytest.raises(RuntimeError, match="Captcha"):
        backfill._with_retry(challenged, what="x")
    assert calls["n"] == 1


def test_retries_are_bounded_and_then_give_up(monkeypatch):
    monkeypatch.setattr(backfill, "BACKOFF_SECONDS", (0, 0))
    with pytest.raises(RuntimeError, match="failed after 2 retries"):
        backfill._with_retry(lambda: (_ for _ in ()).throw(RuntimeError("nope")),
                             what="x")


# ── the run, with fetch mocked ───────────────────────────────────────────────
class _Order:
    def __init__(self, num, dt, total=10.0):
        self.order_number, self.order_placed_date, self.grand_total = num, dt, total
        self.items, self.cancelled, self.item_count = [], False, 0
        self.subtotal = self.estimated_tax = self.shipping_total = None
        self.refund_total = None
        self.payment_method = "Visa"


def test_a_vacuous_year_aborts_that_year_and_the_others_survive(conn, monkeypatch):
    """A year that returns nothing while the ledger shows charges IN THAT YEAR
    is a broken parse. It must not take the whole multi-year run down, and it
    must not be recorded as done."""
    _charge(conn, 1, "2024-05-05")
    _charge(conn, 2, "2025-05-05")
    monkeypatch.setattr(backfill.fetch, "fetch_transactions", lambda **k: [])
    monkeypatch.setattr(
        backfill.fetch, "fetch_orders",
        lambda **k: [] if k.get("year") == 2024 else [_Order("A", date(2025, 5, 5))])

    r = backfill.run_backfill()
    assert r["years"] == [2025], "2024 aborted, 2025 stored"
    with db.connect() as c:
        assert backfill.completed_years(c) == {2025}, "an aborted year is not 'done'"


def test_auth_failure_mid_run_stops_but_keeps_what_was_stored(conn, monkeypatch):
    _charge(conn, 1, "2024-05-05")
    _charge(conn, 2, "2025-05-05")
    monkeypatch.setattr(backfill.fetch, "fetch_transactions", lambda **k: [])
    monkeypatch.setattr(backfill, "BACKOFF_SECONDS", ())

    def orders(**k):
        if k.get("year") == 2024:
            return [_Order("A", date(2024, 5, 5))]
        raise AmazonAuthError("session expired")
    monkeypatch.setattr(backfill.fetch, "fetch_orders", orders)

    r = backfill.run_backfill()
    assert r["years"] == [2024]
    with db.connect() as c:
        assert backfill.completed_years(c) == {2024}
        assert c.execute("SELECT COUNT(*) n FROM amazon_orders").fetchone()["n"] == 1


def test_a_second_run_is_idempotent(conn, monkeypatch):
    _charge(conn, 1, "2025-05-05")
    monkeypatch.setattr(backfill.fetch, "fetch_transactions", lambda **k: [])
    monkeypatch.setattr(backfill.fetch, "fetch_orders",
                        lambda **k: [_Order("SAME", date(2025, 5, 5))])
    backfill.run_backfill()
    backfill.run_backfill(resume=False)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM amazon_orders").fetchone()["n"] == 1


# ── the horizon ──────────────────────────────────────────────────────────────
def test_horizon_reports_the_boundary_and_what_lies_before_it(conn):
    """The number that makes a low coverage figure readable: those charges are
    not unmatched because matching is broken, but because nothing exists to
    match them against."""
    _charge(conn, 1, "2024-03-01", cents=-5000)
    _charge(conn, 2, "2024-04-01", cents=-2500)
    _charge(conn, 3, "2026-07-01", cents=-1000)
    store.store_transactions(
        conn, [type("T", (), {"completed_date": date(2026, 6, 1),
                              "grand_total": -10.0, "is_refund": False,
                              "order_number": "A", "payment_method": "Visa",
                              "seller": "Amazon"})()],
        store.start_run(conn, "t"))
    h = match.horizon(conn)
    assert h["earliest"] == "2026-06-01"
    assert h["pre_count"] == 2 and h["pre_cents"] == 7500
    assert h["has_backlog"] is True


def test_horizon_is_empty_when_no_transactions_are_stored(conn):
    _charge(conn, 1, "2024-03-01")
    h = match.horizon(conn)
    assert h["earliest"] is None and h["has_backlog"] is False


def test_no_backlog_when_every_charge_is_inside_the_window(conn):
    _charge(conn, 1, "2026-07-01")
    store.store_transactions(
        conn, [type("T", (), {"completed_date": date(2026, 1, 1),
                              "grand_total": -10.0, "is_refund": False,
                              "order_number": "A", "payment_method": "Visa",
                              "seller": "Amazon"})()],
        store.start_run(conn, "t"))
    assert match.horizon(conn)["has_backlog"] is False


# ── the per-year gate itself ─────────────────────────────────────────────────
def test_year_scoped_ledger_check_does_not_see_other_years(conn):
    """With only a lower bound, checking 2024 would count 2025's charges too —
    so a 2024 fetch that returned nothing would still look 'expected' and the
    gate would pass vacuously exactly where a long job needs it most."""
    from local_budget.connectors.amazon import sync as az_sync
    _charge(conn, 1, "2025-05-05")
    assert az_sync._ledger_has_amazon_charges(conn, "2025-01-01", "2025-12-31") is True
    assert az_sync._ledger_has_amazon_charges(conn, "2024-01-01", "2024-12-31") is False
    # unbounded (the old behaviour) would wrongly report True for 2024
    assert az_sync._ledger_has_amazon_charges(conn, "2024-01-01") is True
