"""charts.py + html.py — golden snapshots and the recipe edge cases.

Golden files pin the exact HTML under tests/golden/report/; regenerate
deliberately with UPDATE_GOLDEN=1 after an intended visual change:

    UPDATE_GOLDEN=1 uv run pytest tests/test_report_charts.py
"""
from __future__ import annotations

import os
from pathlib import Path

from local_budget.report import charts, html, palette

GOLDEN = Path(__file__).parent / "golden" / "report"


def _check_golden(name: str, produced: str):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(produced)
    assert path.is_file(), f"golden missing — run UPDATE_GOLDEN=1 to create {name}"
    assert produced == path.read_text(), (
        f"{name} drifted — if intended, regenerate with UPDATE_GOLDEN=1")


def _cat(name, spent, budget=None, *, over=False, floor=False, pct=None):
    return {"category": name, "spent_cents": spent, "budget_cents": budget,
            "over": over, "floor": floor, "pct": pct}


_OVERVIEW = {"month": "2026-06", "categories": [
    _cat("Groceries", 42000, 50000, pct=84),                      # warning (>=80)
    _cat("Dining Out", 21000, 20000, over=True, pct=105),         # ceiling over
    _cat("Gas", 9000),                                            # no budget → good
    _cat("Investments", 0, 50000, over=True, floor=True, pct=0),  # floor missed @ $0
    _cat("NY529", 60000, 50000, over=False, floor=True, pct=120), # floor met (>100%)
    _cat("Housing", 0, 120000, pct=0),                            # zero spend → excluded
    _cat("Refunds", -500),                                        # negative → excluded
]}

_SUMMARY = {"spend_total_cents": 132000, "income_cents": 250000}

_TREND = [{"month": f"2026-{m:02d}", "spend_cents": 100000 + m * 1000,
           "income_cents": 200000} for m in range(1, 7)]


def test_stat_row_golden_and_net_color():
    out = charts.stat_row(_SUMMARY)
    assert "$1,320.00" in out and "$2,500.00" in out and "$1,180.00" in out
    assert "var(--report-good)" in out          # positive net
    neg = charts.stat_row({"spend_total_cents": 300000, "income_cents": 100000})
    assert "var(--report-critical)" in neg and "-$2,000.00" in neg
    _check_golden("stat_row.html", out)


def test_stat_row_savings_tile_shown_and_not_subtracted_from_net():
    # No savings_total_cents key (or zero) -> no Savings tile, byte-identical
    # to the no-floor-categories case (golden fixture above has neither key).
    assert "Savings" not in charts.stat_row(_SUMMARY)
    out = charts.stat_row({"spend_total_cents": 262723, "savings_total_cents": 1300000,
                           "income_cents": 645727})
    assert "Savings" in out and "$13,000.00" in out
    # Net = income - spent only; savings is NOT subtracted (Spent 2627.23,
    # Income 6457.27 -> Net 3830.04, positive despite the $13k also moved).
    assert "$3,830.04" in out and "var(--report-good)" in out


def test_spend_vs_budget_golden_and_rules():
    out = charts.spend_vs_budget(_OVERVIEW)
    # row set: Housing (zero-spend ceiling) and Refunds (negative) are out;
    # Investments (floor, over, $0) is IN with a zero-width bar
    assert "Housing" not in out and "Refunds" not in out
    assert "Investments" in out and "width:0.0%" in out
    # colors: ceiling-over + floor-missed → critical; floor met NEVER warning
    assert out.count("var(--report-critical)") == 2
    ny529 = out.split("NY529")[1].split("sb-row")[0]
    assert "var(--report-good)" in ny529 and "var(--report-warning)" not in ny529
    # warning tier for the 84% ceiling row
    groceries = out.split("Groceries")[1].split("sb-row")[0]
    assert "var(--report-warning)" in groceries
    # shared scale = Housing excluded, so max(60000 spent, 50000 budgets…) etc.
    # ticks always inside the row: no tick position > 100%
    for chunk in out.split('class="tick" style="left:')[1:]:
        assert float(chunk.split("%")[0]) <= 100.0
    # over rows carry the ⚠ marker
    assert "⚠ Dining Out" in out and "⚠ Investments" in out
    _check_golden("spend_vs_budget.html", out)


def test_spend_vs_budget_empty():
    out = charts.spend_vs_budget({"month": "2026-06", "categories": [
        _cat("Housing", 0, 120000)]})
    assert "no spending to show" in out


def test_flags_section_golden_and_gates():
    anomalies = [{"posted_date": "2026-06-10", "merchant": "COSTCO", "amount_cents": -90000}]
    recurring = [{"merchant": "NETFLIX", "amount_cents": -1500,
                  "posted_date": "2026-06-05", "months": 8}]
    out = charts.flags_section(anomalies, recurring, "2026-06")
    assert "Unusual charges" in out and "recurring bills in 2026-06" in out
    assert "-$900.00" in out and "-$15.00" in out
    assert "Amount" in out and "Avg amount" not in out     # month-scoped header
    _check_golden("flags_section.html", out)
    # independent subsection gates
    only_rec = charts.flags_section([], recurring, "2026-06")
    assert "Unusual charges" not in only_rec and "NETFLIX" in only_rec
    assert "nothing to flag" in charts.flags_section([], [], "2026-06")


def test_trend_chart_golden_and_empty():
    out = charts.trend_chart(_TREND)
    assert "<svg" in out and out.count("<rect") == 12       # 6 months × 2 series
    assert "no history yet" in charts.trend_chart([])
    _check_golden("trend_chart.html", out)


def test_assemble_full_page_golden_escapes_and_tokens():
    toks = palette.tokens()
    page = html.assemble(
        period="2026-06", tokens=toks,
        sections=[charts.stat_row(_SUMMARY), charts.spend_vs_budget(_OVERVIEW)],
        user_name="Nate <script>alert(1)</script>",
        narrative="Spending & saving on track — <b>not bold</b>",
        generated_on="2026-07-11")
    assert "<script>alert(1)</script>" not in page          # escaped
    assert "&lt;b&gt;not bold&lt;/b&gt;" in page            # narrative is text, not HTML
    assert "--report-accent:#2a78d6" in page                # tokens inlined on :root
    assert "@page{size:letter" in page
    _check_golden("full_page.html", page)
