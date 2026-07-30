"""Pre-report Amazon refresh — the guarantee is that it CANNOT break a report.

Every test here is really the same assertion from a different angle: whatever
goes wrong with a scraper that depends on a cookie, a network and Amazon's
markup, `budget report-pdf` still renders.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from local_budget import db
from local_budget.connectors.amazon import autosync


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    db.init_schema(tmp_path / "budget.db")
    return tmp_path


def _session(env, valid=True):
    from local_budget.connectors.amazon import session as az
    az.cookie_path().write_text('{"x-main": "abc"}' if valid else "{}")


def _record_sync(when: datetime, status="success"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO amazon_sync_runs (started_at, completed_at, status, scope) "
            "VALUES (?,?,?,'days=60')",
            (when.isoformat(timespec="seconds"), when.isoformat(timespec="seconds"), status))


# ── when it should not even try ──────────────────────────────────────────────
@pytest.mark.parametrize("period,expected", [
    ("2026-07", True),      # current month
    ("2026-06", True),      # previous
    ("2026-05", False),     # older — a 60-day pull cannot improve it
    ("2024-03", False),
    ("2026-08", False),     # future
])
def test_only_the_current_and_previous_month_are_refreshed(period, expected):
    assert autosync.is_recent_period(period, today=date(2026, 7, 29)) is expected


def test_year_boundary_does_not_break_the_previous_month(env):
    assert autosync.is_recent_period("2025-12", today=date(2026, 1, 5)) is True
    assert autosync.is_recent_period("2025-11", today=date(2026, 1, 5)) is False


def test_old_period_skips_without_touching_the_network(env, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not sync for an old period")
    monkeypatch.setattr("local_budget.connectors.amazon.sync.run_sync", explode)
    assert autosync.maybe_sync("2024-03")["status"] == "old-period"


def test_no_saved_session_is_a_note_not_a_crash(env, monkeypatch):
    monkeypatch.setattr(
        "local_budget.connectors.amazon.session.stored_session_looks_valid",
        lambda: False)
    r = autosync.maybe_sync("2026-07")
    assert r["status"] == "no-session" and "budget amazon login" in r["detail"]


def test_a_recent_sync_is_reused(env, monkeypatch):
    _session(env)
    _record_sync(datetime(2026, 7, 29, 9, 0))
    monkeypatch.setattr(
        "local_budget.connectors.amazon.session.stored_session_looks_valid",
        lambda: True)
    monkeypatch.setattr("local_budget.connectors.amazon.sync.run_sync",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should not sync")))
    r = autosync.maybe_sync("2026-07", now=datetime(2026, 7, 29, 15, 0))
    assert r["status"] == "fresh" and "6h ago" in r["detail"]


def test_a_stale_sync_triggers_a_refresh(env, monkeypatch):
    _session(env)
    _record_sync(datetime(2026, 7, 27, 9, 0))       # two days old
    monkeypatch.setattr(
        "local_budget.connectors.amazon.session.stored_session_looks_valid",
        lambda: True)
    monkeypatch.setattr(
        "local_budget.connectors.amazon.sync.run_sync",
        lambda **k: {"orders": 3, "matched": 2, "coverage": {"coverage_pct": 88.0}})
    r = autosync.maybe_sync("2026-07", now=datetime(2026, 7, 29, 9, 0))
    assert r["status"] == "synced" and "88.0% coverage" in r["detail"]


def test_a_failed_sync_only_ever_produces_a_note(env, monkeypatch):
    """The whole point. An expired cookie, Amazon down, a page redesign — none
    of it is a reason to be unable to render a budget report."""
    _session(env)
    monkeypatch.setattr(
        "local_budget.connectors.amazon.session.stored_session_looks_valid",
        lambda: True)

    def boom(**k):
        raise RuntimeError("Amazon changed a page")
    monkeypatch.setattr("local_budget.connectors.amazon.sync.run_sync", boom)
    r = autosync.maybe_sync("2026-07")
    assert r["status"] == "failed" and "Amazon changed a page" in r["detail"]


def test_even_a_failure_inside_the_db_is_swallowed(env, monkeypatch):
    """maybe_sync must not raise even if the database read itself fails."""
    monkeypatch.setattr(
        "local_budget.connectors.amazon.session.stored_session_looks_valid",
        lambda: True)
    monkeypatch.setattr("local_budget.db.connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    assert autosync.maybe_sync("2026-07")["status"] == "failed"


# ── sync orchestration (fetch mocked; no network) ────────────────────────────
def test_a_failed_sync_still_leaves_evidence_in_the_run_ledger(env, monkeypatch):
    """db.connect() rolls back on exception, which would take the failure
    record with it — so the failed run is written in a SECOND, independent
    transaction. Reasoned about when written; asserted here, because a run
    ledger that silently loses every failure is worse than none."""
    from local_budget.connectors.amazon import sync

    monkeypatch.setattr(sync.fetch, "fetch_orders", lambda **k: [object()])
    monkeypatch.setattr(sync.fetch, "fetch_transactions", lambda **k: [])
    monkeypatch.setattr(sync.store, "store_orders",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom mid-write")))

    with pytest.raises(RuntimeError, match="boom mid-write"):
        sync.run_sync(days=30)

    with db.connect() as conn:
        row = conn.execute("SELECT status, error_message FROM amazon_sync_runs "
                           "ORDER BY sync_run_id DESC LIMIT 1").fetchone()
    assert row is not None, "the failure was rolled back with the transaction"
    assert row["status"] == "failed" and "boom mid-write" in row["error_message"]


def test_an_aborted_vacuous_sync_writes_nothing_at_all(env, monkeypatch):
    """A broken parser must leave NO trace — not even a run row. There is
    nothing to unwind because nothing began, and a 'failed' row here would be
    noise on every render for an account that simply has no orders."""
    from local_budget.connectors.amazon import store, sync

    monkeypatch.setattr(sync.fetch, "fetch_orders", lambda **k: [])
    monkeypatch.setattr(sync.fetch, "fetch_transactions", lambda **k: [])
    monkeypatch.setattr(sync, "_ledger_has_amazon_charges", lambda *a: True)

    with pytest.raises(store.SyncAborted):
        sync.run_sync(days=30)

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM amazon_sync_runs").fetchone()["c"] == 0


def test_a_failed_prior_run_does_not_count_as_fresh(env, monkeypatch):
    """Only a SUCCESSFUL run suppresses a retry — otherwise one broken sync
    would suppress every retry for 12 hours and hide the breakage."""
    _session(env)
    _record_sync(datetime(2026, 7, 29, 9, 0), status="failed")
    monkeypatch.setattr(
        "local_budget.connectors.amazon.session.stored_session_looks_valid",
        lambda: True)
    called = {}
    monkeypatch.setattr(
        "local_budget.connectors.amazon.sync.run_sync",
        lambda **k: called.setdefault("yes", True) and
        {"orders": 1, "matched": 1, "coverage": {"coverage_pct": 50.0}})
    autosync.maybe_sync("2026-07", now=datetime(2026, 7, 29, 10, 0))
    assert called.get("yes"), "a failed run must not suppress the next attempt"
