"""Merchant normalization: collapse a vendor's many bank-statement spellings to one
canonical identity, retroactively + reversibly (design 2026-06-12).

DETERMINISTIC, offline (no AI): apply_aliases() sets canonical_merchant for each
posted Subscriptions txn from the active aliases (built-in brand rules + cached
manual), and re-derives each Subscriptions subcategory to its canonical so
spelling-variants of one service collapse to a single budgetable sub. Non-sub rows
keep canonical_merchant NULL (a brand token must not collapse distinct retail/travel
vendors in reports). Reversible via a snapshot (undo_last). confirm() lets a user
alias an explicit group → canonical, then re-applies in the same undo-able batch.
"""
from __future__ import annotations

from datetime import date

from . import db, merchants
from .categorize.manual import friendly_name


def _next_batch_id(conn) -> int:
    r = conn.execute("SELECT COALESCE(MAX(batch_id), 0) + 1 AS b FROM normalize_changes").fetchone()
    return int(r["b"])


def _reconcile_subscription_budgets(conn, aliases, real_subs: set[str]) -> int:
    """Collapse ORPHANED Subscriptions sub-budgets (zero-spend variant names like
    'Audible Amzn', 'Anthropic Claude') into their canonical, transaction-backed
    subscription, and set the survivor's budget from the canonical's actual average
    monthly spend (design 2026-06-13). Operates on the passed `conn` (no nested
    connection). Returns the number of orphan sub-budgets merged.

    A budget is a roll-up SOURCE only when its subcategory has NO posted transactions
    (`sub ∉ real_subs`) and its `canonical_merchant` maps to a subcategory that DOES
    (`canon ∈ real_subs`). A subcategory with spend is never touched, so deliberately
    split subs (Apple Music / Apple TV → both 'Apple') are preserved."""
    from . import budgets as budgets_mod
    active = budgets_mod.active_limits(conn)
    sub_budgets = [s for (c, s), _ in active.items() if c == "Subscriptions" and s]
    targets: dict[str, list[str]] = {}
    for s in sub_budgets:
        if s in real_subs:
            continue   # has spend — never a source
        canon = merchants.canonical_merchant(s.upper(), aliases)
        if canon != s and canon in real_subs:
            targets.setdefault(canon, []).append(s)

    today = date.today().isoformat()
    merged = 0
    for canon, orphans in targets.items():
        # Survivor budget = canon's all-history average over the months it was charged
        # (> 0 because canon ∈ real_subs; works for monthly AND annual cadences).
        row = conn.execute(
            "SELECT COALESCE(SUM(-amount_cents),0) AS total, "
            "COUNT(DISTINCT substr(posted_date,1,7)) AS months FROM transactions "
            "WHERE status='posted' AND category='Subscriptions' AND subcategory=? AND amount_cents<0",
            (canon,)).fetchone()
        survivor = round(int(row["total"]) / max(1, int(row["months"])))
        if survivor > 0:
            up = conn.execute(
                "UPDATE budgets SET limit_cents=?, created_at=? WHERE category='Subscriptions' "
                "AND subcategory=? AND effective_from=?", (survivor, db.now_iso(), canon, today))
            if up.rowcount == 0:
                conn.execute(
                    "INSERT INTO budgets (category, subcategory, limit_cents, effective_from, created_at) "
                    "VALUES ('Subscriptions', ?, ?, ?, ?)", (canon, survivor, today, db.now_iso()))
        for orphan in orphans:
            conn.execute(
                "DELETE FROM budgets WHERE category='Subscriptions' AND subcategory=?", (orphan,))
            merged += 1
    return merged


def apply_aliases(batch: int | None = None) -> dict:
    """Set canonical_merchant for posted Subscriptions txns from the active aliases
    (non-sub rows stay NULL so they group by their own merchant_norm in reports), and
    re-derive auto-derived Subscriptions subcategories to the canonical. Records a
    reversible snapshot; idempotent (a row already at its canonical is a no-op).
    A caller may pass a pre-allocated `batch` (so alias-add `new_pattern` rows and
    the txn changes land in ONE undo-able batch); default allocates a new one.
    Returns {batch_id, txns_updated}."""
    with db.connect() as conn:
        merchants.seed_builtin_aliases(conn)
        aliases = merchants.active_aliases(conn)
        rows = conn.execute(
            "SELECT txn_id, merchant_norm, category, subcategory, canonical_merchant "
            "FROM transactions WHERE status='posted'").fetchall()
        # Subcategories that are transaction-backed BEFORE this batch's re-derivation —
        # the roll-up's "real subscriptions". Captured pre-loop so a budget only collapses
        # once its transactions were canonicalized in an EARLIER, committed batch (undo-safe).
        real_subs = {r["subcategory"] for r in rows
                     if r["category"] == "Subscriptions" and r["subcategory"]}
        if batch is None:
            batch = _next_batch_id(conn)
        changed = 0
        for r in rows:
            # Stored column is scoped to Subscriptions rows: NULL for non-sub rows so
            # a brand alias never collapses distinct retail/travel vendors in reports.
            # A non-sub row carrying a STALE non-NULL canonical from a prior (wider)
            # apply is reset to NULL here — change-detection + snapshot below cover undo.
            canon_col = (merchants.canonical_alias(r["merchant_norm"], aliases)
                         if r["category"] == "Subscriptions" else None)
            display = merchants.canonical_merchant(r["merchant_norm"], aliases)  # alias or friendly name
            new_sub = r["subcategory"]
            # Re-derive a Subscriptions subcategory ONLY when it is blank or an
            # AUTO-derived value (the friendly name, already the display, or equal to
            # the row's CURRENT canonical_merchant — i.e. an auto-derived sub from a
            # prior apply that a name CORRECTION must now follow) — never a custom
            # user rename. A user rename will not equal the canonical_merchant column.
            if r["category"] == "Subscriptions" and (
                    not r["subcategory"]
                    or r["subcategory"] == friendly_name(r["merchant_norm"])
                    or r["subcategory"] == display
                    or (r["canonical_merchant"] and r["subcategory"] == r["canonical_merchant"])):
                new_sub = display
            if canon_col != (r["canonical_merchant"] or None) or new_sub != r["subcategory"]:
                conn.execute(
                    "INSERT INTO normalize_changes (batch_id, txn_id, old_canonical, "
                    "old_subcategory, created_at) VALUES (?,?,?,?,?)",
                    (batch, r["txn_id"], r["canonical_merchant"], r["subcategory"], db.now_iso()))
                conn.execute("UPDATE transactions SET canonical_merchant=?, subcategory=? WHERE txn_id=?",
                             (canon_col, new_sub, r["txn_id"]))
                changed += 1
        # Reconcile orphaned Subscriptions sub-budgets to canonical (design 2026-06-13).
        budgets_merged = _reconcile_subscription_budgets(conn, aliases, real_subs)
    return {"batch_id": batch if changed else None, "txns_updated": changed,
            "budgets_merged": budgets_merged}


def _alias_group(conn, canonical: str, members: list[str], source: str, batch: int) -> list[str]:
    """Add an alias for each member merchant_norm -> canonical, using the full
    merchant_norm as the pattern. A single-word pattern matches a whole token of a
    merchant_norm; a multi-word pattern matches only when ALL of its tokens are tokens
    of the merchant_norm (token-subset containment — see merchants.canonical_alias), so
    'QORP A' will not collapse the unrelated 'QORP ABC INC'. Returns the patterns
    actually inserted so a caller can record `new_pattern` snapshot rows for undo."""
    added: list[str] = []
    for m in members:
        p = (m or "").strip().upper()
        if len(p) < merchants.MIN_PATTERN_LEN:
            continue
        # A built-in brand token already maps this vendor broadly (token-anywhere).
        # Re-confirming it is redundant and must never downgrade its source to
        # manual/llm (which would shrink matching to single-token-exact) nor record
        # it as a `new_pattern` (which would let undo_last DELETE the built-in row).
        existing = conn.execute(
            "SELECT source FROM merchant_aliases WHERE pattern = ?", (p,)).fetchone()
        if existing is not None and existing["source"] == "builtin":
            continue
        conn.execute(
            "INSERT INTO merchant_aliases (pattern, canonical, source, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(pattern) DO UPDATE SET canonical=excluded.canonical, source=excluded.source",
            (p, canonical.strip(), source, db.now_iso()))
        # Only record patterns this batch NEWLY created as undo-deletable `new_pattern`
        # rows. A re-confirm/correction of a pre-existing manual/llm alias (the normal
        # "confirm a canonical, then CORRECT it" flow) must NOT be recorded — undoing
        # the correction restores the txn snapshot but must leave the shared alias in
        # place, else the next apply finds no alias and silently de-collapses the
        # canonical to NULL / the subcategory to the raw friendly name.
        if existing is None:
            added.append(p)
    return added


def _record_added_patterns(conn, batch: int, patterns: list[str]) -> None:
    """Snapshot each llm/manual pattern this batch added so `undo_last` can remove it
    from merchant_aliases (durable undo — otherwise the next apply re-collapses). Uses
    txn_id=0 as a sentinel (real txn PKs are >=1); the txn-restore loop skips these."""
    for p in patterns:
        conn.execute(
            "INSERT INTO normalize_changes (batch_id, txn_id, old_canonical, "
            "old_subcategory, new_pattern, created_at) VALUES (?,0,NULL,NULL,?,?)",
            (batch, p, db.now_iso()))


def confirm(canonical: str, members: list[str], source: str = "manual") -> dict:
    """Confirm a proposed/edited group: alias each member to `canonical`, then apply.
    The alias-add + apply share ONE batch and the added patterns are snapshotted, so a
    single `undo_last()` reverts both the txn changes AND the alias (durable undo)."""
    canonical = (canonical or "").strip()
    canonical = canonical[:64]  # defensive bound; normal vendor names are far under 64 chars
    # Drop non-strings (a malformed `members:[1,2,3]` must not crash on .strip()).
    members = [m for m in (members or []) if isinstance(m, str) and m.strip()]
    if not canonical or not members:
        raise ValueError("canonical name and at least one member required")
    with db.connect() as conn:
        batch = _next_batch_id(conn)
        added = _alias_group(conn, canonical, members, source, batch)
        _record_added_patterns(conn, batch, added)
    return apply_aliases(batch=batch)


def undo_last() -> dict:
    """Reverse the most recent apply batch: restore each row's prior
    canonical_merchant + subcategory, REMOVE any llm/manual aliases this batch added
    (durable undo — otherwise the next apply silently re-collapses), and drop the
    batch's snapshot. Built-in aliases are seeded separately and never removed here."""
    with db.connect() as conn:
        row = conn.execute("SELECT MAX(batch_id) AS b FROM normalize_changes").fetchone()
        batch = row["b"]
        if batch is None:
            return {"undone": False, "reason": "nothing to undo"}
        changes = conn.execute(
            "SELECT txn_id, old_canonical, old_subcategory, new_pattern "
            "FROM normalize_changes WHERE batch_id=?", (batch,)).fetchall()
        restored = 0
        for c in changes:
            if c["txn_id"] and c["txn_id"] > 0:          # real txn snapshot (skip pattern sentinels)
                conn.execute("UPDATE transactions SET canonical_merchant=?, subcategory=? WHERE txn_id=?",
                             (c["old_canonical"], c["old_subcategory"], c["txn_id"]))
                restored += 1
            if c["new_pattern"]:                          # only llm/manual patterns are recorded here
                conn.execute("DELETE FROM merchant_aliases WHERE pattern=?", (c["new_pattern"],))
        conn.execute("DELETE FROM normalize_changes WHERE batch_id=?", (batch,))
    return {"undone": True, "batch_id": int(batch), "restored": restored}
