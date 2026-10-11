"""Correction dependencies, operated only inside the ledger apply transaction."""

import json

from ... import merchants
from ...categorize import rules


def categories(conn, fact, old=None):
    if old is not None and old["category_source"] == "manual":
        category, sub, source = old["category"], old["subcategory"], "manual"
    else:
        category, sub, source = rules.categorize(
            fact.merchant_norm, None, fact.amount_cents, conn
        )
    aliases = merchants.active_aliases(conn)
    canonical = (
        merchants.canonical_alias(fact.merchant_norm, aliases)
        if category == "Subscriptions"
        else None
    )
    if category == "Subscriptions" and not sub and source != "manual":
        sub = merchants.canonical_merchant(fact.merchant_norm, aliases)
    return category, sub, source, canonical


def invalidate(conn, old, fact, *, status):
    """Archive and remove invalid derived records; preserve valid manual splits."""
    txn_id = old["txn_id"]
    dependency_changed = status != "posted" or any(
        old[key] != getattr(fact, key)
        for key in ("posted_date", "amount_cents", "merchant_norm")
    )
    archived_matches = {}
    if dependency_changed:
        for name in ("amazon", "walmart"):
            table = f"{name}_matches"
            rows = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM {table} WHERE txn_id=?", (txn_id,)
                )
            ]
            if rows:
                archived_matches[name] = rows
                conn.execute(f"DELETE FROM {table} WHERE txn_id=?", (txn_id,))
    split_rows = [
        dict(r)
        for r in conn.execute("SELECT * FROM txn_splits WHERE txn_id=?", (txn_id,))
    ]
    amount = fact.amount_cents if fact is not None else old["amount_cents"]
    drop_all = status != "posted" or bool(
        split_rows and sum(r["amount_cents"] for r in split_rows) != amount
    )
    archived_splits = [
        r
        for r in split_rows
        if drop_all or (dependency_changed and r["source"] in {"amazon", "walmart"})
    ]
    for row in archived_splits:
        conn.execute("DELETE FROM txn_splits WHERE split_id=?", (row["split_id"],))
    # Removing only generated portions can leave a mixed set invalid too.
    remaining = conn.execute(
        "SELECT COUNT(*),SUM(amount_cents) FROM txn_splits WHERE txn_id=?", (txn_id,)
    ).fetchone()
    if remaining[0] and remaining[1] != amount:
        archived_splits += [
            dict(r)
            for r in conn.execute("SELECT * FROM txn_splits WHERE txn_id=?", (txn_id,))
        ]
        conn.execute("DELETE FROM txn_splits WHERE txn_id=?", (txn_id,))
    conn.execute(
        "UPDATE normalize_changes SET invalidated_reason='provider_correction' WHERE txn_id=? AND invalidated_reason IS NULL",
        (txn_id,),
    )
    return (
        json.dumps(archived_splits) if archived_splits else None,
        json.dumps(archived_matches) if archived_matches else None,
        bool(archived_splits),
    )
