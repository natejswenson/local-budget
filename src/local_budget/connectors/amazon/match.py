"""Reconcile Amazon's own charge list against the bank ledger.

The chain this completes:

    bank transaction  →  amazon_transaction  →  order  →  items

Matching goes through `amazon_transactions` rather than order totals because an
order total is frequently not what hit the card: one order ships in three boxes
and settles as three separate charges, days apart. Amazon's transaction list is
already at charge granularity, which is what makes this tractable at all.

The governing rule is **never guess**. Duplicate-amount Amazon charges in the
same few days are completely routine (two $9.99 orders in a week), and a wrong
match is worse than no match — it would attribute the wrong basket of items to
a charge and quietly mislead every question asked afterwards. Ambiguity is
recorded as unmatched and surfaced for confirmation.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

#: Bank-side merchant patterns that can be an Amazon charge.
MERCHANT_LIKE = ("AMAZON%", "AMZN%", "%AMZN.COM%", "PRIME VIDEO%")

#: How far a settle date may drift from Amazon's completed date. Three days
#: covers a weekend; wider starts pulling in genuinely different purchases.
WINDOW_DAYS = 3


def _candidates(conn: sqlite3.Connection, cents: int, completed: str,
                window: int) -> list[sqlite3.Row]:
    """Unmatched Amazon-looking bank rows of exactly this amount, in window.

    Amount is matched EXACTLY. A tolerance would be the obvious next knob and
    it is deliberately absent: Amazon charges the cent it says it charges, and
    any slop here buys false matches far faster than it buys true ones.
    """
    like = " OR ".join("t.merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    return conn.execute(
        f"""SELECT t.txn_id, t.posted_date, t.merchant_norm, t.amount_cents
              FROM transactions t
             WHERE t.status = 'posted'
               AND t.amount_cents = ?
               AND ({like})
               AND ABS(julianday(t.posted_date) - julianday(?)) <= ?
               AND t.txn_id NOT IN (SELECT txn_id FROM amazon_matches)
          ORDER BY ABS(julianday(t.posted_date) - julianday(?)), t.txn_id""",
        (cents, *MERCHANT_LIKE, completed, window, completed)).fetchall()


def _unmatched_amazon_txns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT amazon_txn_id, completed_date, grand_total_cents, order_number
             FROM amazon_transactions
            WHERE amazon_txn_id NOT IN (SELECT amazon_txn_id FROM amazon_matches)
         ORDER BY completed_date, amazon_txn_id""").fetchall()


def _record(conn: sqlite3.Connection, amazon_txn_id: int, txn_id: int,
            confidence: str, method: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO amazon_matches "
        "(amazon_txn_id, txn_id, confidence, method, matched_at) VALUES (?,?,?,?,?)",
        (amazon_txn_id, txn_id, confidence, method,
         datetime.now().isoformat(timespec="seconds")))


def run(conn: sqlite3.Connection) -> dict:
    """Match everything unambiguous. Returns a summary dict.

    Two passes, and the order is load-bearing: a same-day exact match must be
    able to claim its bank row before any windowed match is allowed to take it.
    Running one combined pass lets a ±3-day match steal the row that a same-day
    charge needed, leaving BOTH wrong — the loose one mismatched and the exact
    one orphaned.
    """
    exact = windowed = 0
    ambiguous: list[dict] = []

    # Pass 1 — same day, same amount.
    for a in _unmatched_amazon_txns(conn):
        cands = _candidates(conn, a["grand_total_cents"], a["completed_date"], 0)
        if len(cands) == 1:
            _record(conn, a["amazon_txn_id"], cands[0]["txn_id"], "exact", "same-day+amount")
            exact += 1

    # Pass 2 — within the window, and only when a single candidate remains.
    for a in _unmatched_amazon_txns(conn):
        cands = _candidates(conn, a["grand_total_cents"], a["completed_date"], WINDOW_DAYS)
        if len(cands) == 1:
            _record(conn, a["amazon_txn_id"], cands[0]["txn_id"], "windowed",
                    f"amount+-{WINDOW_DAYS}d")
            windowed += 1
        elif len(cands) > 1:
            ambiguous.append({
                "amazon_txn_id": a["amazon_txn_id"],
                "completed_date": a["completed_date"],
                "amount_cents": a["grand_total_cents"],
                "order_number": a["order_number"],
                "candidates": [dict(c) for c in cands],
            })

    return {"exact": exact, "windowed": windowed, "ambiguous": ambiguous,
            "matched": exact + windowed}


def confirm(conn: sqlite3.Connection, amazon_txn_id: int, txn_id: int) -> None:
    """Record a human-chosen match for an ambiguous pair."""
    _record(conn, amazon_txn_id, txn_id, "manual", "confirmed")


def coverage(conn: sqlite3.Connection, month: str | None = None) -> dict:
    """What share of Amazon DOLLARS in the ledger are explained by items.

    Dollars, not row count, is the honest denominator: matching nine $6 charges
    and missing one $400 charge is not 90% coverage of anything that matters.
    """
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    where_month = " AND posted_date LIKE ?" if month else ""
    params: tuple = (*MERCHANT_LIKE, *((f"{month}-%",) if month else ()))
    total = conn.execute(
        f"""SELECT COALESCE(-SUM(amount_cents), 0) AS c, COUNT(*) AS n
              FROM transactions
             WHERE status='posted' AND amount_cents < 0
               AND ({like}){where_month}""", params).fetchone()
    matched = conn.execute(
        f"""SELECT COALESCE(-SUM(amount_cents), 0) AS c, COUNT(*) AS n
              FROM transactions
             WHERE status='posted' AND amount_cents < 0
               AND ({like}){where_month}
               AND txn_id IN (SELECT txn_id FROM amazon_matches)""", params).fetchone()
    pct = round(matched["c"] / total["c"] * 100, 1) if total["c"] else 0.0
    return {"month": month, "total_cents": total["c"], "total_txns": total["n"],
            "matched_cents": matched["c"], "matched_txns": matched["n"],
            "coverage_pct": pct}


def horizon(conn: sqlite3.Connection) -> dict:
    """How far back reconciliation actually reaches, and what lies beyond it.

    Matching goes through Amazon's own transaction records, and that page is
    queried days-back — it may not reach as far as order history does. Charges
    older than the earliest stored transaction therefore CANNOT be matched, no
    matter how many orders were backfilled.

    Without this, a low coverage number is unreadable: it looks like a data
    quality problem when it is a window problem. Reporting the horizon is what
    keeps coverage an honest figure rather than a discouraging one.

    Returns ``{earliest, pre_count, pre_cents, has_backlog}``; `earliest` is
    None when no transactions are stored at all.
    """
    row = conn.execute(
        "SELECT MIN(completed_date) AS d FROM amazon_transactions").fetchone()
    earliest = row["d"] if row else None
    if not earliest:
        return {"earliest": None, "pre_count": 0, "pre_cents": 0,
                "has_backlog": False}

    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    pre = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(-SUM(amount_cents), 0) AS c
              FROM transactions
             WHERE status='posted' AND amount_cents < 0
               AND posted_date < ? AND ({like})""",
        (earliest, *MERCHANT_LIKE)).fetchone()
    return {"earliest": earliest, "pre_count": int(pre["n"]),
            "pre_cents": int(pre["c"]), "has_backlog": bool(pre["n"])}


def breakdown(conn: sqlite3.Connection, month: str | None = None) -> list[dict]:
    """Item lines behind the matched Amazon charges, largest first."""
    where_month = " AND t.posted_date LIKE ?" if month else ""
    params: tuple = (f"{month}-%",) if month else ()
    rows = conn.execute(
        f"""SELECT i.title, i.asin, i.quantity, i.unit_price_cents,
                   i.seller, o.order_number, t.posted_date, t.txn_id
              FROM amazon_matches m
              JOIN amazon_transactions a ON a.amazon_txn_id = m.amazon_txn_id
              JOIN transactions t        ON t.txn_id = m.txn_id
              JOIN amazon_orders o       ON o.order_number = a.order_number
              JOIN amazon_items i        ON i.order_number = o.order_number
             WHERE 1=1{where_month}
          ORDER BY (i.unit_price_cents * COALESCE(i.quantity,1)) DESC""",
        params).fetchall()
    return [dict(r) for r in rows]
