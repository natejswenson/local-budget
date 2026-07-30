"""Report orchestrator: gather → compose → render, one call.

Consumes reports.py / detect.py data directly (the same producers the MCP
tools and dashboard read), so over/floor classification, money formatting
and the flags rules are computed exactly once, server-side — no extraction
from printed markdown. The LLM contributes only the optional `narrative`
paragraph (escaped as text in html.assemble).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .. import db, detect, paths, reports
from ..agent.render import money
from . import brand, charts, flags, html
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


def standfirst(summary: dict, overview: dict) -> str:
    """The serif line under the headline, derived from the same numbers the
    charts use — never a model's paraphrase of them.

    A report that opens on a bare grid of figures makes the reader do the
    comparison work themselves. This states the month in one sentence: what
    was spent, which way it moved, and how many budgets broke. Returned as
    plain text; html.assemble escapes it like any other narrative.
    """
    spent = int(summary["spend_total_cents"])
    bits = [f"Spent {money(spent)}"]

    delta = summary.get("mom_delta_cents")
    prev = summary.get("prev_month")
    if delta is not None and prev:
        prev_name = datetime.strptime(prev, "%Y-%m").strftime("%B")
        if delta == 0:
            bits.append(f"level with {prev_name}")
        else:
            direction = "up" if delta > 0 else "down"
            bits.append(f"{direction} {money(abs(delta))} from {prev_name}")
    line = ", ".join(bits) + "."

    over = [c for c in overview["categories"] if c.get("over")]
    if not over:
        return f"{line} Every budget held."
    # Rank by how far past, so the sentence names the worst offender, not
    # whichever category happens to sort first.
    worst = max(over, key=lambda c: c.get("pct") or 0)
    noun = "budget" if len(over) == 1 else "budgets"
    tail = f"{len(over)} {noun} over"
    if worst.get("pct"):
        tail += f", {worst['category']} furthest at {worst['pct']}%"
    return f"{line} {tail}."


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
        '<h3 class="block-title">Spend vs budget</h3>' + charts.spend_vs_budget(overview),
        charts.trend_chart(summary["trend"], highlight=period),
        charts.flags_section(
            flags.month_anomalies(anomalies, period, recurring),
            flags.month_recurring(recurring, txns, period),
            period),
    ]
    page = html.assemble(
        period=period, theme=brand.load_theme(), sections=sections,
        user_name=db.get_setting("user_name"),
        narrative=narrative or standfirst(summary, overview),
        generated_on=date.today().isoformat(),
        provenance=f"{len(txns)} posted transactions")

    base = (out_dir or paths.reports_dir()).resolve()
    out = (base / f"budget-report-{period}.pdf").resolve()
    if not out.is_relative_to(base):          # save_brief-style path confinement
        raise ValueError("invalid period")
    render_pdf(page, out)
    return {"ok": True, "path": str(out)}
