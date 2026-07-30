"""Best-effort refresh before a report. Never fails the caller.

Three rules, each the answer to a way this could go wrong:

1. **A failed sync must never fail the report.** The Amazon session expires on
   Amazon's schedule, and re-authenticating needs a human, a browser and a
   phone. A month-end report that cannot render because a scraper's cookie
   went stale is a worse product than one rendered from slightly older item
   data. Every failure is caught and reported as a note.

2. **Only refresh when fresh data could change the answer.** Re-rendering a
   2024 report gains nothing from pulling the last 60 days of orders, and a
   full-year backfill would turn a two-second render into a long network job
   nobody asked for. Auto-sync applies to the current and previous month only;
   anything older renders from what is already stored.

3. **Don't re-sync on every render.** Rendering three reports in a row should
   hit Amazon once. A recent successful run is treated as good enough.

This lives outside `report/` on purpose: `render_report()` stays a pure,
deterministic function of the database, which is what makes its output
reproducible and its tests offline.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ... import db

#: A successful sync this recent is good enough; skip and render.
DEFAULT_MAX_AGE_HOURS = 12


def _month_start(period: str) -> date:
    return date(int(period[:4]), int(period[5:7]), 1)


def is_recent_period(period: str, today: date | None = None) -> bool:
    """Current or previous month — the only ones a 60-day pull can improve."""
    today = today or date.today()
    this_month = date(today.year, today.month, 1)
    prev = (this_month - timedelta(days=1)).replace(day=1)
    return _month_start(period) in (this_month, prev)


def last_success_at(conn) -> datetime | None:
    row = conn.execute(
        "SELECT completed_at FROM amazon_sync_runs "
        "WHERE status='success' AND completed_at IS NOT NULL "
        "ORDER BY sync_run_id DESC LIMIT 1").fetchone()
    if not row or not row["completed_at"]:
        return None
    try:
        return datetime.fromisoformat(row["completed_at"])
    except ValueError:
        return None


def maybe_sync(period: str, *, max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
               days: int = 60, now: datetime | None = None) -> dict:
    """Refresh Amazon data if it would help and is possible.

    Returns ``{"status": ..., "detail": str}`` where status is one of
    ``synced`` / ``fresh`` / ``old-period`` / ``no-session`` / ``failed``.
    **Never raises** — the caller is about to render a report either way.
    """
    now = now or datetime.now()

    if not is_recent_period(period):
        return {"status": "old-period",
                "detail": f"{period} is not the current or previous month"}

    try:
        from .session import stored_session_looks_valid
        if not stored_session_looks_valid():
            return {"status": "no-session",
                    "detail": "no saved Amazon session — run `budget amazon login`"}

        with db.connect() as conn:
            last = last_success_at(conn)
        if last and (now - last) < timedelta(hours=max_age_hours):
            age = int((now - last).total_seconds() // 3600)
            return {"status": "fresh", "detail": f"last synced {age}h ago"}

        from .sync import run_sync
        r = run_sync(days=days)
        return {"status": "synced",
                "detail": f"{r['orders']} orders · {r['matched']} matched · "
                          f"{r['coverage']['coverage_pct']}% coverage"}
    except Exception as e:
        # Deliberately broad. Anything from an expired cookie to Amazon being
        # down to a page redesign lands here, and none of it is a reason to
        # refuse to render a budget report.
        return {"status": "failed", "detail": f"{type(e).__name__}: {str(e)[:160]}"}
