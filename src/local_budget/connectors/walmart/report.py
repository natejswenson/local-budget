"""A standalone Walmart report — what the order detail behind the charges shows.

Deliberately NOT part of the monthly budget PDF, for the same reason the Amazon
one is not: that page is a fixed one-pager about a month, and hundreds of
product titles would swamp it while answering a question its reader may not have
asked. This is the follow-up, rendered in the same PRESS brand so the two read
as one publication.

**Two things this page carries that the Amazon report does not**, both because
the source is different rather than because the design is:

* *A channel split.* Walmart mixes online orders and in-store receipts into one
  history and one merchant on the statement family. They are different spending
  behaviours with different explanations, and a single bar hides that.

* *A split-settlement note.* A Walmart order routinely settles as several
  partial bank charges — one real order became five — so "orders" and
  "charges" on this page do not correspond, and a reader comparing the two
  counts needs to be told that rather than left to infer it.

**On the "kind" grouping.** Walmart sometimes publishes its own product category
and often does not. Where it does, this reports it; where it does not, it falls
back to the shared keyword heuristic in `connectors/kinds.py` — and the caption
says which mix produced the chart, rather than presenting a guess and a fact in
the same bar.
"""
from __future__ import annotations

import html as _html
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from ...agent.render import money
from ...report import brand
from ..kinds import classify
from .match import MERCHANT_LIKE, horizon, split_settlements


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
                SELECT m.order_number,
                       MIN(t.posted_date) AS posted_date,
                       MIN(t.txn_id)      AS txn_id
                  FROM walmart_matches m
                  JOIN transactions t ON t.txn_id = m.txn_id
                 WHERE 1=1{where}
              GROUP BY m.order_number)
            SELECT mo.posted_date, mo.txn_id, mo.order_number,
                   o.channel, i.product_id, i.title, i.seller,
                   i.category AS source_category,
                   COALESCE(i.quantity,1) AS qty,
                   i.line_price_cents AS line_cents
              FROM matched_orders mo
              JOIN walmart_orders o ON o.order_number = mo.order_number
              JOIN walmart_items i  ON i.order_number = mo.order_number""", params)]

    charges = conn.execute(
        f"""SELECT COUNT(*) n, COALESCE(-SUM(t.amount_cents),0) c
              FROM walmart_matches m JOIN transactions t ON t.txn_id = m.txn_id
             WHERE 1=1{where}""", params).fetchone()

    # Coverage must be scoped to the SAME window as the rest of the page.
    # Reporting the all-time figure on a two-month report describes a different
    # document than the one the reader is holding.
    like = " OR ".join("t.merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    scoped = conn.execute(
        f"""SELECT COUNT(*) n, COALESCE(-SUM(t.amount_cents),0) c
              FROM transactions t
             WHERE t.status='posted' AND t.amount_cents < 0
               AND ({like}){where}""", (*MERCHANT_LIKE, *params)).fetchone()

    by_kind: Counter = Counter()
    by_month: Counter = Counter()
    by_seller: Counter = Counter()
    by_channel: Counter = Counter()
    per_product: dict = defaultdict(lambda: {"n": 0, "cents": 0, "title": ""})
    from_source = 0
    for r in rows:
        # Walmart's own shelf category wins where it exists; the keyword table
        # only fills the gap. Counting which is which is what lets the caption
        # be honest instead of hedging about the whole chart.
        if r["source_category"]:
            r["kind"] = r["source_category"]
            from_source += 1
        else:
            r["kind"] = classify(r["title"])
        by_kind[r["kind"]] += r["line_cents"]
        by_month[r["posted_date"][:7]] += r["line_cents"]
        by_channel[r["channel"] or "unknown"] += r["line_cents"]
        if r["seller"]:
            by_seller[r["seller"]] += r["line_cents"]
        a = per_product[r["product_id"] or r["title"]]
        a["n"] += 1
        a["cents"] += r["line_cents"]
        a["title"] = r["title"] or "—"

    repeats = sorted((v for v in per_product.values() if v["n"] > 1),
                     key=lambda v: -v["cents"])
    biggest = sorted(rows, key=lambda r: -r["line_cents"])[:12]

    return {
        "rows": rows,
        "line_total": sum(r["line_cents"] for r in rows),
        "charge_total": int(charges["c"]), "charge_count": int(charges["n"]),
        "orders": len({r["order_number"] for r in rows}),
        "items": len(rows), "products": len(per_product),
        "span": (min((r["posted_date"] for r in rows), default="—"),
                 max((r["posted_date"] for r in rows), default="—")),
        "by_kind": by_kind.most_common(),
        "by_month": sorted(by_month.items()),
        "by_channel": by_channel.most_common(),
        "by_seller": by_seller.most_common(10),
        "kinds_from_source": from_source,
        "repeats": repeats[:12],
        "biggest": biggest,
        "scoped_total_cents": int(scoped["c"]), "scoped_charges": int(scoped["n"]),
        "scoped_pct": (round(int(charges["c"]) / int(scoped["c"]) * 100, 1)
                       if scoped["c"] else 0.0),
        "settlements": split_settlements(conn),
        "horizon": horizon(conn), "is_scoped": bool(since or until),
    }


def _esc(s: object) -> str:
    return _html.escape(str(s), quote=True)


def _safe(stem: str) -> str:
    """Filename stem reduced to characters that cannot escape the reports dir.
    `since`/`until` reach this from the command line."""
    return "".join(c for c in stem if c.isalnum() or c in "-_") or "walmart"


def _clip(text: str | None, n: int) -> str:
    """Truncate at a word boundary with an ellipsis. A hard slice cuts titles
    mid-word ("Mainstays 5-Shelf Bookcase, Blac"), which reads as corruption
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


def kind_caption(d: dict) -> str:
    """Say where the grouping came from, in the mix it actually came in.

    All three cases are different claims about the chart above, and printing the
    same hedge for all of them would be either a lie or a needless apology.
    """
    n, src = d["items"], d["kinds_from_source"]
    if not n:
        return ""
    if src == n:
        return ("Groups are Walmart's own product categories, as published on "
                "the order.")
    if src == 0:
        return ("Groups are inferred from product titles by keyword — these "
                "orders carried no product category. Treat them as a reading of "
                "the data, not a fact in it.")
    return (f"{src} of {n} lines are grouped by Walmart's own product category; "
            f"the remaining {n - src} are inferred from the title by keyword and "
            f"are a reading of the data rather than a fact in it.")


def build_html(d: dict, theme: dict) -> str:
    lo, hi = d["span"]
    hz, st = d["horizon"], d["settlements"]
    ident = theme["identity"]

    stats = "".join(
        f'<div class="stat{" focal" if focal else ""}"><div class="value">{_esc(v)}</div>'
        f'<div class="label">{_esc(k)}</div></div>'
        for k, v, focal in [
            ("Spent", money(d["charge_total"]), True),
            ("Orders", f'{d["orders"]:,}', False),
            ("Items", f'{d["items"]:,}', False),
            ("Distinct products", f'{d["products"]:,}', False),
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
                f'rollbacks, promotions and any Walmart Cash applied, and tax '
                f'pushes the other way — the two are different figures, not an '
                f'error.</p>')

    # Orders and charges do not correspond here, and the two counts sit a few
    # lines apart on this page. Left unsaid, a reader reconciles them and
    # concludes the report is wrong.
    settle_note = ""
    if st["split_orders"]:
        settle_note = (
            f' · {st["split_orders"]} of {st["orders"]} orders settled as more '
            f'than one charge (up to {st["max_parts"]})')

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<title>Walmart purchases</title>'
        f'<style>{brand.stylesheet(theme)}{template_css()}</style></head><body><main>'
        '<header class="masthead"><div class="masthead-row">'
        f'<span class="stamp">{_esc(ident["stamp"])}</span>'
        f'<span class="eyebrow">LOCAL BUDGET · WALMART PURCHASES · '
        f'{_esc(date.today().isoformat())}</span>'
        f'<span class="byline">{_esc(ident["byline"])}</span></div>'
        f'<h1>Walmart</h1>'
        f'<p class="standfirst">{_esc(lo)} to {_esc(hi)} — every order that '
        f'reconciled to a charge on the statement, itemised.</p></header>'
        f'<section class="stat-strip">{stats}</section>'

        '<h3 class="block-title">What it was</h3>'
        f'<section>{_bars(d["by_kind"])}</section>'
        f'<p class="caption">{_esc(kind_caption(d))}</p>'

        '<h3 class="block-title">Where</h3>'
        f'<section class="channel">{_bars(d["by_channel"])}</section>'
        '<p class="caption">Online orders and in-store receipts reach this '
        'ledger as different merchants and reconcile separately. Walmart only '
        'holds an in-store receipt when the card used is linked to the '
        'account.</p>'

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
        f'{d["charge_count"]} of {d["scoped_charges"]} Walmart charges in this '
        f'period reconciled · {d["scoped_pct"]}% of the '
        f'{money(d["scoped_total_cents"])} charged has item detail'
        f'{settle_note}'
        # The horizon is a property of the whole dataset, so it only belongs on
        # an all-history report — on a scoped one it describes something else.
        + ((f' · item detail reaches back to {_esc(hz["earliest"])}'
            if hz["earliest"] else '')
           + (f'; {hz["pre_count"]} older charges predate any order record'
              if hz["has_backlog"] else '')
           if not d["is_scoped"] else '')
        + '</footer></main></body></html>')


#: The report's layout template, tracked in git and shipped with the package.
#: Kept as a real .css file rather than a Python string so it can be edited and
#: re-rendered without touching code — the same reason the dashboard's colours
#: live in web/static/palette.css instead of a literal.
TEMPLATE_CSS = Path(__file__).resolve().parent / "assets" / "walmart-report.css"


def template_css() -> str:
    """The layout stylesheet. A missing file is a PACKAGING error, not a
    cosmetic one — rendering without it silently produces a differently-laid-out
    document, so it raises rather than falling back to an empty string."""
    if not TEMPLATE_CSS.is_file():
        raise FileNotFoundError(
            f"missing report template: {TEMPLATE_CSS} — the package is "
            f"incomplete (it ships as package data alongside report.py)")
    return TEMPLATE_CSS.read_text(encoding="utf-8")


def render(since: str | None = None, until: str | None = None,
           out_dir=None) -> dict:
    """Render the PDF. Returns {"ok": True, "path": str, "items": n}."""
    from ... import db, paths
    from ...report.pdf import render_pdf

    with db.connect() as conn:
        d = gather(conn, since, until)
    if not d["rows"]:
        raise ValueError("no reconciled Walmart items in that range — run "
                         "`budget walmart backfill` first")
    html = build_html(d, brand.load_theme())
    base = (out_dir or paths.reports_dir()).resolve()
    # The filename carries the scope. With a fixed name, rendering a two-month
    # view silently overwrites the all-history one — same path, wholly different
    # document, and no way to tell them apart afterwards.
    lo, hi = d["span"]
    stem = "walmart-purchases" if not (since or until) else f"walmart-{lo}_{hi}"
    out = (base / f"{_safe(stem)}.pdf").resolve()
    if not out.is_relative_to(base):
        raise ValueError("invalid output path")
    render_pdf(html, out)
    return {"ok": True, "path": str(out), "items": d["items"],
            "orders": d["orders"], "spent_cents": d["charge_total"]}
