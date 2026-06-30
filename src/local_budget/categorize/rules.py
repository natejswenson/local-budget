"""Deterministic, offline rules categorizer (design §3 / §4.2 / I9 / I9b).

Matches `merchant_norm` against `category_rules` by priority (lower wins). The
Investments builtin rule is pinned to the LOWEST priority so it wins over the
Transfer rule (S5), and Investments counts as spend even when TRNTYPE=XFER (the
spend/non-spend split lives in `categories.py`, where Investments is NOT a
non-spend category).

Transfer is assigned ONLY on strong, named evidence — an explicit card-payment /
transfer payee-string rule. A weak same-amount opposite-sign coincidence is
NEVER auto-Transfer (I9b); the conservative bias is "leave it as spend
(visible)" because undercount is worse than overcount.
"""
from __future__ import annotations

import sqlite3

from .. import categories, db

# (pattern, category, priority, source). Lower priority wins.
# Investments pinned to priority 1 so it beats the Transfer rule (S5).
# Patterns are matched as substrings of the NORMALIZED merchant key (≤3 words),
# so keep them ≤3 words and aligned to the normalized form.
BUILTIN_RULES: tuple[tuple[str, str, int], ...] = (
    ("529", "Investments", 1),
    ("WALMART", "Groceries", 50),
    ("VOLT", "Dining Out", 50),
    ("SHELL", "Transportation", 50),
    ("SPEEDWAY", "Transportation", 50),
    ("MOBIL", "Transportation", 50),
    ("EXXON", "Transportation", 50),
    ("CASEY", "Transportation", 50),
    # Strong, named transfer evidence (NOT a weak coincidence — S3).
    ("WF CREDIT CARD", "Transfer", 60),
    ("ONLINE TRANSFER", "Transfer", 60),
    ("CREDIT CARD AUTOPAY", "Transfer", 60),
    ("MOBILE DEPOSIT", "Income", 60),
)


def seed_builtin_rules(conn: sqlite3.Connection) -> None:
    """Insert builtin rules once (idempotent on (pattern, source))."""
    existing = {
        (r["pattern"], r["source"])
        for r in conn.execute("SELECT pattern, source FROM category_rules").fetchall()
    }
    # Don't re-seed builtin rules whose category the user has removed (hidden) — else a
    # removed builtin (e.g. Dining Out/Transportation) would resurrect on the next import.
    hidden = categories.hidden_categories(conn=conn)
    for pattern, category, priority in BUILTIN_RULES:
        if (pattern, "builtin") in existing or category in hidden:
            continue
        assert category in categories.CATEGORIES, category
        conn.execute(
            "INSERT INTO category_rules (pattern, category, priority, source, created_at) "
            "VALUES (?, ?, ?, 'builtin', ?)",
            (pattern, category, priority, db.now_iso()),
        )


def categorize(merchant_norm: str, txn_type: str | None, amount_cents: int,
               conn: sqlite3.Connection) -> tuple[str, str | None, str | None]:
    """Return (category, subcategory, category_source) for one transaction.

    Rule match wins (lowest priority first). Otherwise: a positive amount with
    no rule defaults to Income (deterministic — never the LLM, F4); a negative
    amount with no rule is Uncategorized (triggers the LLM fallback later).
    """
    m = (merchant_norm or "").upper()
    rows = conn.execute(
        "SELECT pattern, category, subcategory FROM category_rules "
        "ORDER BY priority ASC, rule_id ASC"
    ).fetchall()
    for r in rows:
        if r["pattern"].upper() in m:
            return r["category"], r["subcategory"], "rule"

    if amount_cents > 0:
        return categories.INCOME, None, "rule"
    return categories.UNCATEGORIZED, None, None
