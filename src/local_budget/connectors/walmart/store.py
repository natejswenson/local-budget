"""Entities → rows. Everything money-shaped is converted here, exactly once.

`parse.py` hands over plain dicts whose money fields are **plain decimal
strings, exactly as the page displayed them**. That is the whole reason this
module can use `money.cents_from_amount_str` — the project's single mandated
conversion entry point, which parses with Decimal and RAISES on sub-cent
precision rather than silently rounding. The Amazon connector could not: its
upstream library hands back Python floats, so it carries its own lenient
converter. Ours is stricter because we own the layer above it.

**Where strictness applies, and where it deliberately does not.** Order totals
and charge amounts are load-bearing — the matcher compares them to the cent
against the bank ledger — so a malformed one raises and the sync writes nothing.
Item unit prices are descriptive: they are scaled by `splits.allocate()` before
anything is attributed, so one unreadable price costs a line's precision rather
than a ledger fact. Failing an entire backfill over it would be the wrong trade.
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
               charges_seen: int = 0, charges_upserted: int = 0,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE walmart_sync_runs SET completed_at=?, status=?, orders_seen=?, "
        "orders_upserted=?, charges_seen=?, charges_upserted=?, error_message=? "
        "WHERE sync_run_id=?",
        (datetime.now().isoformat(timespec="seconds"), status, orders_seen,
         orders_upserted, charges_seen, charges_upserted, error, run_id))


def _store_items(conn: sqlite3.Connection, order_number: str, items: list) -> None:
    """Replace an order's item lines.

    Deleted and re-inserted rather than merged: an order's item list is small
    and wholly determined by the order, so a replace cannot leave a stale line
    behind the way a partial upsert can.
    """
    conn.execute("DELETE FROM walmart_items WHERE order_number = ?", (order_number,))
    for i, it in enumerate(items or []):
        conn.execute(
            "INSERT INTO walmart_items (order_number, line_no, product_id, title, "
            " quantity, unit_price_cents, seller, category, status) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (order_number, i, it.get("product_id"), it.get("title"),
             int(it.get("quantity") or 1),
             to_cents(it.get("unit_price"), strict=False),
             it.get("seller"), it.get("category"), it.get("status")))


def _store_charges(conn: sqlite3.Connection, order: dict, order_number: str,
                   run_id: int, now: str) -> int:
    """Write an order's charges, synthesizing one if the page showed none.

    A charge row is what the matcher works from, so an order with none is
    invisible to reconciliation — which is why the synthesized fallback exists.
    It is flagged `derived=1` and dated to the order, and that is an inference
    about WHEN the card was hit: a split-shipment order gets one derived row
    standing in for two or three real ones, and the ±3-day match window is what
    absorbs the difference. Reported by `status` and on the report, never hidden.

    Refunds are NEVER synthesized. An order's refund total says a refund
    happened, not when it settled, and a charge row invented on the wrong date
    would either match the wrong bank row or sit unmatched forever looking like
    a parser bug. Only observed refund lines are stored.
    """
    written = 0
    charges = order.get("charges") or []
    if charges:
        # An observed charge supersedes the guess that stood in for it. Without
        # this the derived row survives alongside the real ones — same order,
        # counted twice — because it collides with none of them on the natural
        # key. Its match goes with it: that match was made against an inferred
        # date, and the observed charges are about to be matched properly.
        conn.execute(
            "DELETE FROM walmart_matches WHERE walmart_charge_id IN "
            "(SELECT walmart_charge_id FROM walmart_charges "
            "  WHERE order_number = ? AND derived = 1)", (order_number,))
        conn.execute(
            "DELETE FROM walmart_charges WHERE order_number = ? AND derived = 1",
            (order_number,))
    for c in charges:
        when = _iso(c.get("charged_date"))
        cents = to_cents(c.get("amount"))
        if when is None or cents is None:
            continue
        written += _upsert_charge(
            conn, order_number=order_number, when=when, cents=cents,
            is_refund=bool(c.get("is_refund")),
            payment_method=c.get("payment_method") or order.get("payment_method"),
            derived=0, run_id=run_id, now=now)
    if charges:
        return written

    total = to_cents(order.get("grand_total"))
    when = _iso(order.get("order_placed_date"))
    if total is None or not total or when is None or order.get("cancelled"):
        return written
    # Order totals are POSITIVE magnitudes; the ledger's outflow is negative.
    return _upsert_charge(
        conn, order_number=order_number, when=when, cents=-abs(total),
        is_refund=False, payment_method=order.get("payment_method"),
        derived=1, run_id=run_id, now=now)


def _upsert_charge(conn: sqlite3.Connection, *, order_number: str, when: str,
                   cents: int, is_refund: bool, payment_method, derived: int,
                   run_id: int, now: str) -> int:
    conn.execute(
        "INSERT INTO walmart_charges (order_number, charged_date, amount_cents, "
        " is_refund, payment_method, derived, fetched_at, sync_run_id) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(order_number, charged_date, amount_cents, payment_method) "
        "DO UPDATE SET is_refund=excluded.is_refund, derived=excluded.derived, "
        " fetched_at=excluded.fetched_at, sync_run_id=excluded.sync_run_id",
        (order_number, when, cents, 1 if is_refund else 0, payment_method,
         derived, now, run_id))
    return 1


def store_orders(conn: sqlite3.Connection, orders: list, run_id: int) -> dict:
    """Upsert orders, their items and their charges.

    Returns ``{"orders": n, "charges": n}``.

    `detail_fetched` is only ever raised, never lowered: the list page yields an
    order without items and the detail page fills it in, and a later list-only
    sync passing over the same order must not reset the flag and send backfill
    round again for detail it already has.
    """
    now = datetime.now().isoformat(timespec="seconds")
    n_orders = n_charges = 0
    for o in orders:
        num = o.get("order_number")
        if not num:
            continue                       # a row we could not identify is not a fact
        has_detail = 1 if o.get("detail_fetched") else 0
        conn.execute(
            "INSERT INTO walmart_orders (order_number, order_placed_date, "
            " grand_total_cents, subtotal_cents, tax_cents, shipping_cents, "
            " savings_cents, refund_total_cents, payment_method, item_count, "
            " channel, cancelled, detail_fetched, fetched_at, sync_run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(order_number) DO UPDATE SET "
            " order_placed_date=excluded.order_placed_date,"
            " grand_total_cents=excluded.grand_total_cents,"
            " subtotal_cents=excluded.subtotal_cents,"
            " tax_cents=excluded.tax_cents,"
            " shipping_cents=excluded.shipping_cents,"
            " savings_cents=excluded.savings_cents,"
            " refund_total_cents=excluded.refund_total_cents,"
            " payment_method=excluded.payment_method,"
            " item_count=excluded.item_count,"
            " channel=excluded.channel,"
            " cancelled=excluded.cancelled,"
            " detail_fetched=MAX(walmart_orders.detail_fetched, excluded.detail_fetched),"
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
             1 if o.get("cancelled") else 0, has_detail, now, run_id))

        # Only a detail fetch knows the item list. A list-only pass carries no
        # items, and writing that through would DELETE the lines a previous
        # detail fetch stored — a silent data loss on every rolling sync.
        if has_detail:
            _store_items(conn, num, o.get("items") or [])
        n_charges += _store_charges(conn, o, num, run_id, now)
        n_orders += 1
    return {"orders": n_orders, "charges": n_charges}


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
        "sync returned 0 orders, but the ledger has Walmart charges in this "
        "window — the parser is almost certainly broken rather than the account "
        "being empty. Walmart may have changed its order pages; run "
        "`budget walmart capture` to see what they serve now, or "
        "`budget walmart login` if the session has expired. Nothing was written.")
