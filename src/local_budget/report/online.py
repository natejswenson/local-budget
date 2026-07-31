"""Online spend — Amazon and Walmart.com together, itemised into budget lines.

**The question this page exists to answer.** The ledger categorises MERCHANTS,
not items. Two manual rules carry nearly fifty thousand dollars between them:

    WALMART.COM  →  Groceries      every charge, toilet paper included
    AMAZON       →  Shopping       every charge, vitamins included

Both are reasonable rules and both are wrong about a large minority of what was
actually bought. The item detail behind those charges can say what the rules
cannot, but only if it is grouped into the same vocabulary the budget is set in
— which is what `connectors/kinds.py` now does. So this report's spine is a
comparison: **what the ledger says, beside what the items say.**

**Why it lives here and not under a connector.** It belongs to neither source.
`connectors/amazon/report.py` and `connectors/walmart/report.py` each answer
"what did I buy from this merchant"; this one answers "what did I buy online",
which is a question about the household rather than about a vendor. Putting it
in either connector would make the other one's data a guest in someone else's
module.

**Food is one bar, on purpose.** Splitting a grocery run into produce, dairy and
snacks is detail no budget decision turns on. The report spends its length on
the remainder instead — there is a whole section for non-food alone — because
that is the part the ledger currently gets wrong and the part worth acting on.

**What this page will not do.** It never writes a category to a transaction.
Grouping product titles by keyword is a reading of the data; assigning a
category is a judgment a human confirms. The footer says so, and the numbers are
presented as a description of the basket rather than as a correction to the
ledger.
"""
from __future__ import annotations

import html as _html
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from ..agent.render import money
from ..connectors.kinds import classify, unhoused
from . import brand

#: The bucket every food item lands in. Named once here because three sections
#: are defined relative to it — the non-food chart, the headline figure, and the
#: caption — and a typo in any of them would silently produce a different claim.
FOOD = "Groceries"

#: How far back the report looks when nothing is asked for. A year is the window
#: a household actually reasons about: it covers every season and every annual
#: purchase once, and it keeps the month chart at twelve columns, which reads as
#: a shape. Two years of history was the honest default and the wrong one — it
#: doubled the page count and made the trend a list.
DEFAULT_MONTHS = 12


def default_since(months: int = DEFAULT_MONTHS, today: date | None = None) -> str:
    """The first day of the month `months - 1` back.

    Anchored to a month boundary rather than to "today minus 365 days", so the
    first and last columns of the chart are whole months. A part-month at either
    end reads as a collapse in spending that never happened.
    """
    d = today or date.today()
    total = d.year * 12 + (d.month - 1) - (months - 1)
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01"

#: Bank-side merchant patterns for ONLINE spend, per source. Walmart's in-store
#: strings are deliberately absent: this is an online report, and `WM SUPERC%`
#: is a person standing in a shop.
AMAZON_LIKE = ("AMAZON%", "AMZN%")
WALMART_ONLINE_LIKE = ("WALMART.COM%", "WALMART.CO%")
ONLINE_LIKE = AMAZON_LIKE + WALMART_ONLINE_LIKE


def _rows(conn: sqlite3.Connection, where: str, params: list) -> list[dict]:
    """Item lines from both sources, in one shape.

    Each half keeps the `matched_orders` CTE its own connector's report uses:
    an order that settled as five bank charges matches five rows, and joining
    items through each of them counts the same product five times. Collapsing
    to one row per order first is what prevents that.
    """
    amazon = [dict(r) | {"source": "Amazon"} for r in conn.execute(
        f"""WITH matched_orders AS (
                SELECT a.order_number,
                       MIN(t.posted_date) AS posted_date,
                       MIN(t.txn_id)      AS txn_id
                  FROM amazon_matches m
                  JOIN amazon_transactions a ON a.amazon_txn_id = m.amazon_txn_id
                  JOIN transactions t        ON t.txn_id = m.txn_id
                 WHERE a.order_number IS NOT NULL{where}
              GROUP BY a.order_number)
            SELECT mo.posted_date, mo.txn_id, mo.order_number,
                   i.title, i.seller, COALESCE(i.quantity,1) AS qty,
                   i.unit_price_cents * COALESCE(i.quantity,1) AS line_cents,
                   i.asin AS product_id,
                   COALESCE(t2.category,'Uncategorized') AS ledger_category
              FROM matched_orders mo
              JOIN amazon_items i  ON i.order_number = mo.order_number
              JOIN transactions t2 ON t2.txn_id = mo.txn_id""", params)]

    walmart = [dict(r) | {"source": "Walmart.com"} for r in conn.execute(
        f"""WITH matched_orders AS (
                SELECT m.order_number,
                       MIN(t.posted_date) AS posted_date,
                       MIN(t.txn_id)      AS txn_id
                  FROM walmart_matches m
                  JOIN transactions t ON t.txn_id = m.txn_id
                 WHERE 1=1{where}
              GROUP BY m.order_number)
            SELECT mo.posted_date, mo.txn_id, mo.order_number,
                   i.title, i.seller, COALESCE(i.quantity,1) AS qty,
                   i.line_price_cents AS line_cents, i.product_id,
                   COALESCE(t2.category,'Uncategorized') AS ledger_category
              FROM matched_orders mo
              JOIN walmart_orders o ON o.order_number = mo.order_number
              JOIN walmart_items i  ON i.order_number = mo.order_number
              JOIN transactions t2  ON t2.txn_id = mo.txn_id
             -- Online only: this report's subject. An in-store receipt is a
             -- different behaviour and posts under a different merchant.
             WHERE o.channel = 'online'
               AND (i.status IS NULL OR LOWER(i.status) NOT IN
                    ('canceled','cancelled','unavailable'))""", params)]

    return amazon + walmart


def gather(conn: sqlite3.Connection, since: str | None = None,
           until: str | None = None) -> dict:
    """Everything the report renders, computed once."""
    where, params = "", []
    if since:
        where += " AND t.posted_date >= ?"
        params.append(since)
    if until:
        where += " AND t.posted_date <= ?"
        params.append(until)

    rows = _rows(conn, where, params)
    for r in rows:
        r["line_cents"] = int(r["line_cents"] or 0)
        r["kind"] = classify(r["title"])
        r["unhoused"] = unhoused(r["title"])

    by_kind: Counter = Counter()
    by_month: Counter = Counter()
    by_source: Counter = Counter()
    # Food vs not-food, sliced three ways. One comparison repeated across
    # category, shop and time is the document's whole visual language — three
    # different chart types answering three different questions would be more
    # to look at and less to see.
    split_month: dict = defaultdict(lambda: [0, 0])
    split_source: dict = defaultdict(lambda: [0, 0])
    by_unhoused: Counter = Counter()
    unhoused_lines: Counter = Counter()
    per_product: dict = defaultdict(lambda: {"n": 0, "cents": 0, "title": ""})
    for r in rows:
        by_kind[r["kind"]] += r["line_cents"]
        by_month[r["posted_date"][:7]] += r["line_cents"]
        by_source[r["source"]] += r["line_cents"]
        slot = 0 if r["kind"] == FOOD else 1
        split_month[r["posted_date"][:7]][slot] += r["line_cents"]
        split_source[r["source"]][slot] += r["line_cents"]
        if r["unhoused"]:
            by_unhoused[r["unhoused"]] += r["line_cents"]
            unhoused_lines[r["unhoused"]] += 1
        a = per_product[(r["source"], r["product_id"] or r["title"])]
        a["n"] += 1
        a["cents"] += r["line_cents"]
        a["title"] = r["title"] or "—"

    # What the LEDGER says about the same charges — the other half of the
    # comparison. Read from the transactions themselves rather than from the
    # merchant rules, so a recategorised charge shows up here immediately.
    like = " OR ".join("t.merchant_norm LIKE ?" for _ in ONLINE_LIKE)
    ledger = Counter()
    charge_total = charge_count = 0
    for r in conn.execute(
            f"""SELECT COALESCE(t.category,'Uncategorized') cat,
                       COUNT(*) n, -SUM(t.amount_cents) c
                  FROM transactions t
                 WHERE t.status='posted' AND t.amount_cents < 0
                   AND ({like}){where}
              GROUP BY cat""", (*ONLINE_LIKE, *params)):
        ledger[r["cat"]] += int(r["c"])
        charge_total += int(r["c"])
        charge_count += int(r["n"])

    line_total = sum(r["line_cents"] for r in rows)
    non_food = [(k, v) for k, v in by_kind.most_common() if k != FOOD]

    # Food vs non-food WITHIN the charges the ledger already calls Groceries.
    # Scoped deliberately: the headline is a claim about that category, and the
    # all-sources ratio is a different number about a different population.
    food_ledger_split = {"food": 0, "non_food": 0}
    for r in rows:
        if r["ledger_category"] != FOOD:
            continue
        key = "food" if r["kind"] == FOOD else "non_food"
        food_ledger_split[key] += r["line_cents"]

    return {
        "rows": rows,
        "line_total": line_total,
        "food_cents": by_kind.get(FOOD, 0),
        "non_food_cents": sum(v for _, v in non_food),
        "food_ledger_split": food_ledger_split,
        "orders": len({(r["source"], r["order_number"]) for r in rows}),
        "items": len(rows), "products": len(per_product),
        "span": (min((r["posted_date"] for r in rows), default="—"),
                 max((r["posted_date"] for r in rows), default="—")),
        "by_kind": by_kind.most_common(),
        "non_food": non_food,
        "by_month": sorted(by_month.items()),
        "by_source": by_source.most_common(),
        "split_month": [(m, *split_month[m]) for m in sorted(split_month)],
        "split_source": [(s, *split_source[s])
                         for s, _ in by_source.most_common()],
        "by_unhoused": by_unhoused.most_common(),
        "unhoused_lines": dict(unhoused_lines),
        "ledger": ledger.most_common(),
        "ledger_total": charge_total, "charge_count": charge_count,
        # Eight, not twelve. This is a ranked chart answering "what dominates",
        # and a long tail of near-equal bars is a table wearing a chart's
        # clothes. It also keeps the document to two pages.
        "repeats": sorted((v for v in per_product.values() if v["n"] > 1),
                          key=lambda v: -v["cents"])[:7],
        "uncategorised_cents": by_kind.get("Uncategorized", 0),
        "is_scoped": bool(since or until),
    }


# ── presentation ─────────────────────────────────────────────────────────────
def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _safe(stem: str) -> str:
    """Filename stem reduced to characters that cannot escape the reports dir.
    `since`/`until` reach this from the command line."""
    return "".join(c for c in stem if c.isalnum() or c in "-_") or "online"


def _clip(text: str | None, n: int) -> str:
    """Truncate at a word boundary. A hard slice cuts titles mid-word, which
    reads as corruption rather than as an abbreviation."""
    t = (text or "—").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return (cut or t[:n]).rstrip(" ,-–—") + "…"


def _bars(pairs: list[tuple[str, int]], *, peak: int | None = None) -> str:
    """Horizontal ink bars — label, bar, value. No gridlines, no fills.

    `peak` can be pinned so two charts share a scale. The ledger/items
    comparison needs that: drawn to their own maxima, a $27k bar and a $22k bar
    are the same length and the comparison silently says nothing.
    """
    if not pairs:
        return '<p class="empty">nothing to show</p>'
    top = peak or max(v for _, v in pairs) or 1
    out = []
    for label, v in pairs:
        w = round(v / top * 100, 2)
        out.append(
            f'<div class="sb-row"><div class="sb-label">{_esc(label)}</div>'
            f'<div class="sb-track"><span class="sb-fill" style="width:{w}%">'
            f'</span></div><div class="sb-value">{_esc(money(v))}</div></div>')
    return "".join(out)


#: Brand tokens, emitted as var(...) so the fragments stay theme-independent —
#: the same discipline `report/charts.py` follows.
_INK = "var(--ink)"
_INK_MID = "var(--ink-mid)"
_DIM = "var(--dim)"


def _legend() -> str:
    """One legend, reused by every chart on the page. The document makes a
    single comparison — food against everything else — so it should only ever
    have to teach the reader one key."""
    return ('<div class="legend">'
            f'<span class="key"><i style="background:{_INK}"></i>Food</span>'
            f'<span class="key"><i style="background:{_INK_MID}"></i>'
            'Everything else</span></div>')


def _stacked(rows: list[tuple[str, int, int]]) -> str:
    """Horizontal food/not-food bars — label, stacked bar, total, food share.

    Drawn to a shared maximum rather than each to 100%, so bar LENGTH still
    reads as money. Normalising each row would make a $200 month and a $2,000
    month the same size and turn a spending chart into a ratio chart.
    """
    if not rows:
        return '<p class="empty">nothing to show</p>'
    peak = max((f + n) for _, f, n in rows) or 1
    out = []
    for label, food, non_food in rows:
        total = food + non_food
        fw = round(food / peak * 100, 2)
        nw = round(non_food / peak * 100, 2)
        pct = round(food / total * 100) if total else 0
        out.append(
            f'<div class="st-row"><div class="sb-label">{_esc(label)}</div>'
            f'<div class="sb-track">'
            f'<span class="st-food" style="width:{fw}%"></span>'
            f'<span class="st-rest" style="width:{nw}%"></span></div>'
            f'<div class="sb-value">{_esc(money(total))}</div>'
            f'<div class="st-pct">{pct}% food</div></div>')
    return "".join(out)


def _month_chart(rows: list[tuple[str, int, int]]) -> str:
    """Monthly spend as stacked columns — food on the bottom, the rest above.

    An SVG rather than another row of bars: twelve months read as a shape when
    they are columns on a shared baseline, and as a list when they are rows.
    The trend is the point, and a list does not have one.
    """
    if not rows:
        return '<p class="empty">no history yet</p>'
    w, h, pad = 720, 152, 20
    n = len(rows)
    peak = max((f + nf) for _, f, nf in rows) or 1
    slot = (w - 2 * pad) / n
    bar_w = max(slot * 0.56, 2)
    body, labels = [], []
    for i, (month, food, non_food) in enumerate(rows):
        x = round(pad + i * slot + (slot - bar_w) / 2, 1)
        y = h - pad
        # Food first, so it sits on the baseline: it is the stable floor of the
        # basket and the varying part should be read against it, not balanced on
        # top of it. Same order as the horizontal bars, where food is the left
        # segment — one stacking order across the document.
        for v, fill in ((food, _INK), (non_food, _INK_MID)):
            bh = round(v / peak * (h - 2 * pad - 10), 1)
            y -= bh
            if bh > 0:
                body.append(f'<rect x="{x}" y="{round(y, 1)}" '
                            f'width="{round(bar_w, 1)}" height="{bh}" fill="{fill}"/>')
        # The latest month's LABEL takes the accent, not its bar: the bars carry
        # the food/not-food key, and a third fill in them would read as a third
        # category. This still lets a reader place the document in the series.
        cls = "axis now" if i == n - 1 else "axis"
        labels.append(f'<text x="{round(x + bar_w / 2, 1)}" y="{h - 6}" '
                      f'text-anchor="middle" class="{cls}">'
                      f'{_esc(month[2:])}</text>')
    return (f'{_legend()}'
            f'<svg viewBox="0 0 {w} {h}" role="img" '
            f'aria-label="online spend by month, food and non-food">'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" '
            f'stroke="{_DIM}" stroke-width="1"/>'
            + "".join(body) + "".join(labels) + "</svg>")


def _block(title: str, body: str, caption: str = "", *, cls: str = "") -> str:
    """A heading, a chart and its caption as ONE unbreakable unit.

    Bound together because a caption explains the thing above it: split across a
    page turn it opens the next page attached to nothing. Chromium ignores
    `break-before: avoid` on the caption itself, so the grouping has to be
    structural. Only short chart blocks use this — see the CSS.
    """
    cap = f'<p class="caption">{_esc(caption)}</p>' if caption else ""
    section_cls = f' class="{cls}"' if cls else ""
    return (f'<div class="block"><h3 class="block-title">{title}</h3>'
            f'<section{section_cls}>{body}</section>{cap}</div>')


def _table(headers: list[str], rows: list[list[str]], nums: set[int],
           empty: str = "nothing to show") -> str:
    if not rows:
        return f'<p class="empty">{_esc(empty)}</p>'
    th = "".join(f'<th{" class=\"num\"" if i in nums else ""}>{_esc(h)}</th>'
                 for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(
            f'<td{" class=\"num\"" if i in nums else ""}>{_esc(c)}</td>'
            for i, c in enumerate(r)) + "</tr>" for r in rows)
    return f'<table class="data"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>'


def headline(d: dict) -> str:
    """The one sentence the report exists to be able to write.

    The share is computed **within the grocery-labelled charges only**, never
    across the whole basket. Blending the two was this report's first bug: most
    Amazon spend is non-food, so an all-sources ratio applied to the Groceries
    line inflated the answer by thousands of dollars and stated it confidently.
    A ratio is only meaningful against the population it was measured in.

    Guarded rather than assumed: with no grocery-labelled charge, or no items
    behind one, the comparison has nothing to say.
    """
    ledger_food = dict(d["ledger"]).get(FOOD, 0)
    within = d["food_ledger_split"]
    basket = within["food"] + within["non_food"]
    if not ledger_food or not basket:
        return ""
    # Scaled by the ledger figure rather than summed from the lines: item prices
    # are before discounts and tax, so the PROPORTION carries between the two
    # and the absolute does not.
    return (f"{money(round(ledger_food * within['non_food'] / basket))} of what "
            f"the ledger files as {FOOD} did not buy food.")


def build_html(d: dict, theme: dict) -> str:
    lo, hi = d["span"]
    ident = theme["identity"]

    stats = "".join(
        f'<div class="stat{" focal" if focal else ""}"><div class="value">{_esc(v)}</div>'
        f'<div class="label">{_esc(k)}</div></div>'
        for k, v, focal in [
            ("Spent online", money(d["ledger_total"]), True),
            ("Orders", f'{d["orders"]:,}', False),
            ("Items", f'{d["items"]:,}', False),
            ("Distinct products", f'{d["products"]:,}', False),
        ])

    # One scale across both columns, or the comparison lies. See `_bars`.
    peak = max([v for _, v in d["ledger"]] + [v for _, v in d["by_kind"]] + [1])
    # The column headers ride on `h3.block-title` so their type comes from the
    # brand stylesheet rather than being restyled here.
    compare = (
        '<div class="compare">'
        '<div class="col"><h3 class="block-title col-title">The ledger says</h3>'
        f'{_bars(d["ledger"], peak=peak)}'
        f'<p class="caption">{d["charge_count"]} charges, categorised by '
        f'merchant.</p></div>'
        '<div class="col"><h3 class="block-title col-title">The items say</h3>'
        f'{_bars(d["by_kind"], peak=peak)}'
        f'<p class="caption">{d["items"]:,} product lines, grouped by title.</p>'
        '</div></div>')

    head = headline(d)
    head_html = f'<p class="caption headline">{_esc(head)}</p>' if head else ""

    # The repeat buys as a chart rather than a table. What matters is which few
    # products dominate, and a ranked bar says that at a glance where a column
    # of figures makes the reader do the comparing.
    repeat_bars = [(f'{_clip(v["title"], 44)} · {v["n"]}x', v["cents"])
                   for v in d["repeats"]]

    unhoused_block = ""
    if d["by_unhoused"]:
        # Item COUNT, not a sentence. An explanatory third column ran into the
        # figure beside it and read as one broken line; the explanation belongs
        # in the caption, where prose belongs.
        rows = [[name, money(cents), f'{d["unhoused_lines"].get(name, 0):,}']
                for name, cents in d["by_unhoused"]]
        unhoused_block = (
            '<h3 class="block-title">No home in your budget</h3>'
            f'<section>{_table(["Suggested category", "Spend", "Items"], rows, {1, 2})}</section>'
            '<p class="caption">Spend with no category to file it under lands '
            'in whatever bucket the merchant rule picked, where it cannot be '
            'budgeted or tracked. Adding a category would make it visible — '
            'this report suggests, it does not add one.</p>')
    # Bound to the footer below: a footer that breaks away lands on a page of
    # its own carrying three lines of provenance and nothing else.

    food_share = (round(d["food_cents"] / d["line_total"] * 100)
                  if d["line_total"] else 0)

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<title>Online spend</title>'
        f'<style>{brand.stylesheet(theme)}{template_css()}</style></head><body><main>'
        '<header class="masthead"><div class="masthead-row">'
        f'<span class="stamp">{_esc(ident["stamp"])}</span>'
        f'<span class="eyebrow">LOCAL BUDGET · ONLINE SPEND · '
        f'{_esc(date.today().isoformat())}</span>'
        f'<span class="byline">{_esc(ident["byline"])}</span></div>'
        '<h1>Online spend</h1>'
        f'<p class="standfirst">{_esc(lo)} to {_esc(hi)} — every Amazon and '
        f'Walmart.com order that reconciled to a charge, itemised and read '
        f'against the categories you budget in.</p></header>'
        # A note on the window, right under the masthead, so nobody reads a
        # twelve-month figure as an all-time one.
        f'<p class="caption window">Covering {len(d["split_month"])} months.</p>'
        f'<section class="stat-strip">{stats}</section>'

        '<h3 class="block-title">What the ledger sees, and what was in the box</h3>'
        f'<section>{compare}</section>'
        f'{head_html}'
        '<p class="caption">The ledger categorises the merchant, so every charge '
        'from one shop carries one label. The right-hand column groups the '
        'product titles behind those same charges — a reading of the data, not a '
        'fact in it, and not written back to any transaction. Both columns are '
        'drawn to one shared scale, so a bar on the left is directly comparable '
        'to a bar on the right.</p>'

        # Shops before categories: it is the coarser cut, and it is two rows
        # rather than seven — which is also what lets it sit in the space under
        # the comparison instead of stranding a half-empty page.
        + _block("Each shop, food against everything else",
                 _legend() + _stacked(d["split_source"]),
                 'The two shops are not doing the same job, which is why one '
                 'merchant rule cannot describe both.', cls="stacked")

        + _block("Everything that wasn’t food", _bars(d["non_food"]),
                 f'Food is {food_share}% of the basket and one line on the '
                 f'budget, so it is left as one bar. This is the rest — the '
                 f'part a merchant rule cannot categorise for you.')

        + _block("Month by month", _month_chart(d["split_month"]))

        + _block("What you buy again and again", _bars(repeat_bars),
                 'Repeat purchases by total spend across the period — the '
                 'handful of products a year of ordering actually consists of.',
                 cls="repeats")

        + '<div class="block tail">'
        + unhoused_block

        + f'<footer class="provenance">'
        f'Items list at {money(d["line_total"])} against {money(d["ledger_total"])} '
        f'charged — item prices are before discounts and tax, so the two are '
        f'different figures rather than an error'
        + (f' · {money(d["uncategorised_cents"])} of items matched no keyword '
           f'and are grouped as Uncategorized'
           if d["uncategorised_cents"] else '')
        + ' · groupings are inferred from product titles and are never written '
          'back to a transaction'
        + '</footer></div></main></body></html>')


#: The report's layout template, tracked in git and shipped with the package.
#: A real .css file rather than a Python string so it can be edited and
#: re-rendered without touching code.
TEMPLATE_CSS = Path(__file__).resolve().parent / "assets" / "online-report.css"


def template_css() -> str:
    """The layout stylesheet. A missing file is a PACKAGING error, not a
    cosmetic one — rendering without it silently produces a differently-laid-out
    document, so it raises rather than falling back to an empty string."""
    if not TEMPLATE_CSS.is_file():
        raise FileNotFoundError(
            f"missing report template: {TEMPLATE_CSS} — the package is "
            f"incomplete (it ships as package data alongside online.py)")
    return TEMPLATE_CSS.read_text(encoding="utf-8")


def render(since: str | None = None, until: str | None = None,
           out_dir=None, *, all_history: bool = False) -> dict:
    """Render the PDF. Returns {"ok": True, "path": str, ...}.

    Defaults to the last `DEFAULT_MONTHS`; `all_history=True` opts back into
    everything, and an explicit `since` wins over both.
    """
    from .. import db, paths
    from .pdf import render_pdf

    scoped = bool(since or until)
    if since is None and not all_history:
        since = default_since()

    with db.connect() as conn:
        d = gather(conn, since, until)
    if not d["rows"]:
        raise ValueError(
            "no reconciled online items in that range — run "
            "`budget walmart import`/`budget amazon sync` first")
    html = build_html(d, brand.load_theme())
    base = (out_dir or paths.reports_dir()).resolve()
    # The filename carries the scope. With a fixed name, a two-month view
    # silently overwrites the default one — same path, different document.
    lo, hi = d["span"]
    stem = ("online-spend-all" if all_history and not scoped else
            "online-spend" if not scoped else f"online-{lo}_{hi}")
    out = (base / f"{_safe(stem)}.pdf").resolve()
    if not out.is_relative_to(base):
        raise ValueError("invalid output path")
    render_pdf(html, out)
    return {"ok": True, "path": str(out), "items": d["items"],
            "orders": d["orders"], "spent_cents": d["ledger_total"],
            "food_cents": d["food_cents"], "non_food_cents": d["non_food_cents"]}
