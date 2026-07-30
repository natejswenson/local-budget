"""Pre-report auto-sync. Its whole job is to never be the reason a report fails.

Every branch here is a way the report could have been blocked by a scraper: an
expired session, a Walmart outage, a page redesign, a period no fetch could
improve. None of them is a reason to refuse to render a budget report.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from local_budget import db
from local_budget.connectors.walmart import autosync


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        yield c


TODAY = date(2026, 7, 30)


@pytest.mark.parametrize("period,recent", [
    ("2026-07", True),          # this month
    ("2026-06", True),          # last month
    ("2026-05", False),
    ("2024-11", False),
])
def test_only_the_current_and_previous_month_can_be_improved_by_a_60_day_pull(
        period, recent):
    assert autosync.is_recent_period(period, TODAY) is recent


def test_an_old_period_renders_from_stored_data_without_touching_the_network():
    """A full backfill would turn a two-second render into a long network job
    nobody asked for."""
    r = autosync.maybe_sync("2024-03")
    assert r["status"] == "old-period"


def test_no_session_is_reported_not_raised(conn, monkeypatch):
    monkeypatch.setattr(
        "local_budget.connectors.walmart.session.stored_session_looks_valid",
        lambda: False)
    r = autosync.maybe_sync("2026-07")
    assert r["status"] == "no-session"
    assert "budget walmart login" in r["detail"]


def test_a_recent_success_is_good_enough_to_skip(conn, monkeypatch):
    """Rendering three reports in a row should hit Walmart once — and each order
    costs its own detail request, so a redundant sync is expensive as well as
    rude."""
    monkeypatch.setattr(
        "local_budget.connectors.walmart.session.stored_session_looks_valid",
        lambda: True)
    now = datetime(2026, 7, 30, 12, 0)
    conn.execute("INSERT INTO walmart_sync_runs (started_at, completed_at, "
                 "status, scope) VALUES (?,?, 'success','days=60')",
                 ((now - timedelta(hours=2)).isoformat(), (now - timedelta(hours=2)).isoformat()))
    conn.commit()
    r = autosync.maybe_sync("2026-07", now=now)
    assert r["status"] == "fresh"


def test_a_stale_success_does_not_count_as_fresh(conn, monkeypatch):
    monkeypatch.setattr(
        "local_budget.connectors.walmart.session.stored_session_looks_valid",
        lambda: True)
    monkeypatch.setattr("local_budget.connectors.walmart.sync.run_sync",
                        lambda **kw: {"orders": 3, "matched": 2,
                                      "coverage": {"coverage_pct": 88.0}})
    now = datetime(2026, 7, 30, 12, 0)
    old = (now - timedelta(hours=40)).isoformat()
    conn.execute("INSERT INTO walmart_sync_runs (started_at, completed_at, "
                 "status, scope) VALUES (?,?, 'success','days=60')", (old, old))
    conn.commit()
    r = autosync.maybe_sync("2026-07", now=now)
    assert r["status"] == "synced"
    assert "88.0% coverage" in r["detail"]


def test_a_failed_sync_becomes_a_note_never_an_exception(conn, monkeypatch):
    """The rule the whole module exists for. A month-end report that cannot
    render because a scraper's session went stale is a worse product than one
    rendered from slightly older item data."""
    monkeypatch.setattr(
        "local_budget.connectors.walmart.session.stored_session_looks_valid",
        lambda: True)

    def boom(**kw):
        raise RuntimeError("Walmart served a bot challenge")

    monkeypatch.setattr("local_budget.connectors.walmart.sync.run_sync", boom)
    r = autosync.maybe_sync("2026-07")
    assert r["status"] == "failed"
    assert "bot challenge" in r["detail"]


def test_a_corrupt_completed_at_does_not_crash_the_freshness_check(conn):
    conn.execute("INSERT INTO walmart_sync_runs (started_at, completed_at, "
                 "status, scope) VALUES ('x','not-a-date','success','t')")
    assert autosync.last_success_at(conn) is None


def test_a_failed_run_is_never_mistaken_for_a_fresh_one(conn):
    """Otherwise a connector that has been broken for a week reports itself as
    freshly synced and the report silently goes stale."""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT INTO walmart_sync_runs (started_at, completed_at, "
                 "status, scope, error_message) "
                 "VALUES (?,?, 'failed','days=60','boom')", (now, now))
    assert autosync.last_success_at(conn) is None
