"""Entities → rows. Everything money-shaped is converted here, exactly once.

`money.py` opens with "Money is signed INTEGER CENTS everywhere. Never float
dollars." The upstream library hands back Python floats, so this module is the
seam where that rule is enforced: every value goes through `Decimal(str(x))`,
never `int(x * 100)`. `19.99 * 100` is `1998.9999...` in binary floating point
and truncates to 1998 — a silent one-cent loss on an unknown fraction of rows,
which is precisely the kind of error a ledger must not have.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP


class SyncAborted(RuntimeError):
    """A sync that would have written a suspiciously empty result.

    The failure mode this exists to prevent: Amazon redesigns a page, the
    parser silently yields zero orders, the sync reports success, and the
    connector quietly stops working while every command keeps printing a
    confident empty table. Zero rows is treated as a failure to be proven
    otherwise, never as a fact.
    """


def to_cents(value) -> int | None:
    """Dollars (float | str | Decimal | None) → signed integer cents.

    Via Decimal(str(...)) so the decimal literal the site displayed is the one
    that gets rounded, and ROUND_HALF_UP so a true half-cent goes the way a
    human would expect rather than to-even.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
        if not value:
            return None
    d = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(d * 100)


def _text(v) -> str | None:
    """Anything destined for a TEXT column → a plain string.

    Not defensive padding: the library returns a `Seller` OBJECT for
    `Item.seller` and a plain `str` for `Transaction.seller` — same concept,
    two types — and sqlite3 rejects the object outright with "type 'Seller' is
    not supported". Every mock-based test passed while this was guaranteed to
    crash on the first real sync.

    `.name` is preferred over `str()` because the entity's `__str__` renders as
    `Seller: Amazon.com Services, Inc`, which would land the class name in the
    data.
    """
    if v is None:
        return None
    name = getattr(v, "name", None)
    if isinstance(name, str):
        return name
    return v if isinstance(v, str) else str(v)


def _iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)[:10]


def start_run(conn: sqlite3.Connection, scope: str) -> int:
    cur = conn.execute(
        "INSERT INTO amazon_sync_runs (started_at, status, scope) VALUES (?, 'running', ?)",
        (datetime.now().isoformat(timespec="seconds"), scope))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str,
               orders_seen: int = 0, orders_upserted: int = 0,
               txns_seen: int = 0, txns_upserted: int = 0,
               error: str | None = None) -> None:
    conn.execute(
        "UPDATE amazon_sync_runs SET completed_at=?, status=?, orders_seen=?, "
        "orders_upserted=?, txns_seen=?, txns_upserted=?, error_message=? "
        "WHERE sync_run_id=?",
        (datetime.now().isoformat(timespec="seconds"), status, orders_seen,
         orders_upserted, txns_seen, txns_upserted, error, run_id))


def store_orders(conn: sqlite3.Connection, orders: list, run_id: int) -> int:
    """Upsert orders + their items. Returns the number of orders written.

    Items are deleted and re-inserted per order rather than merged: an order's
    item list is small and wholly determined by the order, so a replace cannot
    leave a stale line behind the way a partial upsert can.
    """
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    for o in orders:
        num = getattr(o, "order_number", None)
        if not num:
            continue                       # a row we could not identify is not a fact
        conn.execute(
            "INSERT INTO amazon_orders (order_number, order_placed_date, "
            " grand_total_cents, subtotal_cents, tax_cents, shipping_cents, "
            " refund_total_cents, payment_method, item_count, cancelled, "
            " fetched_at, sync_run_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(order_number) DO UPDATE SET "
            " order_placed_date=excluded.order_placed_date,"
            " grand_total_cents=excluded.grand_total_cents,"
            " subtotal_cents=excluded.subtotal_cents,"
            " tax_cents=excluded.tax_cents,"
            " shipping_cents=excluded.shipping_cents,"
            " refund_total_cents=excluded.refund_total_cents,"
            " payment_method=excluded.payment_method,"
            " item_count=excluded.item_count,"
            " cancelled=excluded.cancelled,"
            " fetched_at=excluded.fetched_at,"
            " sync_run_id=excluded.sync_run_id",
            (num, _iso(getattr(o, "order_placed_date", None)),
             to_cents(getattr(o, "grand_total", None)),
             to_cents(getattr(o, "subtotal", None)),
             to_cents(getattr(o, "estimated_tax", None)),
             to_cents(getattr(o, "shipping_total", None)),
             to_cents(getattr(o, "refund_total", None)),
             _text(getattr(o, "payment_method", None)),
             getattr(o, "item_count", None),
             1 if getattr(o, "cancelled", False) else 0,
             now, run_id))

        conn.execute("DELETE FROM amazon_items WHERE order_number = ?", (num,))
        for i, it in enumerate(getattr(o, "items", None) or []):
            conn.execute(
                "INSERT INTO amazon_items (order_number, line_no, asin, title, "
                " quantity, unit_price_cents, seller, condition) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (num, i,
                 _text(getattr(it, "asin", None)),
                 _text(getattr(it, "title", None)),
                 getattr(it, "quantity", None) or 1,
                 to_cents(getattr(it, "price", None)),
                 _text(getattr(it, "seller", None)),
                 _text(getattr(it, "condition", None))))
        written += 1
    return written


def store_transactions(conn: sqlite3.Connection, txns: list, run_id: int) -> int:
    """Upsert Amazon's own charge list. Returns rows written.

    `Transaction.grand_total` is POSITIVE for a refund and negative for a
    charge (the library derives `is_refund` from exactly that sign), which
    happens to match this ledger's own convention for `amount_cents` — an
    outflow is negative. Stored as given, so the two compare directly.
    """
    now = datetime.now().isoformat(timespec="seconds")
    written = 0
    for t in txns:
        cents = to_cents(getattr(t, "grand_total", None))
        completed = _iso(getattr(t, "completed_date", None))
        if cents is None or completed is None:
            continue
        conn.execute(
            "INSERT INTO amazon_transactions (completed_date, grand_total_cents, "
            " is_refund, order_number, payment_method, seller, fetched_at, sync_run_id) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(completed_date, grand_total_cents, order_number, payment_method) "
            "DO UPDATE SET is_refund=excluded.is_refund, seller=excluded.seller, "
            " fetched_at=excluded.fetched_at, sync_run_id=excluded.sync_run_id",
            (completed, cents,
             1 if getattr(t, "is_refund", False) else 0,
             _text(getattr(t, "order_number", None)),
             _text(getattr(t, "payment_method", None)),
             _text(getattr(t, "seller", None)),
             now, run_id))
        written += 1
    return written


def assert_not_vacuous(conn: sqlite3.Connection, *, orders: int, txns: int,
                       scope_has_known_charges: bool) -> None:
    """Refuse to call an empty sync a success when we know better.

    `scope_has_known_charges` is computed from the ledger: if the bank recorded
    Amazon charges in the window and the connector came back with nothing, the
    parser is broken and saying "0 orders — all done" would hide it. An account
    with genuinely no orders is a legitimate zero and passes.
    """
    if orders or txns or not scope_has_known_charges:
        return
    raise SyncAborted(
        "sync returned 0 orders and 0 transactions, but the ledger has Amazon "
        "charges in this window — the parser is almost certainly broken rather "
        "than the account being empty. Try `uv add amazon-orders@latest`, then "
        "`budget amazon login`. Nothing was written.")
