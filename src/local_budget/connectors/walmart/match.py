"""Reconcile Walmart orders against the bank ledger.

The chain this completes:

    bank transactions  →  order  →  items

**An order is not a charge.** Amazon publishes its own charge list at the
granularity the bank posts, so its matcher pairs one charge to one bank row.
Walmart publishes orders, and an order routinely settles as several partial
charges it never enumerates. A pattern seen in a real ledger:

    day 1   $100.00  ->  one bank row
    day 8   $150.00  ->  two:   $20.00 + $130.00
    day 20  $200.00  ->  five:  $25.00 + $5.00 + $10.00 + $50.00 + $110.00

So matching is a subset-sum: find the set of unmatched Walmart bank rows near
the order date that sums to the order total exactly. One-row matches are simply
the k=1 case, tried first and separately so a simple order can claim its row
before any multi-row combination is allowed to take it.

**The governing rule is still: never guess.** Subset-sum is far more willing to
find *an* answer than exact pairing is — with enough small charges in a window,
several different subsets can hit the same total. So a match is recorded ONLY
when the solution is UNIQUE. Two ways to sum to the same total means we do not know
which was the order, and a wrong basket of items attributed to a charge is worse
than an unexplained charge.

**Channel matters too.** Walmart puts online orders and in-store receipts in one
history, and they post under different merchant strings — `WALMART.COM` versus
`WM SUPERCENTER`. Without that filter, amount and date alone would happily
attach a grocery pickup's item list to a same-total Supercenter run.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from itertools import combinations

#: Bank-side merchant patterns, split by where the purchase happened.
#: Verified against the ledger: these three patterns match every Walmart charge
#: in it and nothing else. Sam's Club is deliberately absent — it is a separate
#: site with a separate login, so walmart.com order history structurally cannot
#: explain it, and counting it would report ~9% of "Walmart" spend as
#: unexplained forever. A missing number beats a dishonest one.
ONLINE_LIKE = ("WALMART%",)
INSTORE_LIKE = ("WAL MART%", "WM SUPERC%")
MERCHANT_LIKE = ONLINE_LIKE + INSTORE_LIKE

#: How far a bank row may sit from the order date. Wider than the Amazon
#: connector's ±3 because this window starts at the ORDER, not at a charge:
#: Walmart bills as each part ships or is picked, and the observed spread runs
#: to three days after the order with the tail of a split settlement later
#: still. Asymmetric for the same reason — a charge before the order is not a
#: settlement of it.
DAYS_BEFORE = 1
DAYS_AFTER = 10

#: Bounds on the search. A window holding more rows than this is not evidence,
#: it is a haystack: the number of subsets grows exponentially, and so does the
#: chance that two of them coincidentally hit the same total. Beyond these the
#: order is left unmatched and reported, which is the honest outcome.
MAX_CANDIDATES = 14
MAX_SUBSET = 6


def patterns_for(channel: str | None) -> tuple[str, ...]:
    """Which merchant patterns an order of this channel may match.

    An unknown channel falls back to all of them. That is the honest default:
    refusing to match what we cannot classify would drop real reconciliations,
    and the sum still has to come out exact.
    """
    if channel == "online":
        return ONLINE_LIKE
    if channel == "in-store":
        return INSTORE_LIKE
    return MERCHANT_LIKE


def _candidates(conn: sqlite3.Connection, order: sqlite3.Row) -> list[sqlite3.Row]:
    """Unmatched Walmart-looking bank rows in this order's settlement window."""
    pats = patterns_for(order["channel"])
    like = " OR ".join("t.merchant_norm LIKE ?" for _ in pats)
    return conn.execute(
        f"""SELECT t.txn_id, t.posted_date, t.merchant_norm, t.amount_cents
              FROM transactions t
             WHERE t.status = 'posted'
               AND t.amount_cents < 0
               AND ({like})
               AND julianday(t.posted_date) - julianday(?) BETWEEN ? AND ?
               AND t.txn_id NOT IN (SELECT txn_id FROM walmart_matches)
          ORDER BY t.posted_date, t.txn_id""",
        (*pats, order["order_placed_date"], -DAYS_BEFORE, DAYS_AFTER)).fetchall()


def solve(candidates: list, target: int, *, max_subset: int = MAX_SUBSET) -> list | None:
    """The unique subset of `candidates` summing to `target`, or None.

    Returns None both when nothing sums to the target and when SEVERAL things
    do — the caller cannot act on either, and collapsing them here keeps the
    "never guess" rule in one place. `ambiguous_solutions` reports the second
    case separately for the operator.

    Smallest subsets first, so a single row that matches exactly is preferred
    over a coincidental pair summing to the same figure.
    """
    found = _solutions(candidates, target, max_subset, limit=2)
    return found[0] if len(found) == 1 else None


def _solutions(candidates: list, target: int, max_subset: int,
               *, limit: int = 2) -> list[list]:
    """Up to `limit` distinct subsets summing to target, smallest first."""
    if len(candidates) > MAX_CANDIDATES:
        return []
    out: list[list] = []
    for k in range(1, min(max_subset, len(candidates)) + 1):
        for combo in combinations(candidates, k):
            if sum(c["amount_cents"] for c in combo) == target:
                out.append(list(combo))
                if len(out) >= limit:
                    return out
    return out


def _unmatched_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Orders with a usable total and no bank rows attached yet.

    A cancelled order is skipped: nothing settled, so there is nothing to find,
    and letting it into the search only adds a target that can steal rows from
    an order that really was charged.
    """
    return conn.execute(
        """SELECT order_number, order_placed_date, grand_total_cents, channel
             FROM walmart_orders
            WHERE cancelled = 0
              AND grand_total_cents IS NOT NULL AND grand_total_cents > 0
              AND order_number NOT IN
                  (SELECT order_number FROM walmart_matches)
         ORDER BY order_placed_date, order_number""").fetchall()


def _record(conn: sqlite3.Connection, order_number: str, txns: list,
            confidence: str, method: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for t in txns:
        conn.execute(
            "INSERT OR IGNORE INTO walmart_matches "
            "(order_number, txn_id, confidence, method, matched_at) "
            "VALUES (?,?,?,?,?)",
            (order_number, t["txn_id"], confidence, method, now))


def run(conn: sqlite3.Connection) -> dict:
    """Match every order whose settlement is unambiguous. Returns a summary.

    Two passes, and the order is load-bearing. A single bank row equal to the
    order total is the strongest evidence there is, so every order gets the
    chance to claim one before any multi-row combination is considered. Run as
    one pass, a three-row sum could consume the exact row that a different order
    needed, leaving both wrong — the loose one mismatched and the exact one
    orphaned.
    """
    exact = split = 0
    ambiguous: list[dict] = []

    # Pass 1 — a single bank row for the whole order total.
    for o in _unmatched_orders(conn):
        cands = _candidates(conn, o)
        hits = [c for c in cands if c["amount_cents"] == -o["grand_total_cents"]]
        if len(hits) == 1:
            _record(conn, o["order_number"], hits, "exact", "single")
            exact += 1

    # Pass 2 — a set of rows summing to it.
    for o in _unmatched_orders(conn):
        cands = _candidates(conn, o)
        target = -o["grand_total_cents"]
        found = _solutions(cands, target, MAX_SUBSET, limit=2)
        if len(found) == 1:
            _record(conn, o["order_number"], found[0], "split",
                    f"sum-of-{len(found[0])}")
            split += 1
        elif len(found) > 1:
            # Several different sets of charges hit this total. Which one was
            # the order is not knowable from the ledger, so it is reported
            # rather than picked.
            ambiguous.append({
                "order_number": o["order_number"],
                "order_placed_date": o["order_placed_date"],
                "amount_cents": o["grand_total_cents"],
                "channel": o["channel"],
                "solutions": [[dict(c) for c in s] for s in found],
            })

    return {"exact": exact, "split": split, "ambiguous": ambiguous,
            "matched": exact + split}


def confirm(conn: sqlite3.Connection, order_number: str, txn_ids: list[int]) -> None:
    """Record a human-chosen settlement for an ambiguous order."""
    _record(conn, order_number, [{"txn_id": t} for t in txn_ids],
            "manual", "confirmed")


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
        "split_settlements": split_settlements(conn, month),
    }


def split_settlements(conn: sqlite3.Connection, month: str | None = None) -> dict:
    """How many matched orders settled as more than one bank charge.

    Worth stating rather than burying: it is the fact that makes this connector
    different from the Amazon one, and a reader comparing "orders" against
    "charges" on the same page needs to know the two do not correspond.
    """
    where_month = " AND t.posted_date LIKE ?" if month else ""
    params: tuple = (f"{month}-%",) if month else ()
    rows = conn.execute(
        f"""SELECT m.order_number, COUNT(*) AS n
              FROM walmart_matches m
              JOIN transactions t ON t.txn_id = m.txn_id
             WHERE 1=1{where_month}
          GROUP BY m.order_number""", params).fetchall()
    multi = [r for r in rows if r["n"] > 1]
    return {"orders": len(rows), "split_orders": len(multi),
            "max_parts": max((r["n"] for r in rows), default=0)}


def horizon(conn: sqlite3.Connection) -> dict:
    """How far back reconciliation actually reaches, and what lies beyond it.

    Without this, a low coverage number is unreadable: it looks like a data
    quality problem when it is a window problem. Charges older than the earliest
    stored order cannot be matched no matter what.

    Returns ``{earliest, pre_count, pre_cents, has_backlog}``; `earliest` is
    None when nothing is stored at all.
    """
    row = conn.execute(
        "SELECT MIN(order_placed_date) AS d FROM walmart_orders").fetchone()
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
    """Item lines behind the matched Walmart orders, largest first.

    One row per item line. The join goes order → items, and the bank side is
    collapsed with MIN/GROUP BY: an order matched to five bank rows would
    otherwise repeat every one of its items five times.
    """
    where_month = " AND t.posted_date LIKE ?" if month else ""
    params: tuple = (f"{month}-%",) if month else ()
    rows = conn.execute(
        f"""SELECT i.title, i.product_id, i.quantity, i.line_price_cents,
                   i.seller, i.category, o.order_number, o.channel,
                   MIN(t.posted_date) AS posted_date, MIN(t.txn_id) AS txn_id,
                   COUNT(DISTINCT t.txn_id) AS charge_parts
              FROM walmart_matches m
              JOIN transactions t   ON t.txn_id = m.txn_id
              JOIN walmart_orders o ON o.order_number = m.order_number
              JOIN walmart_items i  ON i.order_number = o.order_number
             WHERE 1=1{where_month}
          GROUP BY i.item_id
          ORDER BY i.line_price_cents DESC""", params).fetchall()
    return [dict(r) for r in rows]
