"""Multi-year backfill — planning, resumability, and stopping safely.

The fetch boundary is faked throughout: `fetch.browser_session` yields an object
with `order_list` and `order_detail`, and that is the entire contract backfill
depends on. Nothing here opens a browser.

The property most worth pinning is that an INTERRUPTED backfill is still worth
having run: the cheap list pass lands first, so coverage improves before a
single detail page is read, and the next run picks up exactly where this one
stopped without re-fetching what it already has.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from local_budget import db
from local_budget.connectors.walmart import backfill, store
from local_budget.connectors.walmart.session import WalmartAuthError


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


@pytest.fixture(autouse=True)
def no_polite_delay(monkeypatch):
    """The 1.5s courtesy pause between detail fetches is real behaviour, not
    something a test should sit through."""
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)


def _bank(c, txn_id, dt, cents, merchant="WALMART.COM"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, imported_at) "
        "VALUES (?,1,?,?,?, 'posted',?,'x')",
        (txn_id, f"f{txn_id}", dt, cents, merchant))


def _order(num, dt, total, *, detail=False, items=None):
    return {"order_number": num, "order_placed_date": dt, "grand_total": total,
            "channel": "online", "detail_fetched": detail,
            "items": items or [], "charges": []}


class FakeFetcher:
    """Stands in for the Playwright-backed fetcher. `calls` is the assertion
    surface: which detail pages a run actually spent a request on."""

    def __init__(self, orders, *, fail_on=None):
        self.orders = orders
        self.fail_on = fail_on
        self.calls: list[str] = []

    def order_list(self, *, since=None, on_progress=None):
        return [dict(o) for o in self.orders]

    def order_detail(self, order_number):
        self.calls.append(order_number)
        if order_number == self.fail_on:
            raise WalmartAuthError("session expired")
        src = next(o for o in self.orders if o["order_number"] == order_number)
        return {**src, "detail_fetched": True,
                "items": [{"title": f"item for {order_number}",
                           "unit_price": "10.00", "quantity": 1,
                           "product_id": f"p-{order_number}"}]}


@pytest.fixture()
def fetcher(monkeypatch):
    """Install a FakeFetcher and hand the test a hook to configure it."""
    holder = {}

    @contextmanager
    def fake_session(*, headless=True):
        yield holder["f"]

    import sys
    import types
    mod = types.ModuleType("local_budget.connectors.walmart.fetch")
    mod.browser_session = fake_session
    monkeypatch.setitem(sys.modules, "local_budget.connectors.walmart.fetch", mod)
    return holder


# ── planning ─────────────────────────────────────────────────────────────────
def test_the_default_range_comes_from_the_ledger_not_a_guess(conn):
    """Fetching further back would be requests spent on orders no bank row will
    ever match."""
    _bank(conn, 1, "2024-06-11", -1000)
    _bank(conn, 2, "2026-07-28", -2000)
    assert backfill.earliest_charge_date(conn) == "2024-06-11"


def test_a_ledger_with_no_walmart_charges_has_nothing_to_backfill(conn):
    _bank(conn, 1, "2026-07-01", -1000, merchant="TARGET")
    assert backfill.plan(conn)["reason"] == "no Walmart charges in the ledger"


def test_plan_counts_what_still_needs_detail(conn):
    _bank(conn, 1, "2026-07-01", -1000)
    store.store_orders(conn, [_order("A", "2026-07-01", "10.00"),
                              _order("B", "2026-07-02", "20.00", detail=True)],
                       store.start_run(conn, "t"))
    p = backfill.plan(conn)
    assert (p["stored"], p["pending"]) == (2, 1)


def test_a_cancelled_order_is_never_queued_for_detail(conn):
    """It has no items to collect and no charge to reconcile, so a request spent
    on one buys nothing."""
    o = _order("X", "2026-07-01", "10.00")
    o["cancelled"] = True
    store.store_orders(conn, [o], store.start_run(conn, "t"))
    assert backfill.pending_detail(conn) == []


def test_detail_is_queued_newest_first(conn):
    """That is where the ledger's unexplained charges concentrate, and where an
    interrupted run leaves the most value behind."""
    store.store_orders(conn, [_order("OLD", "2024-01-01", "10.00"),
                              _order("NEW", "2026-07-01", "20.00")],
                       store.start_run(conn, "t"))
    assert backfill.pending_detail(conn) == ["NEW", "OLD"]


# ── running ──────────────────────────────────────────────────────────────────
def test_the_list_pass_lands_before_any_detail_is_read(conn, fetcher, tmp_path,
                                                       monkeypatch):
    """This is what makes an interrupted backfill worth having run: every order
    and its derived charge are stored, so coverage improves before a single
    detail request."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-01", -1000)
    conn.commit()
    fetcher["f"] = FakeFetcher([_order("A", "2026-07-01", "10.00")],
                               fail_on="A")
    r = backfill.run_backfill()
    assert r["orders"] == 1
    assert r["detailed"] == 0
    assert r["matched"] == 1, "the derived charge matched without any detail"


def test_detail_is_fetched_and_stored(conn, fetcher, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-01", -1000)
    conn.commit()
    fetcher["f"] = FakeFetcher([_order("A", "2026-07-01", "10.00")])
    r = backfill.run_backfill()
    assert r["detailed"] == 1
    with db.connect(tmp_path / "budget.db") as c:
        assert c.execute("SELECT COUNT(*) n FROM walmart_items").fetchone()["n"] == 1


def test_a_second_run_does_not_refetch_detail_it_already_has(conn, fetcher,
                                                             tmp_path, monkeypatch):
    """Resumability with no new bookkeeping: `detail_fetched` is simply true or
    false for each order."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-01", -1000)
    conn.commit()
    fetcher["f"] = FakeFetcher([_order("A", "2026-07-01", "10.00")])
    backfill.run_backfill()
    fetcher["f"] = FakeFetcher([_order("A", "2026-07-01", "10.00")])
    backfill.run_backfill()
    assert fetcher["f"].calls == [], "the second run spent no detail requests"


def test_losing_the_session_stops_the_run_and_keeps_what_it_stored(
        conn, fetcher, tmp_path, monkeypatch):
    """A backfill that dies at 80% must keep everything it collected — and say
    how to resume, rather than re-raising an auth error into a traceback."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-03", -3000)
    conn.commit()
    orders = [_order("C", "2026-07-03", "30.00"),   # newest — fetched first
              _order("B", "2026-07-02", "20.00"),
              _order("A", "2026-07-01", "10.00")]
    fetcher["f"] = FakeFetcher(orders, fail_on="B")
    r = backfill.run_backfill()
    assert fetcher["f"].calls == ["C", "B"], "it stopped instead of grinding on"
    assert r["detailed"] == 1
    assert r["remaining"] == 2
    assert r["stopped_early"]
    with db.connect(tmp_path / "budget.db") as c:
        assert c.execute("SELECT COUNT(*) n FROM walmart_orders").fetchone()["n"] == 3


def test_limit_bounds_a_run_and_the_rest_resumes(conn, fetcher, tmp_path,
                                                 monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    _bank(conn, 1, "2026-07-01", -1000)
    conn.commit()
    orders = [_order(n, f"2026-07-0{i+1}", "10.00")
              for i, n in enumerate(["A", "B", "C"])]
    fetcher["f"] = FakeFetcher(orders)
    r = backfill.run_backfill(limit=2)
    assert len(fetcher["f"].calls) == 2
    assert r["remaining"] == 1


# ── retry policy ─────────────────────────────────────────────────────────────
def test_a_transient_failure_is_retried(monkeypatch):
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("connection reset")
        return "ok"

    assert backfill._with_retry(flaky, what="x") == "ok"
    assert len(attempts) == 3


def test_an_auth_error_is_never_retried(monkeypatch):
    """The session is not going to become valid on the third attempt."""
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    attempts = []

    def dead():
        attempts.append(1)
        raise WalmartAuthError("expired")

    with pytest.raises(WalmartAuthError):
        backfill._with_retry(dead, what="x")
    assert len(attempts) == 1


@pytest.mark.parametrize("msg", ["bot challenge served", "CAPTCHA required",
                                 "are you a robot?"])
def test_a_challenge_is_never_retried(monkeypatch, msg):
    """Repeatedly re-hitting a challenge is how a soft block becomes a hard
    one."""
    monkeypatch.setattr(backfill.time, "sleep", lambda *_: None)
    attempts = []

    def challenged():
        attempts.append(1)
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        backfill._with_retry(challenged, what="x")
    assert len(attempts) == 1
