"""Resolve import conflicts (design §4.3 — never auto-resolve, never silently
drop or double-count).

near_duplicate (a quarantined incoming row, status='conflict'):
  - keep_one      : drop the incoming, existing stays posted
  - mark_distinct : promote incoming to posted (BOTH count — I8e)
  - merge         : posted-is-truth — drop existing, promote incoming

fitid_collision (incoming was never inserted; only the conflict recorded):
  - keep_one        : keep existing unchanged
  - accept_incoming : update existing row's amount/date to the incoming values

Any resolution that changes the posted set bumps the generation and rebuilds
agent.db (F2). A user-assigned category is preserved (I8c).
"""
from __future__ import annotations

from . import db

NEAR_DUP_ACTIONS = {"keep_one", "mark_distinct", "merge"}
COLLISION_ACTIONS = {"keep_one", "accept_incoming"}


class ReconcileError(ValueError):
    pass


def list_open() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM import_conflicts WHERE resolved = 0 ORDER BY conflict_id"
        ).fetchall()
    return [dict(r) for r in rows]


def resolve(conflict_id: int, action: str) -> dict:
    changed = False
    with db.connect() as conn:
        c = conn.execute(
            "SELECT * FROM import_conflicts WHERE conflict_id = ? AND resolved = 0",
            (conflict_id,),
        ).fetchone()
        if not c:
            raise ReconcileError(f"no open conflict {conflict_id}")

        if c["kind"] == "near_duplicate":
            changed = _resolve_near_dup(conn, c, action)
        elif c["kind"] == "fitid_collision":
            changed = _resolve_collision(conn, c, action)
        else:  # pragma: no cover - schema-constrained
            raise ReconcileError(f"unknown conflict kind {c['kind']!r}")

        conn.execute("UPDATE import_conflicts SET resolved = 1 WHERE conflict_id = ?", (conflict_id,))

    return {"conflict_id": conflict_id, "action": action, "rebuilt": changed}


def _row_exists(conn, txn_id) -> bool:  # noqa: ANN001
    """True if a transactions row with this txn_id is present (None -> False)."""
    if txn_id is None:
        return False
    return conn.execute(
        "SELECT 1 FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone() is not None


def _resolve_near_dup(conn, c, action) -> bool:  # noqa: ANN001
    if action not in NEAR_DUP_ACTIONS:
        raise ReconcileError(f"near_duplicate action must be one of {sorted(NEAR_DUP_ACTIONS)}")
    incoming, existing = c["incoming_txn_id"], c["existing_txn_id"]

    # Re-check the referenced rows at resolution time. A prior resolution of a
    # sibling conflict (multiple near-dups can point at the same existing row,
    # and ON DELETE SET NULL nulls the FK) may already have consumed them.
    if not _row_exists(conn, incoming):
        return False  # stale conflict: the incoming row is already gone — no-op.

    if action == "keep_one":
        conn.execute("DELETE FROM transactions WHERE txn_id = ?", (incoming,))
    elif action == "mark_distinct":
        conn.execute("UPDATE transactions SET status = 'posted' WHERE txn_id = ?", (incoming,))
    elif action == "merge":
        if _row_exists(conn, existing):
            # posted-is-truth: carry category, drop the existing, promote incoming.
            _preserve_category(conn, src=existing, dst=incoming)
            conn.execute("DELETE FROM transactions WHERE txn_id = ?", (existing,))
            conn.execute("UPDATE transactions SET status = 'posted' WHERE txn_id = ?", (incoming,))
        else:
            # The canonical row was already established by an earlier merge of a
            # sibling conflict (existing is NULL/deleted). This incoming is a
            # redundant copy of the same charge — collapse it; do NOT promote
            # (that would double-count, violating I8e).
            conn.execute("DELETE FROM transactions WHERE txn_id = ?", (incoming,))
    return True


def _resolve_collision(conn, c, action) -> bool:  # noqa: ANN001
    if action not in COLLISION_ACTIONS:
        raise ReconcileError(f"fitid_collision action must be one of {sorted(COLLISION_ACTIONS)}")
    if action == "keep_one":
        return False  # keep existing unchanged
    # accept_incoming: adopt the changed amount/date on the existing row.
    conn.execute(
        "UPDATE transactions SET amount_cents = ?, posted_date = ? WHERE txn_id = ?",
        (c["incoming_amount_cents"], c["incoming_posted_date"], c["existing_txn_id"]),
    )
    return True


def _preserve_category(conn, src: int, dst: int) -> None:  # noqa: ANN001
    """Carry a user/rule-assigned category from src to dst when dst lacks one (I8c)."""
    s = conn.execute("SELECT category, category_source FROM transactions WHERE txn_id = ?", (src,)).fetchone()
    d = conn.execute("SELECT category FROM transactions WHERE txn_id = ?", (dst,)).fetchone()
    if s and s["category"] and (not d or d["category"] in (None, "Uncategorized")):
        conn.execute(
            "UPDATE transactions SET category = ?, category_source = ? WHERE txn_id = ?",
            (s["category"], s["category_source"], dst),
        )
