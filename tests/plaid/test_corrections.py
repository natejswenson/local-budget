from local_budget import db, splits, normalize
from local_budget.connectors.plaid import store
from .test_apply import apply, enabled
from .test_mapping import fact


def test_correction_preserves_manual_category_invalidates_normalization(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        conn.execute(
            "UPDATE transactions SET category='Subscriptions',subcategory='My label',category_source='manual',canonical_merchant='Old'"
        )
        conn.execute(
            "INSERT INTO normalize_changes(batch_id,txn_id,old_canonical,old_subcategory) VALUES(1,1,'stale','stale')"
        )
    apply(
        plaid_db,
        item,
        ("modified", fact(merchant_norm="ANTHROPIC", payee="Anthropic")),
        cursor="next",
    )
    with db.connect(plaid_db) as conn:
        row = conn.execute("SELECT * FROM transactions").fetchone()
        assert (
            row["txn_id"],
            row["category"],
            row["subcategory"],
            row["category_source"],
            row["canonical_merchant"],
        ) == (1, "Subscriptions", "My label", "manual", "Anthropic")
        assert (
            conn.execute("SELECT invalidated_reason FROM normalize_changes").fetchone()[
                0
            ]
            == "provider_correction"
        )
    normalize.undo_last()
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute("SELECT subcategory FROM transactions").fetchone()[0]
            == "My label"
        )


def test_amount_correction_archives_splits_and_immediate_unsplit_total(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        splits.apply(conn, 1, [dict(amount_cents=-1200, category="Shopping")])
    apply(plaid_db, item, ("modified", fact(amount_cents=-1600)), cursor="next")
    with db.connect(plaid_db) as conn:
        assert not splits.for_txn(conn, 1)
        assert (
            conn.execute(
                "SELECT SUM(amount_cents) FROM effective_txns WHERE status='posted'"
            ).fetchone()[0]
            == -1600
        )
        assert (
            conn.execute(
                "SELECT split_needs_review FROM plaid_transactions"
            ).fetchone()[0]
            == 1
        )
        assert (
            "-1200"
            in conn.execute(
                "SELECT archived_splits FROM plaid_changes WHERE event_kind='modified'"
            ).fetchone()[0]
        )


def test_rules_recompute_and_valid_manual_splits_survive_merchant_change(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        splits.apply(conn, 1, [dict(amount_cents=-1200, category="Shopping")])
    apply(plaid_db, item, ("modified", fact(merchant_norm="WALMART")), cursor="next")
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute("SELECT category FROM transactions").fetchone()[0]
            == "Groceries"
        )
        assert len(splits.for_txn(conn, 1)) == 1


def test_merchant_match_and_generated_splits_invalidated_on_date_change(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO amazon_transactions(amazon_txn_id,completed_date,grand_total_cents,fetched_at) VALUES(1,'2026-10-01',-1200,'now')"
        )
        conn.execute(
            "INSERT INTO amazon_matches(amazon_txn_id,txn_id,confidence,matched_at) VALUES(1,1,'manual','now')"
        )
        conn.execute(
            "INSERT INTO walmart_orders(order_number,order_placed_date,fetched_at) VALUES('w1','2026-10-01','now')"
        )
        conn.execute(
            "INSERT INTO walmart_matches(order_number,txn_id,confidence,matched_at) VALUES('w1',1,'manual','now')"
        )
        splits.apply(
            conn, 1, [dict(amount_cents=-1200, category="Shopping")], source="amazon"
        )
    apply(plaid_db, item, ("modified", fact(posted_date="2026-10-02")), cursor="next")
    with db.connect(plaid_db) as conn:
        assert not splits.for_txn(conn, 1)
        assert not conn.execute("SELECT * FROM amazon_matches").fetchall()
        assert not conn.execute("SELECT * FROM walmart_matches").fetchall()
        archive = conn.execute(
            "SELECT archived_matches FROM plaid_changes WHERE event_kind='modified'"
        ).fetchone()[0]
        assert "amazon" in archive and "walmart" in archive


def test_removed_and_pending_existing_rows_remove_all_derived(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        splits.apply(conn, 1, [dict(amount_cents=-1200, category="Shopping")])
    apply(plaid_db, item, ("modified", fact(pending=True)), cursor="next")
    with db.connect(plaid_db) as conn:
        assert not splits.for_txn(conn, 1)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM effective_txns WHERE status='posted'"
            ).fetchone()[0]
            == 0
        )
    apply(plaid_db, item, ("removed", "t1"), cursor="next")
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute("SELECT status FROM transactions").fetchone()[0] == "removed"
        )
        assert store.public_status(conn)[0]["review_count"] >= 0
