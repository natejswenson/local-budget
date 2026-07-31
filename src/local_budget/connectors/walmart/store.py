"""Entities → rows. Everything money-shaped is converted here, exactly once.

`import_xlsx.py` hands over plain dicts whose money fields are **plain decimal
strings, exactly as the export displayed them**. That is the whole reason this
module can use `money.cents_from_amount_str` — the project's single mandated
conversion entry point, which parses with Decimal and RAISES on sub-cent
precision rather than silently rounding. The Amazon connector could not: its
upstream library hands back Python floats, so it carries its own lenient
converter. Ours is stricter because we own the layer above it.

**Where strictness applies, and where it deliberately does not.** The order
TOTAL is load-bearing — the matcher sums bank rows against it to the cent — so a
malformed one raises and the import writes nothing. Item line prices are
descriptive: they are scaled by `splits.allocate()` before anything is
attributed, so one unreadable price costs a line's precision rather than a
ledger fact. Failing an entire import over it would be the wrong trade.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from ... import money


class SyncAborted(RuntimeError):
    """A sync that would have written a suspiciously empty result.

    The failure mode this exists to prevent: Walmart redesigns a page, the
    parser silently yields zero orders, the sync reports success, and the
    connector quietly stops working while every command keeps printing a
    confident empty table. Zero rows is treated as a failure to be proven
    otherwise, never as a fact. It matters more here than it does for Amazon:
    there is no upstream library whose test suite would notice first.
    """


def to_cents(value, *, strict: bool = True) -> int | None:
    """A displayed amount → signed integer cents.

    The currency symbol is stripped here rather than in `money.py`: that module
    parses bank-export amount fields, which never carry one, and widening it to
    accept `$` would loosen the one place in the app that is deliberately strict
    about money. Thousands separators and the sign are its job, and stay its job.

    `strict=False` swallows a malformed value as None, for the descriptive
    fields where a missing price beats a failed sync (see the module docstring).
    """
    if value is None:
        return None
    s = str(value).replace("$", "").replace("−", "-").strip()
    if not s:
        return None
    try:
        return money.cents_from_amount_str(s)
    except money.AmountParseError:
        if strict:
            raise
        return None


def _iso(d) -> str | None:
    if d is None or d == "":
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)[:10]


def start_run(conn: sqlite3.Connection, scope: str) -> int:
    cur = conn.execute(
        "INSERT INTO walmart_sync_runs (started_at, status, scope) "
        "VALUES (?, 'running', ?)",
        (datetime.now().isoformat(timespec="seconds"), scope))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               orders_seen: int = 0, orders_upserted: int = 0,
               items_seen: int = 0, items_upserted: int = 0,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE walmart_sync_runs SET completed_at=?, status=?, orders_seen=?, "
        "orders_upserted=?, items_seen=?, items_upserted=?, error_message=? "
        "WHERE sync_run_id=?",
        (datetime.now().isoformat(timespec="seconds"), status, orders_seen,
         orders_upserted, items_seen, items_upserted, error, run_id))


def _quantity(value):
    """A line quantity, keeping fractions.

    Weighed goods are sold in fractions and the sources say so — half a pound of
    deli turkey arrives as `0.514`. This used to coerce to `int`, which read that
    as ZERO: the line kept its price and lost its quantity, so a report could
    show $5.58 of nothing. Whole numbers still store as ints so the common case
    reads `2`, not `2.0`.
    """
    if value in (None, ""):
        return 1
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 1
    return int(n) if n == int(n) else n


def item_sum_cents(items: list) -> int:
    """What an order's lines come to, computed before anything is written.

    Separate from `_store_items` because of ordering: `walmart_items` has a
    foreign key onto `walmart_orders`, so the lines cannot be inserted until the
    order row exists — but the order row wants this figure as it is written.
    Arithmetic first, then one insert, then the lines.
    """
    return sum(to_cents(i.get("line_price"), strict=False) or 0 for i in items or [])


def _store_items(conn: sqlite3.Connection, order_number: str, items: list) -> int:
    """Replace an order's item lines. Returns how many were written.

    Deleted and re-inserted rather than merged: an order's item list is small
    and wholly determined by the order, so a replace cannot leave a stale line
    behind the way a partial upsert can.

    `line_price` is stored as given, NOT divided by quantity. Walmart publishes
    a line total — two bags of peanuts is one line reading $14.50 — and turning
    that back into a unit price would invent precision the source never had, and
    lose a cent to rounding on every odd-quantity line.

    Must run AFTER the order row is inserted — the table has a foreign key onto
    it. `item_sum_cents` exists so the order row can still carry the total.
    """
    conn.execute("DELETE FROM walmart_items WHERE order_number = ?", (order_number,))
    n = 0
    for i, it in enumerate(items or []):
        conn.execute(
            "INSERT INTO walmart_items (order_number, line_no, product_id, title, "
            " quantity, line_price_cents, seller, category, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (order_number, i, it.get("product_id"), it.get("title"),
             _quantity(it.get("quantity")),
             to_cents(it.get("line_price"), strict=False),
             it.get("seller"), it.get("category"), it.get("status")))
        n += 1
    return n


def store_orders(conn: sqlite3.Connection, orders: list, run_id: int) -> dict:
    """Upsert orders and their items. Returns ``{"orders": n, "items": n}``.

    No charge rows are written, because Walmart publishes none: an order settles
    as a set of bank charges it never names, and `match.py` finds that set by
    summing against the order total.

    `detail_fetched` is only ever raised, never lowered: the list page yields an
    order without items and the detail page fills it in, and a later list-only
    sync passing over the same order must not reset the flag and send backfill
    round again for detail it already has.
    """
    now = datetime.now().isoformat(timespec="seconds")
    n_orders = n_items = 0
    for o in orders:
        num = o.get("order_number")
        if not num:
            continue                       # a row we could not identify is not a fact
        has_detail = 1 if o.get("detail_fetched") else 0
        # Summed before the insert (the order row carries it), written after it
        # (the item table has a foreign key onto that row). A list-only pass
        # knows neither and must leave both alone — see below.
        item_sum = item_sum_cents(o.get("items")) if has_detail else None
        conn.execute(
            "INSERT INTO walmart_orders (order_number, order_placed_date, "
            " grand_total_cents, subtotal_cents, tax_cents, shipping_cents, "
            " savings_cents, refund_total_cents, payment_method, item_count, "
            " channel, cancelled, detail_fetched, item_sum_cents, source, "
            " fetched_at, sync_run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(order_number) DO UPDATE SET "
            # Present on both pages, so the newer read wins outright.
            " order_placed_date=excluded.order_placed_date,"
            " grand_total_cents=excluded.grand_total_cents,"
            " cancelled=excluded.cancelled,"
            # COALESCED, because the two pages carry different fields and either
            # can arrive second. Only the LIST page knows the channel; only the
            # DETAIL page knows tax and shipping. Taking `excluded` outright
            # would let a detail fetch blank the channel an order was already
            # classified by — and the matcher filters candidates on it, so the
            # order would quietly stop matching anything.
            " subtotal_cents=COALESCE(excluded.subtotal_cents, subtotal_cents),"
            " tax_cents=COALESCE(excluded.tax_cents, tax_cents),"
            " shipping_cents=COALESCE(excluded.shipping_cents, shipping_cents),"
            " savings_cents=COALESCE(excluded.savings_cents, savings_cents),"
            " refund_total_cents=COALESCE(excluded.refund_total_cents, refund_total_cents),"
            " payment_method=COALESCE(excluded.payment_method, payment_method),"
            " item_count=COALESCE(excluded.item_count, item_count),"
            " channel=COALESCE(excluded.channel, channel),"
            " detail_fetched=MAX(walmart_orders.detail_fetched, excluded.detail_fetched),"
            # Coalesced for the same reason as the fields above: only a pass that
            # carried items knows the sum, and a later list-only sync must not
            # blank what a detail pass established.
            " item_sum_cents=COALESCE(excluded.item_sum_cents, item_sum_cents),"
            " source=COALESCE(excluded.source, source),"
            " fetched_at=excluded.fetched_at,"
            " sync_run_id=excluded.sync_run_id",
            (num, _iso(o.get("order_placed_date")),
             to_cents(o.get("grand_total")),
             to_cents(o.get("subtotal"), strict=False),
             to_cents(o.get("tax"), strict=False),
             to_cents(o.get("shipping"), strict=False),
             to_cents(o.get("savings"), strict=False),
             to_cents(o.get("refund_total"), strict=False),
             o.get("payment_method"), o.get("item_count"), o.get("channel"),
             1 if o.get("cancelled") else 0, has_detail, item_sum,
             o.get("source", "scrape"), now, run_id))

        # Only a detail fetch knows the item list. A list-only pass carries no
        # items, and writing that through would DELETE the lines a previous
        # detail fetch stored — a silent data loss on every rolling sync.
        if has_detail:
            n_items += _store_items(conn, num, o.get("items") or [])
        n_orders += 1
    return {"orders": n_orders, "items": n_items}


def assert_not_vacuous(conn: sqlite3.Connection, *, orders: int,
                       scope_has_known_charges: bool) -> None:
    """Refuse to call an empty sync a success when we know better.

    `scope_has_known_charges` is computed from the ledger: if the bank recorded
    Walmart charges in the window and the connector came back with nothing, the
    parser is broken and saying "0 orders — all done" would hide it. An account
    with genuinely no orders is a legitimate zero and passes.
    """
    if orders or not scope_has_known_charges:
        return
    raise SyncAborted(
        "the import produced 0 orders, but the ledger has Walmart charges in "
        "this window — the parser is almost certainly broken rather than the "
        "export being empty. Check that the file is a Walmart purchase-history "
        "export and that its sheets still carry the expected columns. Nothing "
        "was written.")
