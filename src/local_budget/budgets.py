"""Per-category and per-subcategory monthly budget limits + expected income.

Budgets are recurring monthly caps stored append-only with `effective_from`
history; the ONE active limit per (category, subcategory) is the latest
`effective_from`, tiebroken by the latest `budget_id` so a same-day re-edit never
produces two competing active rows (Budgets-tab inline editor). `set_limit` also
upserts within the same day to keep the history clean.

Expected monthly income (zero-based "to allocate" base) is a single scalar in
`settings` — it is not per-category, so a settings key is the correct grain.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from . import categories, db

EXPECTED_INCOME_KEY = "expected_monthly_income_cents"


class BudgetError(ValueError):
    pass


def set_limit(category: str, limit_cents: int, subcategory: str | None = None,
              effective_from: str | None = None, conn=None) -> None:
    """Set a monthly limit for a category, or for a (category, subcategory) such
    as ('Subscriptions', 'Netflix'). A same-day re-edit REPLACES the existing row
    for that key+date rather than appending a duplicate (which would tie for the
    active-row selection); earlier-dated history rows are untouched."""
    if not categories.is_spend(category):
        raise BudgetError(
            f"cannot budget structural category {category!r}; budgets apply only to "
            f"spend categories (not Income/Transfer/Uncategorized)")
    if category not in categories.all_categories():
        raise BudgetError(f"unknown category {category!r}; one of {sorted(categories.all_categories())}")
    if limit_cents <= 0:
        raise BudgetError("limit must be positive")
    subcategory = (subcategory or "").strip() or None
    eff = effective_from or date.today().isoformat()
    with db.writer(conn) as conn:
        cur = conn.execute(
            "UPDATE budgets SET limit_cents = ?, created_at = ? "
            "WHERE category = ? AND IFNULL(subcategory,'') = IFNULL(?, '') "
            "AND effective_from = ?",
            (limit_cents, db.now_iso(), category, subcategory, eff),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO budgets (category, subcategory, limit_cents, effective_from, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (category, subcategory, limit_cents, eff, db.now_iso()),
            )


def clear_limit(category: str, subcategory: str | None = None, conn=None) -> None:
    subcategory = (subcategory or "").strip() or None
    with db.writer(conn) as conn:
        if subcategory is None:
            conn.execute("DELETE FROM budgets WHERE category = ? AND subcategory IS NULL", (category,))
        else:
            conn.execute("DELETE FROM budgets WHERE category = ? AND subcategory = ?",
                         (category, subcategory))


def active_limits(conn: sqlite3.Connection,
                  as_of: str | None = None) -> dict[tuple[str, str | None], int]:
    """The ONE active limit per (category, subcategory): latest `effective_from`,
    tiebroken by latest `budget_id`. Single source of truth for list_limits,
    budget_status, and the Budgets overview — guarantees exactly one active row per
    key even if duplicate-date history rows exist.

    `as_of` (an ISO-date upper bound, e.g. '2026-01-31') restricts resolution to
    rows that were already effective then: only rows with `effective_from <= as_of`
    are considered (both for row selection AND the latest-effective_from/latest-
    budget_id tiebreak). A key with no row effective by `as_of` is omitted. When
    `as_of is None` behavior is unchanged (latest budget)."""
    bound = " AND b.effective_from <= ?" if as_of is not None else ""
    inner_bound = " AND b2.effective_from <= ?" if as_of is not None else ""
    params: tuple = (as_of, as_of) if as_of is not None else ()
    rows = conn.execute(
        "SELECT category, subcategory, limit_cents FROM budgets b "
        f"WHERE 1=1{bound} AND b.budget_id = ("
        "  SELECT b2.budget_id FROM budgets b2 "
        "  WHERE b2.category = b.category AND IFNULL(b2.subcategory,'') = IFNULL(b.subcategory,'')"
        f"{inner_bound} "
        "  ORDER BY b2.effective_from DESC, b2.budget_id DESC LIMIT 1)",
        params,
    ).fetchall()
    return {(r["category"], r["subcategory"]): int(r["limit_cents"]) for r in rows}


def list_limits() -> list[dict]:
    """Active limit per (category, subcategory) — one row per key."""
    with db.connect() as conn:
        active = active_limits(conn)
    return sorted(
        ({"category": cat, "subcategory": sub, "limit_cents": cents}
         for (cat, sub), cents in active.items()),
        key=lambda d: (d["category"], d["subcategory"] or ""),
    )


# ── expected monthly income (zero-based "to allocate" base) ──────────────────
def get_expected_income() -> int:
    """The user's expected monthly income in cents (0 if unset)."""
    v = db.get_setting(EXPECTED_INCOME_KEY)
    try:
        return max(0, int(v)) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def set_expected_income(cents: int, conn=None) -> None:
    if cents <= 0:
        raise BudgetError("expected income must be positive")
    db.set_setting(EXPECTED_INCOME_KEY, str(int(cents)), conn=conn)


def clear_expected_income() -> None:
    db.set_setting(EXPECTED_INCOME_KEY, "0")
