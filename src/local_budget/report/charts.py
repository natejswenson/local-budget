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
from .brand import WARN

# Brand token names (declared on :root by brand.stylesheet). Fragments emit
# var(...) rather than hex so they stay theme-independent and the golden
# snapshots don't bake in one palette.
_INK = "var(--ink)"
_INK_MID = "var(--ink-mid)"
_DIM = "var(--dim)"


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _warn_mark() -> str:
    """The over-budget / negative-net mark. Carries U+FE0E via brand.WARN —
    see the note there; bare U+26A0 renders as a colored emoji in Chromium and
    would put a second loud color on a strictly one-accent page."""
    return f'<span class="warn">{WARN} </span>'


# ── recipe 1: stat row ────────────────────────────────────────────────────────
def stat_row(summary: dict) -> str:
    """Spent / [Savings] / Income / Net figures from reports.month_summary data.
    Net has no dedicated field, so it's computed here from the same integer
    cents the old recipe extracted — formatting still goes through money().
    Savings (floor-marked categories like Investments — money relocated, not
    spent) is its own figure, shown only when present, and is NOT subtracted
    from Net: Net = income - spent answers "did ordinary spending stay under
    income," independent of how much also went to savings that month.

    Spent is the document's ONE accent figure (`.focal`). A negative Net takes
    the same ⚠ mark the over-budget rows use rather than a red — one exception
    mark used consistently, instead of a second color."""
    spent = int(summary["spend_total_cents"])
    income = int(summary["income_cents"])
    savings = int(summary.get("savings_total_cents") or 0)
    net = income - spent
    # (label, value, focal, marked)
    tiles = [("Spent", money(spent), True, False)]
    if savings:
        tiles.append(("Savings", money(savings), False, False))
    tiles += [
        ("Income", money(income), False, False),
        ("Net", money(net), False, net < 0),
    ]
    cells = "".join(
        f'<div class="stat{" focal" if focal else ""}">'
        f'<div class="value">{_warn_mark() if marked else ""}{_esc(value)}</div>'
        f'<div class="label">{_esc(label)}</div></div>'
        for label, value, focal, marked in tiles)
    return f'<section class="stat-strip">{cells}</section>'


# ── recipe 2: spend vs budget ─────────────────────────────────────────────────
# The old _row_color() encoded budget-visualizer's recipe-2 traffic light
# (floor: `over` alone decides; ceiling: over → critical, pct >= 80 → warning,
# else good). Under the strict one-accent brand every bar is ink, so there is
# no color left to select and the function is gone. The classification it read
# has not changed meaning: `over` still drives the ⚠ mark, and the 80% warning
# tier is now carried by the budget tick's position against the bar, which was
# always the more precise signal anyway.


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
        width = round(max(spent, 0) / scale * 100, 2)   # bar floors at zero
        warn = _warn_mark() if c.get("over") else ""
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
            f'<span class="sb-fill" style="width:{width}%"></span>'
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
            '<h3 class="block-title">Unusual charges</h3>'
            f'<table class="data"><thead><tr><th>Date</th><th>Merchant</th>'
            f'<th class="num">Amount</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>')
    if month_recurring:
        rows = "".join(
            f'<tr><td>{_esc(r["merchant"])}</td>'
            f'<td class="num">{_esc(money(int(r["amount_cents"])))}</td>'
            f'<td>{_esc(r["posted_date"])}</td><td class="num">{_esc(r["months"])}</td></tr>'
            for r in month_recurring)
        parts.append(
            f'<h3 class="block-title">Subscriptions &amp; recurring bills in {_esc(month)}</h3>'
            f'<table class="data"><thead><tr><th>Merchant</th><th class="num">Amount</th>'
            f'<th>Date</th>'
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
        # Ink for spend (the subject), mid-ink for income. Both clear 3:1 on
        # cream; the legend below carries identity, never the hue alone.
        for j, (key, color) in enumerate((("spend_cents", _INK),
                                          ("income_cents", _INK_MID))):
            v = max(int(r[key]), 0)
            bh = round(v / peak * (h - 2 * pad), 1)
            x = round(x0 + group_w * 0.15 + j * bar_w, 1)
            bars.append(f'<rect x="{x}" y="{round(h - pad - bh, 1)}" '
                        f'width="{round(bar_w, 1)}" height="{bh}" fill="{color}"/>')
        if n <= 12 or i % 2 == 0:
            labels.append(f'<text x="{round(x0 + group_w / 2, 1)}" y="{h - 4}" '
                          f'text-anchor="middle" class="axis">{_esc(r["month"][2:])}</text>')
    legend = (f'<span class="key"><i style="background:{_INK}"></i>'
              f'Spent</span><span class="key">'
              f'<i style="background:{_INK_MID}"></i>Income</span>')
    return ('<section class="trend"><h3 class="block-title">Trend</h3>'
            f'<div class="legend">{legend}</div>'
            f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="monthly spend and income">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" '
            f'stroke="{_DIM}" stroke-width="1"/>'
            + "".join(bars) + "".join(labels) + "</svg></section>")
