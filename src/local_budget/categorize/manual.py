"""Manual merchant→category decisions saved as persistent rules ("memory").

A manual rule is pinned to a low priority number (wins over builtin/llm), so the
decision sticks for existing rows AND every future import of that merchant.
"""
from __future__ import annotations

from datetime import date

from .. import categories, db

MANUAL_PRIORITY = 5   # beats builtin (50/60) and llm (100); Investments (1) still wins


class CategorizeError(ValueError):
    pass


def set_merchant_category(merchant_norm: str, category: str, subcategory: str | None = None,
                          conn=None) -> int:
    """Pin `merchant_norm` to `category` (+ optional `subcategory`): replace any
    llm/manual rule with a manual one, recategorize that merchant's existing rows,
    and rebuild agent.db. Returns the number of transactions updated. Substring
    match (pinning "HARDWARE CO" also catches "HARDWARE CO 1465")."""
    if category not in categories.all_categories():
        raise CategorizeError(f"unknown category {category!r}")
    subcategory = (subcategory or "").strip() or None
    merchant_norm = merchant_norm.strip().upper()
    with db.writer(conn) as conn:
        conn.execute(
            "DELETE FROM category_rules WHERE pattern = ? AND source IN ('llm', 'manual')",
            (merchant_norm,),
        )
        conn.execute(
            "INSERT INTO category_rules (pattern, category, subcategory, priority, source, created_at) "
            "VALUES (?, ?, ?, ?, 'manual', ?)",
            (merchant_norm, category, subcategory, MANUAL_PRIORITY, db.now_iso()),
        )
        n = conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ?, category_source = 'manual' "
            "WHERE merchant_norm LIKE '%' || ? || '%'",
            (category, subcategory, merchant_norm),
        ).rowcount
    return n


def set_transaction_category(txn_id: int, category: str, subcategory: str | None = None,
                             conn=None) -> None:
    """Categorize a SINGLE transaction (a one-off, no rule) — for things like
    individual checks that each go to a different place. Rebuilds agent.db."""
    if category not in categories.all_categories():
        raise CategorizeError(f"unknown category {category!r}")
    subcategory = (subcategory or "").strip() or None
    with db.writer(conn) as conn:
        conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ?, category_source = 'manual' "
            "WHERE txn_id = ?",
            (category, subcategory, txn_id),
        )


def remove_category(name: str, merge_into: str, conn=None) -> dict:
    """Remove a spend category by MERGING it into `merge_into`: re-point ALL its
    transactions, category_rules and budgets to the target, then hide it from the
    vocabulary. Atomic (one connection, like `set_merchant_category`); rebuilds agent.db.
    Returns {moved_txns, moved_rules, merged_budget, summed_limit_cents}.

    Everything inside the txn uses the open `conn`; vocabulary writes go through the
    conn-aware `categories.mark_hidden` / `remove_custom` helpers (the no-arg settings
    helpers would open their OWN connection and deadlock against this one)."""
    from .. import budgets as budgets_mod
    name = " ".join(name.split()).strip()
    merge_into = " ".join(merge_into.split()).strip()
    spend = categories.spend_categories()
    if name in categories.PROTECTED:
        raise CategorizeError(f"cannot remove protected category {name!r}")
    if merge_into in categories.PROTECTED:
        raise CategorizeError(f"cannot merge into protected category {merge_into!r}")
    if name not in spend:
        raise CategorizeError(f"unknown spend category {name!r}")
    if merge_into not in spend:
        raise CategorizeError(f"unknown target category {merge_into!r}")
    if name == merge_into:
        raise CategorizeError("cannot merge a category into itself")

    today = date.today().isoformat()
    with db.writer(conn) as conn:
        active = budgets_mod.active_limits(conn)   # {(cat, sub): cents} current limits

        # 1) Transactions — ALL statuses (posted/conflict/...); subcategory preserved.
        moved_txns = conn.execute(
            "UPDATE transactions SET category = ?, category_source = 'manual' WHERE category = ?",
            (merge_into, name)).rowcount

        # 2) Category rules — re-point (keep source); dedupe on pattern collision so the
        # target never ends up with two rules for the same pattern.
        into_patterns = {r["pattern"] for r in conn.execute(
            "SELECT pattern FROM category_rules WHERE category = ?", (merge_into,)).fetchall()}
        moved_rules = 0
        for r in conn.execute(
                "SELECT rule_id, pattern FROM category_rules WHERE category = ?", (name,)).fetchall():
            if r["pattern"] in into_patterns:
                conn.execute("DELETE FROM category_rules WHERE rule_id = ?", (r["rule_id"],))
            else:
                conn.execute("UPDATE category_rules SET category = ? WHERE rule_id = ?",
                             (merge_into, r["rule_id"]))
                into_patterns.add(r["pattern"])
                moved_rules += 1

        # 3) Budgets — move/sum into a NEW today-dated active row (append-correct; never
        # UPDATE an older history row), then delete all of `name`'s budget rows.
        def _upsert_limit(sub, cents):
            up = conn.execute(
                "UPDATE budgets SET limit_cents = ?, created_at = ? WHERE category = ? "
                "AND IFNULL(subcategory,'') = IFNULL(?, '') AND effective_from = ?",
                (cents, db.now_iso(), merge_into, sub, today))
            if up.rowcount == 0:
                conn.execute(
                    "INSERT INTO budgets (category, subcategory, limit_cents, effective_from, created_at) "
                    "VALUES (?, ?, ?, ?, ?)", (merge_into, sub, cents, today, db.now_iso()))

        summed_limit_cents = None
        name_top = active.get((name, None))
        if name_top is not None:
            summed_limit_cents = (active.get((merge_into, None)) or 0) + name_top
            _upsert_limit(None, summed_limit_cents)
        for (c, s), cents in active.items():
            if c == name and s is not None:
                _upsert_limit(s, (active.get((merge_into, s)) or 0) + cents)
        merged_budget = conn.execute(
            "DELETE FROM budgets WHERE category = ?", (name,)).rowcount > 0

        # 4) Vocabulary — hide the merged-away category; drop it if it was custom.
        # Both writes reuse THIS transaction via the conn-aware categories helpers,
        # which own the settings-blob encoding (F-1: no inline json here).
        categories.mark_hidden(name, conn=conn)
        categories.remove_custom(name, conn=conn)

    return {"moved_txns": moved_txns, "moved_rules": moved_rules,
            "merged_budget": merged_budget, "summed_limit_cents": summed_limit_cents}


def checks_to_review() -> list[dict]:
    """Every transaction currently in the 'Checks' category, biggest first."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT txn_id, posted_date, amount_cents, merchant_norm FROM transactions "
            "WHERE category = 'Checks' AND status = 'posted' ORDER BY ABS(amount_cents) DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def set_merchant_subcategory(merchant_norm: str, subcategory: str | None) -> int:
    """Set just the subcategory for a merchant (keeps its category), updating its
    rule + rows. `subcategory=None`/'' clears it."""
    subcategory = (subcategory or "").strip() or None
    merchant_norm = merchant_norm.strip().upper()
    with db.connect() as conn:
        conn.execute(
            "UPDATE category_rules SET subcategory = ? WHERE pattern = ? AND source = 'manual'",
            (subcategory, merchant_norm),
        )
        n = conn.execute(
            "UPDATE transactions SET subcategory = ? WHERE merchant_norm LIKE '%' || ? || '%'",
            (subcategory, merchant_norm),
        ).rowcount
    return n


def rename_subcategory(category: str, old: str, new: str | None) -> int:
    """Rename a subcategory within a category; renaming to an existing name MERGES
    them. Updates transactions, rules, and any budget. Returns rows updated."""
    new = (new or "").strip() or None
    with db.connect() as conn:
        n = conn.execute(
            "UPDATE transactions SET subcategory = ? WHERE category = ? AND subcategory = ?",
            (new, category, old)).rowcount
        conn.execute(
            "UPDATE category_rules SET subcategory = ? WHERE category = ? AND subcategory = ?",
            (new, category, old))
        conn.execute(
            "UPDATE budgets SET subcategory = ? WHERE category = ? AND subcategory = ?",
            (new, category, old))
    return n


_SUB_NOISE = {"BILL", "USA", "INC", "LLC", "CO", "SUBSCRIP", "SUBSCRIPTION", "SUBSCR",
              "RECURRING", "PAYMENT", "AMZN.COM", "COM", "HTTP", "HTTPS"}


def friendly_name(merchant_norm: str) -> str:
    """Best-effort human name for a merchant (default subcategory for a sub).
    e.g. 'NETFLIX.COM NETFLIX.COM' → 'Netflix'. Editable later."""
    raw = merchant_norm.replace(".COM", "").replace(".AI", "").replace("*", " ")
    out, seen = [], set()
    for w in raw.split():
        wu = w.strip(".").upper()
        if not wu or wu in _SUB_NOISE or wu in seen:
            continue
        seen.add(wu)
        out.append(wu)
    return (" ".join(out[:2]).title() or merchant_norm.title()) if out else merchant_norm.title()


def split_subscriptions(conn=None) -> int:
    """Give every Subscriptions merchant its own subcategory (the service name),
    so each can be budgeted individually. Only fills BLANK subcategories — your
    renamed ones are kept. Returns merchants updated.

    Reads the active aliases (built-ins are seeded at `init_schema`); does NOT
    re-seed `merchant_aliases` (an unlisted write table that would abort under the
    agent's write authorizer — design-gate S2)."""
    from .. import merchants
    with db.writer(conn) as conn:
        aliases = merchants.active_aliases(conn)
        rows = conn.execute(
            "SELECT DISTINCT merchant_norm FROM transactions "
            "WHERE category = 'Subscriptions' AND (subcategory IS NULL OR subcategory = '')"
        ).fetchall()
        n = 0
        for r in rows:
            sub = merchants.canonical_merchant(r["merchant_norm"], aliases)   # canonical vendor, collapses spellings
            conn.execute(
                "UPDATE transactions SET subcategory = ? "
                "WHERE category = 'Subscriptions' AND merchant_norm = ? AND (subcategory IS NULL OR subcategory = '')",
                (sub, r["merchant_norm"]),
            )
            conn.execute(
                "UPDATE category_rules SET subcategory = ? "
                "WHERE pattern = ? AND (subcategory IS NULL OR subcategory = '')",
                (sub, r["merchant_norm"]),
            )
            n += 1
    return n


def needs_review(limit: int = 500) -> list[dict]:
    """Distinct merchants the AI was NOT confident about (still Uncategorized),
    most-frequent first — the review queue."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT merchant_norm, COUNT(*) AS n, SUM(-amount_cents) AS spent "
            "FROM transactions WHERE status = 'posted' AND category = ? "
            "GROUP BY merchant_norm ORDER BY n DESC LIMIT ?",
            (categories.UNCATEGORIZED, limit),
        ).fetchall()
    return [{"merchant": r["merchant_norm"], "count": r["n"], "spent_cents": int(r["spent"] or 0)}
            for r in rows]


def top_merchants(limit: int = 50, only_uncertain: bool = False) -> list[dict]:
    """Top spend merchants by transaction count, with current category/source —
    drives the interactive review. `only_uncertain` keeps llm/Random/Uncategorized."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT merchant_norm, COUNT(*) AS n, SUM(-amount_cents) AS spent, "
            "category, category_source FROM transactions "
            "WHERE status = 'posted' AND amount_cents < 0 "
            "GROUP BY merchant_norm ORDER BY n DESC LIMIT ?",
            (limit if not only_uncertain else limit * 3,),
        ).fetchall()
    out = [{"merchant": r["merchant_norm"], "count": r["n"], "spent_cents": int(r["spent"] or 0),
            "category": r["category"], "source": r["category_source"]} for r in rows]
    if only_uncertain:
        out = [m for m in out if m["source"] in ("llm", None) or m["category"] in ("Random", "Uncategorized")]
    return out[:limit]
