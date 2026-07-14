"""Report orchestrator: gather → compose → render, one call.

Consumes reports.py / detect.py data directly (the same producers the MCP
tools and dashboard read), so over/floor classification, money formatting
and the flags rules are computed exactly once, server-side — no extraction
from printed markdown. The LLM contributes only the optional `narrative`
paragraph (escaped as text in html.assemble).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from .. import db, detect, paths, reports
from . import charts, flags, html, palette
from .pdf import ChromeNotFoundError, render_pdf  # noqa: F401 (re-exported)

PERIOD_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")


def _month_txns(month: str) -> list[dict]:
    """The month's posted rows for the flags cross-reference — the sanitized
    projection only (merchant_norm, cents, date, category)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT merchant_norm, canonical_merchant, amount_cents, posted_date, category "
            "FROM transactions WHERE status='posted' AND posted_date LIKE ?",
            (f"{month}-%",)).fetchall()
    return [dict(r) for r in rows]


def render_report(period: str, narrative: str | None = None,
                  out_dir: Path | None = None) -> dict:
    """Render the month's visual report PDF. Returns {"ok": True, "path": str}.
    Raises ValueError on a bad period and ChromeNotFoundError when no browser
    is available (callers surface the fallback guidance)."""
    period = (period or "").strip()
    if not PERIOD_RE.match(period):
        raise ValueError("invalid period (use YYYY-MM)")

    summary = reports.month_summary(period)
    overview = reports.budget_overview(period)
    recurring = detect.recurring()
    anomalies = detect.anomalies()
    txns = _month_txns(period)

    sections = [
        charts.stat_row(summary),
        "<h3>💸 Spend vs budget</h3>" + charts.spend_vs_budget(overview),
        charts.trend_chart(summary["trend"]),
        "<h3>🚩 Flags</h3>" + charts.flags_section(
            flags.month_anomalies(anomalies, period, recurring),
            flags.month_recurring(recurring, txns, period),
            period),
    ]
    page = html.assemble(
        period=period, tokens=palette.tokens(), sections=sections,
        user_name=db.get_setting("user_name"), narrative=narrative,
        generated_on=date.today().isoformat())

    base = (out_dir or paths.reports_dir()).resolve()
    out = (base / f"budget-report-{period}.pdf").resolve()
    if not out.is_relative_to(base):          # save_brief-style path confinement
        raise ValueError("invalid period")
    render_pdf(page, out)
    return {"ok": True, "path": str(out)}
