"""A standalone Amazon report — what two years of order detail actually shows.

Deliberately NOT part of the monthly budget PDF. That page is a fixed
one-pager about a month; six hundred product titles would swamp it while
answering a question its reader may not have asked. This is the follow-up,
rendered in the same PRESS brand so the two read as one publication.

**On the "kind" grouping.** There is no product category in the source — only
titles, ASINs and sellers — so `KINDS` below is a keyword heuristic, and the
report says so on its face. It is kept visible and auditable rather than hidden
behind a model call: a wrong bucket should be something a reader can spot and
correct, not something they have to trust.
"""
from __future__ import annotations

import html as _html
import sqlite3
from collections import Counter, defaultdict
from datetime import date

from ...agent.render import money
from ...report import brand
from .match import MERCHANT_LIKE, horizon

#: Keyword → bucket, first match wins. Order matters: more specific patterns
#: come first so "canvas board" lands in art rather than office supplies.
KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("School & office", ("binder", "calculator", "notebook", "planner", "pencil",
                         "backpack", "lunch bag", "index card", "stapler",
                         "folder", "printer paper", "sharpie")),
    ("Sports & rec", ("pickleball", "volleyball", "basketball", "soccer", "golf",
                      "tennis", "racket", "rebounder", "elbow sleeve", "knee pad",
                      "mouthguard", "cleat", "yoga", "dumbbell", "bike")),
    ("Outdoor & patio", ("patio", "umbrella", "cantilever", "outdoor", "cooler",
                         "beach", "camping", "tent", "grill", "lawn", "garden hose",
                         "planter")),
    ("Kids & toys", ("kids", " toy", "toys", "lego", "puzzle", "stem ",
                     "activity book", "board game", "chess", "fidget", "doll",
                     "craft kit", "airplane", "hidden pictures")),
    ("Art & hobby", ("canvas", "paint", "coloring", "yarn", "sketch", "marker",
                     "bead", "sticker", "glitter", "drum", "instrument")),
    ("Pets & backyard", ("bird", "peanut", "mealworm", "seed", "feeder", "dog",
                         "cat ", "pet ", "bedding", "aquarium", "chicken",
                         "dewormer", "leash", "litter")),
    ("Personal care", ("acne", "skin", "shampoo", "lotion", "nail", "hair",
                       "razor", "toothbrush", "deodorant", "cotton round",
                       "makeup", "serum", "sunscreen", "sanitizer", "perfume",
                       "conditioner", "moisturi")),
    ("Clothing & footwear", ("shirt", "sock", "shoe", "jacket", "hat", "glove",
                             "legging", "dress", "pajama", "slipper", "boot",
                             "sandal", "vest", "hoodie", "sweater", "pants",
                             "shorts", "swimsuit", "underwear", "bra ")),
    ("Storage & organisation", ("storage", "bin", "tote", "container", "organizer",
                                "shelf", "rack", "basket", "drawer", "hanger")),
    ("Home & decor", ("curtain", "rug", "frame", "lamp", "pillow", "blanket",
                      "sheet set", "towel", "backdrop", "wall art", "candle",
                      "mirror", "vase")),
    ("Kitchen & baking", ("scoop", "whisk", "zester", "grater", "spatula",
                          "baking", "cookie", "measuring", "mixing bowl",
                          "food storage", "mug", "utensil", "cutting board",
                          "skillet", "blender")),
    ("Home & repair", ("glue", "adhesive", "cement", "screw", "tool", "battery",
                       "light bulb", "hook", "tape", "filter", "cleaner",
                       "caulk", "sandpaper", "wrench", "drill")),
    ("Tech & cables", ("cable", "charger", "usb", "hdmi", "adapter", "headphone",
                       "earbud", "mouse", "keyboard", "drive", "router",
                       "photo printer", "camera", "speaker", "tablet")),
    ("Food & drink", ("candy", "snack", "coffee", "tea ", "protein", "cereal",
                      "sauce", "spice", "granola", "chocolate", "vitamin",
                      "supplement")),
]


def classify(title: str | None) -> str:
    t = (title or "").lower()
    for kind, pats in KINDS:
        if any(p in t for p in pats):
            return kind
    return "Uncategorised"


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

    # One row per ORDER first. A split-shipment order matches several charges,
    # and joining items through each of them counts the same product once per
    # shipment — a silent double-count that inflates every total downstream.
    rows = [dict(r) for r in conn.execute(
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
                   i.asin, i.title, i.seller, i.condition,
                   COALESCE(i.quantity,1) AS qty,
                   i.unit_price_cents * COALESCE(i.quantity,1) AS line_cents
              FROM matched_orders mo
              JOIN amazon_items i ON i.order_number = mo.order_number""", params)]

    charges = conn.execute(
        f"""SELECT COUNT(*) n, COALESCE(-SUM(t.amount_cents),0) c
              FROM amazon_matches m JOIN transactions t ON t.txn_id = m.txn_id
             WHERE 1=1{where}""", params).fetchone()

    # Coverage must be scoped to the SAME window as the rest of the page.
    # Reporting the all-time figure on a two-month report describes a
    # different document than the one the reader is holding.
    like = " OR ".join("t.merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    scoped = conn.execute(
        f"""SELECT COUNT(*) n, COALESCE(-SUM(t.amount_cents),0) c
              FROM transactions t
             WHERE t.status='posted' AND t.amount_cents < 0
               AND ({like}){where}""", (*MERCHANT_LIKE, *params)).fetchone()

    by_kind: Counter = Counter()
    by_month: Counter = Counter()
    by_seller: Counter = Counter()
    per_asin: dict = defaultdict(lambda: {"n": 0, "cents": 0, "title": ""})
    for r in rows:
        r["kind"] = classify(r["title"])
        by_kind[r["kind"]] += r["line_cents"]
        by_month[r["posted_date"][:7]] += r["line_cents"]
        if r["seller"]:
            by_seller[r["seller"]] += r["line_cents"]
        a = per_asin[r["asin"] or r["title"]]
        a["n"] += 1
        a["cents"] += r["line_cents"]
        a["title"] = r["title"] or "—"

    repeats = sorted((v for v in per_asin.values() if v["n"] > 1),
                     key=lambda v: -v["cents"])
    biggest = sorted(rows, key=lambda r: -r["line_cents"])[:12]

    return {
        "rows": rows,
        "line_total": sum(r["line_cents"] for r in rows),
        "charge_total": int(charges["c"]), "charge_count": int(charges["n"]),
        "orders": len({r["order_number"] for r in rows}),
        "items": len(rows), "asins": len(per_asin),
        "span": (min((r["posted_date"] for r in rows), default="—"),
                 max((r["posted_date"] for r in rows), default="—")),
        "by_kind": by_kind.most_common(),
        "by_month": sorted(by_month.items()),
        "by_seller": by_seller.most_common(10),
        "repeats": repeats[:12],
        "biggest": biggest,
        "scoped_total_cents": int(scoped["c"]), "scoped_charges": int(scoped["n"]),
        "scoped_pct": (round(int(charges["c"]) / int(scoped["c"]) * 100, 1)
                       if scoped["c"] else 0.0),
        "horizon": horizon(conn), "is_scoped": bool(since or until),
    }


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _safe(stem: str) -> str:
    """Filename stem reduced to characters that cannot escape the reports dir.
    `since`/`until` reach this from the command line."""
    return "".join(c for c in stem if c.isalnum() or c in "-_") or "amazon"


def _clip(text: str | None, n: int) -> str:
    """Truncate at a word boundary with an ellipsis. A hard slice cuts titles
    mid-word ("Memory Foam Floor Chair – Ideal f"), which reads as corruption
    rather than as an abbreviation."""
    t = (text or "—").strip()
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return (cut or t[:n]).rstrip(" ,-–—") + "…"


def _bars(pairs: list[tuple[str, int]], *, accent_last: bool = False) -> str:
    """Horizontal ink bars. Same visual grammar as the monthly report: label,
    ink bar, value — no gridlines, no fills, one accent at most."""
    if not pairs:
        return '<p class="empty">nothing to show</p>'
    peak = max(v for _, v in pairs) or 1
    out = []
    for i, (label, v) in enumerate(pairs):
        w = round(v / peak * 100, 2)
        cls = " last" if (accent_last and i == len(pairs) - 1) else ""
        out.append(
            f'<div class="sb-row"><div class="sb-label">{_esc(label)}</div>'
            f'<div class="sb-track"><span class="sb-fill{cls}" style="width:{w}%">'
            f'</span></div><div class="sb-value">{_esc(money(v))}</div></div>')
    return "".join(out)


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


def build_html(d: dict, theme: dict) -> str:
    lo, hi = d["span"]
    hz = d["horizon"]
    ident = theme["identity"]

    stats = "".join(
        f'<div class="stat{" focal" if focal else ""}"><div class="value">{_esc(v)}</div>'
        f'<div class="label">{_esc(k)}</div></div>'
        for k, v, focal in [
            ("Spent", money(d["charge_total"]), True),
            ("Orders", f'{d["orders"]:,}', False),
            ("Items", f'{d["items"]:,}', False),
            ("Distinct products", f'{d["asins"]:,}', False),
        ])

    biggest_rows = [[r["posted_date"], money(r["line_cents"]),
                     _clip(r["title"], 60)] for r in d["biggest"]]
    repeat_rows = [[str(v["n"]) + "x", money(v["cents"]), _clip(v["title"], 58)]
                   for v in d["repeats"]]
    seller_rows = [[_clip(s, 38), money(v)] for s, v in d["by_seller"]]

    note = ""
    if d["line_total"] != d["charge_total"]:
        note = (f'<p class="caption">Items list at {money(d["line_total"])}; '
                f'{money(d["charge_total"])} was charged. Item prices are before '
                f'discounts, promotions and gift cards, and tax pushes the other '
                f'way — the two are different figures, not an error.</p>')

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<title>Amazon purchases</title>'
        f'<style>{brand.stylesheet(theme)}{_EXTRA_CSS}</style></head><body><main>'
        '<header class="masthead"><div class="masthead-row">'
        f'<span class="stamp">{_esc(ident["stamp"])}</span>'
        f'<span class="eyebrow">LOCAL BUDGET · AMAZON PURCHASES · '
        f'{_esc(date.today().isoformat())}</span>'
        f'<span class="byline">{_esc(ident["byline"])}</span></div>'
        f'<h1>Amazon</h1>'
        f'<p class="standfirst">{_esc(lo)} to {_esc(hi)} — every order that '
        f'reconciled to a charge on the statement, itemised.</p></header>'
        f'<section class="stat-strip">{stats}</section>'

        '<h3 class="block-title">What it was</h3>'
        f'<section>{_bars(d["by_kind"])}</section>'
        '<p class="caption">Groups are inferred from product titles by keyword — '
        'the source carries no product category. Treat them as a reading of the '
        'data, not a fact in it.</p>'

        '<h3 class="block-title">When</h3>'
        f'<section>{_bars(d["by_month"], accent_last=True)}</section>'

        '<h3 class="block-title">Biggest single items</h3>'
        f'<section>{_table(["Date", "Amount", "Item"], biggest_rows, {1})}</section>'

        '<h3 class="block-title">Bought more than once</h3>'
        f'<section>{_table(["Times", "Total", "Item"], repeat_rows, {0, 1}, "nothing was bought more than once in this period")}</section>'

        '<h3 class="block-title">Top sellers</h3>'
        f'<section>{_table(["Seller", "Total"], seller_rows, {1})}</section>'
        f'{note}'

        f'<footer class="provenance">'
        f'{d["charge_count"]} of {d["scoped_charges"]} Amazon charges in this '
        f'period reconciled · {d["scoped_pct"]}% of the '
        f'{money(d["scoped_total_cents"])} charged has item detail'
        # The horizon is a property of the whole dataset, so it only belongs on
        # an all-history report — on a scoped one it describes something else.
        + ((f' · item detail reaches back to {_esc(hz["earliest"])}'
            if hz["earliest"] else '')
           + (f'; {hz["pre_count"]} older charges predate any transaction record'
              if hz["has_backlog"] else '')
           if not d["is_scoped"] else '')
        + '</footer></main></body></html>')


#: Only what the monthly report's stylesheet does not already provide.
#:
#: The two pagination rules are the difference between five ragged pages and
#: three tight ones. The monthly report keeps whole sections together because
#: its sections are short; here they are long tables, so forbidding a split
#: shunts an entire table to the next page and leaves half a page blank.
_EXTRA_CSS = """
div.sb-row { grid-template-columns: 13rem 1fr 7rem; }
span.sb-fill.last { background: var(--accent); }
table.data td:last-child { width: 55%; }
h3.block-title { padding-top: 2.2rem; }

/* A heading must never be the last thing on a page. */
h3.block-title { break-after: avoid; page-break-after: avoid; }
/* Long tables may split; individual rows may not. */
section { break-inside: auto; }
table.data tr, div.sb-row { break-inside: avoid; }
"""


def render(since: str | None = None, until: str | None = None,
           out_dir=None) -> dict:
    """Render the PDF. Returns {"ok": True, "path": str, "items": n}."""
    from ... import db, paths
    from ...report.pdf import render_pdf

    with db.connect() as conn:
        d = gather(conn, since, until)
    if not d["rows"]:
        raise ValueError("no reconciled Amazon items in that range — "
                         "run `budget amazon backfill` first")
    html = build_html(d, brand.load_theme())
    base = (out_dir or paths.reports_dir()).resolve()
    # The filename carries the scope. With a fixed name, rendering a two-month
    # view silently overwrites the all-history one — same path, wholly different
    # document, and no way to tell them apart afterwards.
    lo, hi = d["span"]
    stem = "amazon-purchases" if not (since or until) else f"amazon-{lo}_{hi}"
    out = (base / f"{_safe(stem)}.pdf").resolve()
    if not out.is_relative_to(base):
        raise ValueError("invalid output path")
    render_pdf(html, out)
    return {"ok": True, "path": str(out), "items": d["items"],
            "orders": d["orders"], "spent_cents": d["charge_total"]}
