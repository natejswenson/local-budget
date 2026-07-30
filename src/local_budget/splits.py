"""Transaction splits — one bank charge apportioned across several categories.

The ledger stays the record of what the bank said. A split never edits the
imported row; it reapportions it, and every category aggregate reads the
`effective_txns` view rather than `transactions` so the apportionment is
applied consistently in one place.

**The invariant, which is the whole module:** a transaction's splits sum to its
amount, exactly, in integer cents. A violation does not produce a slightly-off
report — it invents or destroys money in every total downstream, silently and
forever. So it is enforced on write (`apply` refuses and writes nothing) and
auditable at any time (`verify`).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from . import categories


class SplitError(ValueError):
    """A split set that would break the invariant, or an unknown category."""


def allocate(txn_amount_cents: int, line_amounts: list[int]) -> list[int]:
    """Scale `line_amounts` proportionally so they sum to `txn_amount_cents`.

    Item prices do not sum to what the card was charged — discounts, promotions
    and gift cards land between the two, and tax pushes the other way. Scaling
    by `charge / line_total` keeps every cent attached to something real, with
    no synthetic "adjustment" line appearing in anyone's category totals.

    Rounding is the part that matters. Scaling each line independently leaves a
    remainder of a cent or two that has to go somewhere, so **the largest line
    absorbs it**: biggest line, smallest relative distortion, and the result
    sums to the target by construction rather than by luck.

    Returns amounts in the same order and sign as the input. Raises if the
    lines sum to zero, which cannot be scaled to anything.
    """
    if not line_amounts:
        raise SplitError("no lines to allocate")
    total = sum(line_amounts)
    if total == 0:
        raise SplitError("lines sum to zero — nothing to scale")

    ratio = Decimal(txn_amount_cents) / Decimal(total)
    scaled = [int((Decimal(a) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
              for a in line_amounts]

    # Push the rounding drift onto the largest line, by magnitude.
    drift = txn_amount_cents - sum(scaled)
    if drift:
        biggest = max(range(len(scaled)), key=lambda i: abs(scaled[i]))
        scaled[biggest] += drift
    return scaled


def _validate(conn: sqlite3.Connection, txn_id: int,
              lines: list[dict]) -> int:
    """Shared precondition check. Returns the parent transaction's amount."""
    if not lines:
        raise SplitError("a split needs at least one line")
    row = conn.execute(
        "SELECT amount_cents FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if row is None:
        raise SplitError(f"no transaction {txn_id}")
    parent = int(row["amount_cents"])

    known = categories.all_categories()
    for ln in lines:
        cat = (ln.get("category") or "").strip()
        if not cat:
            raise SplitError("every split line needs a category")
        if cat not in known:
            raise SplitError(f"unknown category {cat!r} — add it first")

    got = sum(int(ln["amount_cents"]) for ln in lines)
    if got != parent:
        raise SplitError(
            f"splits sum to {got} but transaction {txn_id} is {parent} "
            f"(off by {got - parent}). Refusing to write — a split set that does "
            f"not sum to its parent invents or destroys money in every report.")
    return parent


def apply(conn: sqlite3.Connection, txn_id: int, lines: list[dict],
          source: str = "manual") -> int:
    """Replace `txn_id`'s splits with `lines`. Returns the number written.

    Each line is ``{"amount_cents", "category", ["subcategory"], ["item_ref"],
    ["note"]}``. Validated in full BEFORE anything is deleted, so a rejected
    split leaves the previous state intact rather than clearing it and failing.
    """
    _validate(conn, txn_id, lines)
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("DELETE FROM txn_splits WHERE txn_id = ?", (txn_id,))
    for ln in lines:
        conn.execute(
            "INSERT INTO txn_splits (txn_id, amount_cents, category, subcategory, "
            " source, item_ref, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (txn_id, int(ln["amount_cents"]), ln["category"].strip(),
             (ln.get("subcategory") or None), source,
             (ln.get("item_ref") or None), (ln.get("note") or None), now))
    return len(lines)


def unsplit(conn: sqlite3.Connection, txn_id: int) -> int:
    """Drop a transaction's splits; it reverts to its own category. Returns rows
    removed. Reversibility is a requirement, not a convenience — an allocation
    is a judgment call and judgment calls get revised."""
    cur = conn.execute("DELETE FROM txn_splits WHERE txn_id = ?", (txn_id,))
    return cur.rowcount


def verify(conn: sqlite3.Connection) -> list[dict]:
    """Every split transaction whose parts don't sum to its whole.

    Always empty in a healthy database. It exists because the invariant is the
    one thing here that can fail silently: nothing errors, reports just quietly
    stop adding up. Cheap enough to run on demand and after any migration.
    """
    rows = conn.execute(
        """SELECT t.txn_id, t.posted_date, t.merchant_norm,
                  t.amount_cents AS txn_cents,
                  SUM(s.amount_cents) AS split_cents,
                  COUNT(*) AS n_splits
             FROM txn_splits s
             JOIN transactions t ON t.txn_id = s.txn_id
         GROUP BY s.txn_id
           HAVING SUM(s.amount_cents) <> t.amount_cents""").fetchall()
    return [dict(r) | {"drift_cents": int(r["split_cents"]) - int(r["txn_cents"])}
            for r in rows]


def for_txn(conn: sqlite3.Connection, txn_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT split_id, amount_cents, category, subcategory, source, item_ref, note "
        "FROM txn_splits WHERE txn_id = ? ORDER BY ABS(amount_cents) DESC",
        (txn_id,)).fetchall()]


def list_split_txns(conn: sqlite3.Connection, month: str | None = None) -> list[dict]:
    """Split transactions in scope, each with its parts — so a total that moved
    is always traceable to the split that moved it."""
    where = " AND t.posted_date LIKE ?" if month else ""
    params: tuple = (f"{month}-%",) if month else ()
    parents = conn.execute(
        f"""SELECT DISTINCT t.txn_id, t.posted_date, t.merchant_norm,
                   t.amount_cents, t.category AS original_category
              FROM txn_splits s JOIN transactions t ON t.txn_id = s.txn_id
             WHERE 1=1{where}
          ORDER BY t.posted_date DESC, t.txn_id""", params).fetchall()
    return [dict(p) | {"splits": for_txn(conn, p["txn_id"])} for p in parents]
