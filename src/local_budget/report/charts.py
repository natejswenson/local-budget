"""Report recipes as pure functions: tool-shaped data in, HTML fragments out.

Encodes budget-visualizer's three recipes (stat row, spend-vs-budget bars,
flags list) plus a monthly-trend chart, deterministically. All money strings
come from agent/render.money — the same formatter the tools use (rule 3 made
structural). Fragments are plain HTML/inline-SVG, no JS, so golden-file
snapshots pin the output byte-for-byte.
"""
from __future__ import annotations

import html as _html

from ..agent.render import money

# Palette token names (resolved to hex by html.assemble via palette.tokens()).
_GOOD = "var(--report-good)"
_WARNING = "var(--report-warning)"
_CRITICAL = "var(--report-critical)"
_TRACK = "var(--report-gridline)"
_ACCENT = "var(--report-accent)"


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


# ── recipe 1: stat row ────────────────────────────────────────────────────────
def stat_row(summary: dict) -> str:
    """Spent / [Savings] / Income / Net tiles from reports.month_summary data.
    Net has no dedicated field, so it's computed here from the same integer
    cents the old recipe extracted — formatting still goes through money().
    Savings (floor-marked categories like Investments — money relocated, not
    spent) is its own tile, shown only when present, and is NOT subtracted
    from Net: Net = income - spent answers "did ordinary spending stay under
    income," independent of how much also went to savings that month."""
    spent = int(summary["spend_total_cents"])
    income = int(summary["income_cents"])
    savings = int(summary.get("savings_total_cents") or 0)
    net = income - spent
    net_color = _CRITICAL if net < 0 else _GOOD
    tiles = [("Spent", money(spent), "inherit")]
    if savings:
        tiles.append(("Savings", money(savings), "inherit"))
    tiles += [
        ("Income", money(income), "inherit"),
        ("Net", money(net), net_color),
    ]
    cells = "".join(
        f'<div class="stat"><div class="label">{_esc(label)}</div>'
        f'<div class="value" style="color:{color}">{_esc(value)}</div></div>'
        for label, value, color in tiles)
    return f'<section class="stat-row">{cells}</section>'


# ── recipe 2: spend vs budget ─────────────────────────────────────────────────
def _row_color(cat: dict) -> str:
    """budget-visualizer recipe-2 color rules, verbatim:
    floor rows: `over` alone decides (pct NEVER selects warning);
    ceiling rows: over → critical; budgeted and pct >= 80 → warning; else good
    (no budget / zero-negative spend has no over signal → good)."""
    if cat.get("floor"):
        return _CRITICAL if cat.get("over") else _GOOD
    if cat.get("over"):
        return _CRITICAL
    if cat.get("budget_cents") is not None and (cat.get("pct") or 0) >= 80:
        return _WARNING
    return _GOOD


def _in_row_set(cat: dict) -> bool:
    """Positive spend only — except a floor row still short of its target
    (over == true), which must render even at $0 (the single most off-track
    case the floor feature exists to surface)."""
    if cat.get("floor") and cat.get("over"):
        return True
    return int(cat.get("spent_cents") or 0) > 0


def spend_vs_budget(overview: dict) -> str:
    """One row per category from reports.budget_overview: bar = spend, thin
    tick at the budget position, one shared scale across all rows (a big
    barely-touched budget's tick must not clip)."""
    rows = sorted((c for c in overview["categories"] if _in_row_set(c)),
                  key=lambda c: (-int(c.get("spent_cents") or 0), c["category"]))
    if not rows:
        return ('<section class="spend-budget">'
                '<p class="empty">no spending to show</p></section>')

    scale = max(
        [int(r.get("spent_cents") or 0) for r in rows]
        + [int(r["budget_cents"]) for r in rows if r.get("budget_cents") is not None]
    ) or 1

    out = ['<section class="spend-budget">']
    for c in rows:
        spent = int(c.get("spent_cents") or 0)
        budget = c.get("budget_cents")
        color = _row_color(c)
        width = round(max(spent, 0) / scale * 100, 2)   # bar floors at zero
        warn = "⚠ " if c.get("over") else ""
        if budget is not None:
            pct = c.get("pct")
            trailing = f"{money(spent)} of {money(int(budget))}"
            if pct is not None:
                trailing += f" · {pct}%"
            tick_left = round(int(budget) / scale * 100, 2)
            tick = f'<span class="tick" style="left:{tick_left}%"></span>'
        else:
            trailing = money(spent)
            tick = ""
        out.append(
            f'<div class="sb-row"><div class="sb-label">{warn}{_esc(c["category"])}</div>'
            f'<div class="sb-track">'
            f'<span class="sb-fill" style="width:{width}%;background:{color}"></span>'
            f'{tick}</div>'
            f'<div class="sb-value">{_esc(trailing)}</div></div>')
    out.append("</section>")
    return "".join(out)


# ── recipe 3: flags list ──────────────────────────────────────────────────────
def flags_section(month_anomalies: list[dict], month_recurring: list[dict],
                  month: str) -> str:
    """Unusual charges + subscriptions/recurring bills, each subsection
    independently shown-or-omitted; both empty → "nothing to flag". Inputs are
    the ALREADY-scoped lists from report.flags."""
    parts = ['<section class="flags">']
    if month_anomalies:
        rows = "".join(
            f'<tr><td>{_esc(a.get("posted_date"))}</td>'
            f'<td>{_esc(a.get("merchant") or "—")}</td>'
            f'<td class="num">{_esc(money(int(a["amount_cents"])))}</td></tr>'
            for a in month_anomalies)
        parts.append(
            '<h3>⚡ Unusual charges</h3>'
            f'<table><thead><tr><th>Date</th><th>Merchant</th><th class="num">Amount</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')
    if month_recurring:
        rows = "".join(
            f'<tr><td>{_esc(r["merchant"])}</td>'
            f'<td class="num">{_esc(money(int(r["amount_cents"])))}</td>'
            f'<td>{_esc(r["posted_date"])}</td><td class="num">{_esc(r["months"])}</td></tr>'
            for r in month_recurring)
        parts.append(
            f'<h3>🔁 Subscriptions &amp; recurring bills in {_esc(month)}</h3>'
            f'<table><thead><tr><th>Merchant</th><th class="num">Amount</th><th>Date</th>'
            f'<th class="num">Months seen</th></tr></thead><tbody>{rows}</tbody></table>'
            '<p class="caption">Amounts are the month\'s own charge, intentionally '
            'scoped to this report — they can differ from all-time averages.</p>')
    if not month_anomalies and not month_recurring:
        parts.append('<p class="empty">nothing to flag</p>')
    parts.append("</section>")
    return "".join(parts)


# ── monthly trend (dashboard parity — new in the deterministic renderer) ─────
def trend_chart(trend: list[dict], months: int = 12) -> str:
    """Grouped spend/income bars per month as inline SVG. `trend` is
    reports.monthly_trend's oldest-first list; the most recent `months` are
    shown. No per-bar numeric labels (axis months only), so no formatted-money
    text is re-derived here."""
    rows = trend[-months:]
    if not rows:
        return '<section class="trend"><p class="empty">no history yet</p></section>'
    w, h, pad = 720, 160, 18
    n = len(rows)
    peak = max([max(int(r["spend_cents"]), int(r["income_cents"])) for r in rows]) or 1
    group_w = (w - 2 * pad) / n
    bar_w = max(group_w * 0.32, 2)
    bars, labels = [], []
    for i, r in enumerate(rows):
        x0 = pad + i * group_w
        for j, (key, color) in enumerate((("spend_cents", _ACCENT),
                                          ("income_cents", _GOOD))):
            v = max(int(r[key]), 0)
            bh = round(v / peak * (h - 2 * pad), 1)
            x = round(x0 + group_w * 0.15 + j * bar_w, 1)
            bars.append(f'<rect x="{x}" y="{round(h - pad - bh, 1)}" '
                        f'width="{round(bar_w, 1)}" height="{bh}" fill="{color}"/>')
        if n <= 12 or i % 2 == 0:
            labels.append(f'<text x="{round(x0 + group_w / 2, 1)}" y="{h - 4}" '
                          f'text-anchor="middle" class="axis">{_esc(r["month"][2:])}</text>')
    legend = (f'<span class="key"><span class="swatch" style="background:{_ACCENT}"></span>'
              f'Spent</span><span class="key"><span class="swatch" '
              f'style="background:{_GOOD}"></span>Income</span>')
    return ('<section class="trend"><h3>📈 Trend</h3>'
            f'<div class="legend">{legend}</div>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="monthly spend and income">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" '
            f'stroke="{_TRACK}" stroke-width="1"/>'
            + "".join(bars) + "".join(labels) + "</svg></section>")
