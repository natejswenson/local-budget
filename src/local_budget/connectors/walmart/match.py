"""Reconcile Walmart charges against the bank ledger.

The chain this completes:

    bank transaction  →  walmart_charge  →  order  →  items

The governing rule is **never guess**, the same as the Amazon matcher: two
Walmart charges of the same amount days apart is routine, and attributing the
wrong basket of items to a charge is worse than leaving it unexplained.
Ambiguity is recorded as unmatched and surfaced for confirmation.

**One rule the Amazon matcher has no need for: channel.** Walmart puts online
orders and in-store receipts in one history, and they post to the bank under
different merchant strings — `WALMART.COM` versus `WM SUPERCENTER`. Amount and
date alone would happily attach a $63.41 grocery pickup to a $63.41 in-store
run three days earlier, and the resulting item list would be confidently,
invisibly wrong. So candidates are filtered by channel whenever the order says
which it was.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

#: Bank-side merchant patterns, split by where the purchase happened.
#: Verified against the ledger: these three patterns match every Walmart charge
#: in it and nothing else. Sam's Club is deliberately absent — it is a separate
#: site with a separate login, so walmart.com order history structurally cannot
#: explain it, and counting it would report ~9% of "Walmart" spend as
#: unexplained forever. A missing number beats a dishonest one.
ONLINE_LIKE = ("WALMART%",)
INSTORE_LIKE = ("WAL MART%", "WM SUPERC%")
MERCHANT_LIKE = ONLINE_LIKE + INSTORE_LIKE

#: How far a settle date may drift from the charge date. Three days covers a
#: weekend; wider starts pulling in genuinely different purchases. It also
#: absorbs the error in a `derived` charge, which is dated to the order rather
#: than to the day the card was actually hit.
WINDOW_DAYS = 3


def patterns_for(channel: str | None) -> tuple[str, ...]:
    """Which merchant patterns a charge of this channel may match.

    An unknown channel falls back to all of them. That is the honest default:
    refusing to match what we cannot classify would drop real reconciliations,
    and this is still amount-exact and date-bounded.
    """
    if channel == "online":
        return ONLINE_LIKE
    if channel == "in-store":
        return INSTORE_LIKE
    return MERCHANT_LIKE


def _candidates(conn: sqlite3.Connection, cents: int, when: str, window: int,
                channel: str | None) -> list[sqlite3.Row]:
    """Unmatched Walmart-looking bank rows of exactly this amount, in window.

    Amount is matched EXACTLY. A tolerance would be the obvious next knob and it
    is deliberately absent: any slop here buys false matches far faster than it
    buys true ones.
    """
    pats = patterns_for(channel)
    like = " OR ".join("t.merchant_norm LIKE ?" for _ in pats)
    return conn.execute(
        f"""SELECT t.txn_id, t.posted_date, t.merchant_norm, t.amount_cents
              FROM transactions t
             WHERE t.status = 'posted'
               AND t.amount_cents = ?
               AND ({like})
               AND ABS(julianday(t.posted_date) - julianday(?)) <= ?
               AND t.txn_id NOT IN (SELECT txn_id FROM walmart_matches)
          ORDER BY ABS(julianday(t.posted_date) - julianday(?)), t.txn_id""",
        (cents, *pats, when, window, when)).fetchall()


def _unmatched_charges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.walmart_charge_id, c.charged_date, c.amount_cents,
                  c.order_number, c.derived, o.channel
             FROM walmart_charges c
             LEFT JOIN walmart_orders o ON o.order_number = c.order_number
            WHERE c.walmart_charge_id NOT IN
                  (SELECT walmart_charge_id FROM walmart_matches)
         ORDER BY c.charged_date, c.walmart_charge_id""").fetchall()


def _record(conn: sqlite3.Connection, charge_id: int, txn_id: int,
            confidence: str, method: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO walmart_matches "
        "(walmart_charge_id, txn_id, confidence, method, matched_at) "
        "VALUES (?,?,?,?,?)",
        (charge_id, txn_id, confidence, method,
         datetime.now().isoformat(timespec="seconds")))


def run(conn: sqlite3.Connection) -> dict:
    """Match everything unambiguous. Returns a summary dict.

    Two passes, and the order is load-bearing: a same-day exact match must be
    able to claim its bank row before any windowed match is allowed to take it.
    One combined pass lets a ±3-day match steal the row a same-day charge
    needed, leaving BOTH wrong — the loose one mismatched and the exact one
    orphaned.
    """
    exact = windowed = 0
    ambiguous: list[dict] = []

    # Pass 1 — same day, same amount, same channel.
    for c in _unmatched_charges(conn):
        cands = _candidates(conn, c["amount_cents"], c["charged_date"], 0,
                            c["channel"])
        if len(cands) == 1:
            _record(conn, c["walmart_charge_id"], cands[0]["txn_id"],
                    "exact", "same-day+amount")
            exact += 1

    # Pass 2 — within the window, and only when a single candidate remains.
    for c in _unmatched_charges(conn):
        cands = _candidates(conn, c["amount_cents"], c["charged_date"],
                            WINDOW_DAYS, c["channel"])
        if len(cands) == 1:
            _record(conn, c["walmart_charge_id"], cands[0]["txn_id"], "windowed",
                    f"amount+-{WINDOW_DAYS}d")
            windowed += 1
        elif len(cands) > 1:
            ambiguous.append({
                "walmart_charge_id": c["walmart_charge_id"],
                "charged_date": c["charged_date"],
                "amount_cents": c["amount_cents"],
                "order_number": c["order_number"],
                "derived": bool(c["derived"]),
                "candidates": [dict(x) for x in cands],
            })

    return {"exact": exact, "windowed": windowed, "ambiguous": ambiguous,
            "matched": exact + windowed}


def confirm(conn: sqlite3.Connection, charge_id: int, txn_id: int) -> None:
    """Record a human-chosen match for an ambiguous pair."""
    _record(conn, charge_id, txn_id, "manual", "confirmed")


def _cov_pair(conn: sqlite3.Connection, pats: tuple[str, ...],
              month: str | None) -> dict:
    """Total vs matched outflow for one set of merchant patterns."""
    like = " OR ".join("merchant_norm LIKE ?" for _ in pats)
    where_month = " AND posted_date LIKE ?" if month else ""
    params: tuple = (*pats, *((f"{month}-%",) if month else ()))
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
               AND txn_id IN (SELECT txn_id FROM walmart_matches)""",
        params).fetchone()
    pct = round(matched["c"] / total["c"] * 100, 1) if total["c"] else 0.0
    return {"total_cents": int(total["c"]), "total_txns": int(total["n"]),
            "matched_cents": int(matched["c"]), "matched_txns": int(matched["n"]),
            "coverage_pct": pct}


def coverage(conn: sqlite3.Connection, month: str | None = None) -> dict:
    """What share of Walmart DOLLARS in the ledger are explained by items.

    Dollars, not row count, is the honest denominator: matching nine $6 charges
    and missing one $400 charge is not 90% coverage of anything that matters.

    Reported per channel as well as overall, because the two are different
    stories with different fixes. Online coverage is a question about the
    parser; in-store coverage is a question about whether Walmart has your
    receipts at all — it only does when the card is linked to the account — and
    one number averaging both would obscure which is which.
    """
    overall = _cov_pair(conn, MERCHANT_LIKE, month)
    return {
        "month": month, **overall,
        "channels": {"online": _cov_pair(conn, ONLINE_LIKE, month),
                     "in-store": _cov_pair(conn, INSTORE_LIKE, month)},
        "derived": derived_share(conn, month),
    }


def derived_share(conn: sqlite3.Connection, month: str | None = None) -> dict:
    """How much of what we matched rests on a SYNTHESIZED charge.

    A derived charge is an inference about when the card was hit, made because
    the order page showed no payment line. The items behind it are still real;
    the date is ours. Quoting coverage without this would present an inference
    with the same confidence as an observation.
    """
    where_month = " AND t.posted_date LIKE ?" if month else ""
    params: tuple = (f"{month}-%",) if month else ()
    row = conn.execute(
        f"""SELECT COUNT(*) AS n,
                   COALESCE(SUM(c.derived), 0) AS d,
                   COALESCE(-SUM(t.amount_cents), 0) AS cents,
                   COALESCE(-SUM(CASE WHEN c.derived THEN t.amount_cents END), 0)
                       AS d_cents
              FROM walmart_matches m
              JOIN walmart_charges c ON c.walmart_charge_id = m.walmart_charge_id
              JOIN transactions t    ON t.txn_id = m.txn_id
             WHERE 1=1{where_month}""", params).fetchone()
    n, d = int(row["n"]), int(row["d"])
    return {"matched": n, "derived": d, "cents": int(row["cents"]),
            "derived_cents": int(row["d_cents"]),
            "derived_pct": round(d / n * 100, 1) if n else 0.0}


def horizon(conn: sqlite3.Connection) -> dict:
    """How far back reconciliation actually reaches, and what lies beyond it.

    Without this, a low coverage number is unreadable: it looks like a data
    quality problem when it is a window problem. Charges older than the earliest
    stored Walmart charge cannot be matched no matter what.

    Returns ``{earliest, pre_count, pre_cents, has_backlog}``; `earliest` is
    None when nothing is stored at all.
    """
    row = conn.execute(
        "SELECT MIN(charged_date) AS d FROM walmart_charges").fetchone()
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
    """Item lines behind the matched Walmart charges, largest first.

    Joined through the ORDER, and deduplicated to one row per item line: a
    split-shipment order matches several charges, and joining items through
    each of them counts the same product once per shipment.
    """
    where_month = " AND t.posted_date LIKE ?" if month else ""
    params: tuple = (f"{month}-%",) if month else ()
    rows = conn.execute(
        f"""SELECT i.title, i.product_id, i.quantity, i.unit_price_cents,
                   i.seller, i.category, o.order_number, o.channel,
                   MIN(t.posted_date) AS posted_date, MIN(t.txn_id) AS txn_id
              FROM walmart_matches m
              JOIN walmart_charges c ON c.walmart_charge_id = m.walmart_charge_id
              JOIN transactions t    ON t.txn_id = m.txn_id
              JOIN walmart_orders o  ON o.order_number = c.order_number
              JOIN walmart_items i   ON i.order_number = o.order_number
             WHERE 1=1{where_month}
          GROUP BY i.item_id
          ORDER BY (i.unit_price_cents * COALESCE(i.quantity,1)) DESC""",
        params).fetchall()
    return [dict(r) for r in rows]
