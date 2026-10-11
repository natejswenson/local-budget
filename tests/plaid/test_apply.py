from datetime import datetime, timezone
import sqlite3

import pytest

from local_budget import db
from local_budget.connectors.plaid import store, sync
from local_budget.connectors.plaid.types import SyncBatch
from .test_mapping import fact, mapped

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)


def health_response(endpoint):
    if endpoint == '/item/get':
        return {'item': {'item_id': 'test-item', 'error': None}}
    if endpoint == '/accounts/get':
        return {'item': {'item_id': 'test-item'}, 'accounts': [dict(account_id='a1', type='depository', subtype='checking', balances={'iso_currency_code': 'USD'})]}
    return None


def enabled(path, item):
    with db.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mapped(conn, item)
        store.enable_account(
            conn,
            item_id=item,
            provider_account_id="a1",
            reviewed_generation=1,
            batch=SyncBatch(None, "", (), "HISTORICAL_UPDATE_COMPLETE", None),
        )
    return 2


def apply(path, item, *events, cursor=None, next_cursor="next", generation=2):
    with db.connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return sync.apply_batch(
            conn,
            item,
            SyncBatch(
                cursor, next_cursor, tuple(events), "HISTORICAL_UPDATE_COMPLETE", None
            ),
            expected_generation=generation,
        )


def test_generation_race_never_advances_cursor(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sync.SyncStateError, match="mapping_changed"):
            sync.apply_batch(
                conn,
                item,
                SyncBatch(None, "next", (), "unknown", None),
                expected_generation=1,
            )
        assert store.item(conn, item)["cursor"] is None


def test_final_observation_replay_distinct_purchases_and_opaque_fitids(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    result = apply(
        plaid_db,
        item,
        ("added", fact("SECRET_PROVIDER_1")),
        ("modified", fact("SECRET_PROVIDER_1", amount_cents=-1500)),
        ("added", fact("SECRET_PROVIDER_2")),
    )
    assert result.inserted == 2
    apply(
        plaid_db,
        item,
        ("added", fact("SECRET_PROVIDER_1", amount_cents=-1500)),
        cursor="next",
    )
    with db.connect(plaid_db) as conn:
        rows = conn.execute("SELECT * FROM transactions").fetchall()
        assert len(rows) == 2 and sum(r["amount_cents"] for r in rows) == -2700
        assert all(
            "SECRET_PROVIDER" not in r["fitid"]
            and r["raw_ofx"] is None
            and r["import_run_id"] is None
            for r in rows
        )
        assert store.item(conn, item)["cursor"] == "next"
        assert store.public_status(conn)[0]["state"] == "healthy"


def test_removal_before_add_pending_preboundary_reappearance(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(
        plaid_db,
        item,
        ("removed", "unknown"),
        ("added", fact("pending", pending=True)),
        ("added", fact("old", posted_date="2026-09-30")),
    )
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT provider_account_id FROM plaid_transactions WHERE transaction_id='unknown'"
            ).fetchone()[0]
            is None
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM plaid_transactions").fetchone()[0] == 3
        )
    apply(
        plaid_db, item, ("removed", "pending"), ("added", fact("posted")), cursor="next"
    )
    apply(plaid_db, item, ("removed", "posted"), cursor="next")
    with db.connect(plaid_db) as conn:
        txn = conn.execute("SELECT * FROM transactions").fetchone()
        assert txn["status"] == "removed"
        txn_id = txn["txn_id"]
    apply(plaid_db, item, ("added", fact("posted")), cursor="next")
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT txn_id,status FROM transactions").fetchone()[:] == (
            txn_id,
            "posted",
        )


@pytest.mark.parametrize(
    "bad",
    [
        fact("bad", account_id="other"),
        fact("bad", currency="EUR"),
        fact("bad", posted_date="invalid"),
        fact("bad", amount_cents=True),
    ],
)
def test_invalid_final_fact_rejects_whole_batch_before_mutation(
    plaid_db, seed_item, bad
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        with pytest.raises(sync.SyncStateError):
            sync.apply_batch(
                conn,
                item,
                SyncBatch(
                    None, "next", (("added", fact()), ("added", bad)), "unknown", None
                ),
                expected_generation=2,
            )
        assert conn.total_changes == before
        assert store.item(conn, item)["cursor"] is None


def test_inactive_owner_cannot_reclaim_replaced_transaction(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE plaid_transactions SET active_owner=0")
    with pytest.raises(sync.SyncStateError, match="inactive_owner"):
        apply(plaid_db, item, ("modified", fact(amount_cents=-9000)), cursor="next")
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute("SELECT amount_cents FROM transactions").fetchone()[0] == -1200
        )


def test_outer_failure_rolls_back_facts_maps_audit_health_and_cursor(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute(
            "CREATE TRIGGER abort_cursor BEFORE UPDATE OF cursor ON plaid_items BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        for table in [
            "transactions",
            "plaid_transactions",
            "plaid_changes",
            "plaid_sync_runs",
            "plaid_status_public",
        ]:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert store.item(conn, item)["cursor"] is None


def test_adoption_requires_exact_fresh_unowned_local_match(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')"
        )
        with pytest.raises(ValueError, match="stale_review"):
            store.adopt_transaction(
                conn,
                item_id=item,
                provider_transaction_id="t1",
                local_txn_id=1,
                fact=fact(amount_cents=-999),
            )
        store.adopt_transaction(
            conn,
            item_id=item,
            provider_transaction_id="t1",
            local_txn_id=1,
            fact=fact(),
        )
        with pytest.raises(ValueError, match="already_owned"):
            store.adopt_transaction(
                conn,
                item_id=item,
                provider_transaction_id="t2",
                local_txn_id=1,
                fact=fact("t2"),
            )
    apply(plaid_db, item, ("modified", fact(amount_cents=-1500)))
    with db.connect(plaid_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*),amount_cents FROM transactions"
        ).fetchone()[:] == (1, -1500)


def test_old_private_schema_migrates_nullable_with_item_fk(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='plaid_transactions'"
        ).fetchone()[0]
        conn.execute("DROP TABLE plaid_transactions")
        old_sql = sql.replace(
            "provider_account_id TEXT,", "provider_account_id TEXT NOT NULL,"
        ).replace(
            "item_id INTEGER NOT NULL REFERENCES plaid_items(item_id) ON DELETE RESTRICT",
            "item_id INTEGER NOT NULL",
        )
        conn.execute(old_sql)
        conn.execute(
            "INSERT INTO plaid_transactions(item_id,transaction_id,provider_account_id) VALUES(?,'seed','a1')",
            (item,),
        )
    db.init_schema(plaid_db)
    apply(plaid_db, item, ("removed", "unknown"))
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute(
                "SELECT transaction_id FROM plaid_transactions WHERE transaction_id='seed'"
            ).fetchone()[0]
            == "seed"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plaid_transactions(item_id,transaction_id) VALUES(999,'orphan')"
            )
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='plaid_active_txn'"
        ).fetchone()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_sync_item_network_outside_write_and_failure_health_preserves_cursor(
    plaid_db, seed_item, monkeypatch
):
    from .test_link import Vault
    from local_budget.connectors.plaid.client import PlaidError

    item = seed_item()
    enabled(plaid_db, item)
    vault = Vault()
    vault.put("test-ref-test-item", "FABRICATED_ACCESS")

    class Client:
        bytes_received = 0

        def call(self, endpoint, body, **kwargs):
            with db.connect(plaid_db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR REPLACE INTO settings(key,value) VALUES('network-probe','unlocked')"
                )
            health = health_response(endpoint)
            if health is not None:
                return health
            return dict(
                added=[],
                modified=[],
                removed=[],
                has_more=False,
                next_cursor="saved",
                transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
            )

    sync.sync_item(plaid_db, item, Client(), vault, now=NOW)

    class Bad(Client):
        def call(self, *args, **kwargs):
            raise PlaidError("ITEM_LOGIN_REQUIRED")

    with pytest.raises(PlaidError):
        sync.sync_item(plaid_db, item, Bad(), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        current = store.item(conn, item)
        assert current["cursor"] == "saved" and current["last_commit_at"] is not None
        health = store.public_status(conn)[0]
        assert (
            health["state"] == "reconnect_required"
            and health["safe_error_code"] == "ITEM_LOGIN_REQUIRED"
        )
        assert health["last_commit_at"] == current["last_commit_at"]
        assert (
            conn.execute(
                "SELECT outcome FROM plaid_sync_runs ORDER BY run_id DESC"
            ).fetchone()[0]
            == "failed"
        )


def test_review_fetch_full_then_saved_delta_with_removal_and_shared_client(monkeypatch):
    requests = []

    class Client:
        bytes_received = 0

        def call(self, endpoint, body, **kwargs):
            requests.append(body.get("cursor"))
            data = dict(
                transaction_id="t1",
                account_id="a1",
                date="2026-10-01",
                amount=12,
                pending=False,
                iso_currency_code="USD",
                name="SHOP",
            )
            return dict(
                added=[data] if len(requests) == 1 else [],
                modified=[],
                removed=[] if len(requests) == 1 else [dict(transaction_id="t1")],
                has_more=False,
                next_cursor="full" if len(requests) == 1 else "delta",
                transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
            )

    client = Client()
    result = sync.fetch_review_batch(
        client, "FABRICATED_ACCESS", cursor="saved", deadline=sync._monotonic() + 300
    )
    assert requests == [None, "saved"]
    assert (result.original_cursor, result.next_cursor) == ("saved", "delta")
    assert list(sync.final_events(result).values()) == [("removed", "t1")]
    assert client.updates_received == 2


def test_historical_adoption_after_excluded_observation_survives_remove_reappear(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    old = fact("historical", posted_date="2026-09-30")
    apply(plaid_db, item, ("added", old))
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'history','2026-09-30',-1200,'SHOP','now')"
        )
        store.adopt_transaction(
            conn,
            item_id=item,
            provider_transaction_id="historical",
            local_txn_id=1,
            fact=old,
        )
    apply(plaid_db, item, ("removed", "historical"), cursor="next")
    apply(plaid_db, item, ("added", old), cursor="next")
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT txn_id,status FROM transactions").fetchone()[:] == (
            1,
            "posted",
        )


def test_unsupported_or_revoked_source_cannot_adopt(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')"
        )
        conn.execute("UPDATE plaid_items SET state='disconnected'")
        with pytest.raises(ValueError, match="item_disabled"):
            store.adopt_transaction(
                conn,
                item_id=item,
                provider_transaction_id="t1",
                local_txn_id=1,
                fact=fact(),
            )


def test_cursor_race_precedes_lifecycle_error(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE plaid_items SET cursor='saved',state='disconnected'")
        with pytest.raises(sync.SyncStateError, match="cursor_changed"):
            sync.apply_batch(
                conn,
                item,
                SyncBatch(None, "next", (), "unknown", None),
                expected_generation=0,
            )


def test_sync_failure_after_apply_rolls_back_then_records_safe_health(
    plaid_db, seed_item
):
    from .test_link import Vault

    item = seed_item()
    enabled(plaid_db, item)
    vault = Vault()
    vault.put("test-ref-test-item", "FABRICATED_ACCESS")
    with db.connect(plaid_db) as conn:
        conn.execute(
            "CREATE TRIGGER abort_cursor BEFORE UPDATE OF cursor ON plaid_items BEGIN SELECT RAISE(ABORT,'RAW_SECRET_ERROR'); END"
        )

    class Client:
        bytes_received = 0

        def call(self, endpoint, *args, **kwargs):
            health = health_response(endpoint)
            if health is not None:
                return health
            return dict(
                added=[
                    dict(
                        transaction_id="t1",
                        account_id="a1",
                        date="2026-10-01",
                        amount=12,
                        pending=False,
                        iso_currency_code="USD",
                        name="SHOP",
                    )
                ],
                modified=[],
                removed=[],
                has_more=False,
                next_cursor="next",
                transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
            )

    with pytest.raises(sync.SyncStateError, match="sync_failed"):
        sync.sync_item(plaid_db, item, Client(), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM plaid_changes").fetchone()[0] == 0
        assert store.item(conn, item)["cursor"] is None
        assert store.item(conn, item)["last_commit_at"] is None
        assert store.public_status(conn)[0]["safe_error_code"] == "sync_failed"
        assert conn.execute("SELECT COUNT(*) FROM plaid_sync_runs").fetchone()[0] == 1


def test_unexpected_vault_failure_has_no_secret_exception_context(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)

    class Hostile:
        def get(self, *args, **kwargs):
            raise RuntimeError("RAW_SECRET_VALUE")

    with pytest.raises(sync.SyncStateError) as raised:
        sync.sync_item(plaid_db, item, object(), Hostile(), now=NOW)
    assert str(raised.value) == "sync_failed"
    assert raised.value.__context__ is None


def test_failure_health_write_failure_does_not_expose_sqlite_error(plaid_db, seed_item):
    from .test_link import Vault
    from local_budget.connectors.plaid.client import PlaidError

    item = seed_item()
    enabled(plaid_db, item)
    vault = Vault()
    vault.put("test-ref-test-item", "FABRICATED_ACCESS")
    with db.connect(plaid_db) as conn:
        conn.execute(
            "CREATE TRIGGER abort_audit BEFORE INSERT ON plaid_sync_runs BEGIN SELECT RAISE(ABORT,'SECRET_SQLITE_FAILURE'); END"
        )

    class Bad:
        bytes_received = 0

        def call(self, *args, **kwargs):
            raise PlaidError("ITEM_LOGIN_REQUIRED")

    with pytest.raises(PlaidError) as raised:
        sync.sync_item(plaid_db, item, Bad(), vault, now=NOW)
    assert str(raised.value) == "ITEM_LOGIN_REQUIRED"
    assert raised.value.__context__ is None


def test_failed_legacy_migration_rolls_back_scratch_table_and_retries(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='plaid_transactions'"
        ).fetchone()[0]
        conn.execute("DROP TABLE plaid_transactions")
        old_sql = sql.replace(
            "provider_account_id TEXT,", "provider_account_id TEXT NOT NULL,"
        ).replace(
            "item_id INTEGER NOT NULL REFERENCES plaid_items(item_id) ON DELETE RESTRICT",
            "item_id INTEGER NOT NULL",
        )
        conn.execute(old_sql)
    # Legacy composite FKs cannot catch a broken source if checks were disabled.
    raw = sqlite3.connect(plaid_db)
    raw.execute(
        "INSERT INTO plaid_transactions(item_id,transaction_id,provider_account_id) VALUES(999,'bad','a1')"
    )
    raw.commit()
    raw.close()
    with pytest.raises(sqlite3.IntegrityError):
        db.init_schema(plaid_db)
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='plaid_transactions_new'"
            ).fetchone()
            is None
        )
        conn.execute("DELETE FROM plaid_transactions WHERE transaction_id='bad'")
    db.init_schema(plaid_db)
    with db.connect(plaid_db) as conn:
        assert not next(
            r
            for r in conn.execute("PRAGMA table_info(plaid_transactions)")
            if r[1] == "provider_account_id"
        )[3]


@pytest.mark.parametrize("state", ["credential_locked", "credential_missing"])
def test_credential_failure_state_can_retry_after_credentials_available(
    plaid_db, seed_item, state
):
    from .test_link import Vault
    from local_budget.connectors.plaid.credentials import CredentialError

    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        sync.record_failure(conn, item, CredentialError(state), now=NOW)
    vault = Vault()
    vault.put("test-ref-test-item", "FABRICATED_ACCESS")

    class Client:
        bytes_received = 0

        def call(self, endpoint, *args, **kwargs):
            health = health_response(endpoint)
            if health is not None:
                return health
            return dict(
                added=[],
                modified=[],
                removed=[],
                has_more=False,
                next_cursor="next",
                transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
            )

    assert sync.sync_item(plaid_db, item, Client(), vault, now=NOW).outcome == "healthy"


def test_apply_rechecks_local_account_compatibility_and_history_approval(
    plaid_db, seed_item
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE accounts SET acct_type='savings'")
    with pytest.raises(sync.SyncStateError, match="account_changed"):
        apply(plaid_db, item, ("added", fact()))


def test_adoption_cannot_claim_account_with_another_enabled_source(plaid_db, seed_item):
    item, other = seed_item(), seed_item("other")
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mapped(conn, item)
        mapped(conn, other)
        store.enable_account(
            conn,
            item_id=other,
            provider_account_id="a1",
            reviewed_generation=1,
            batch=SyncBatch(None, "", (), "HISTORICAL_UPDATE_COMPLETE", None),
        )
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')"
        )
        with pytest.raises(ValueError, match="active_source"):
            store.adopt_transaction(
                conn,
                item_id=item,
                provider_transaction_id="t1",
                local_txn_id=1,
                fact=fact(),
            )


def test_private_active_success_and_temporary_failure_preserves_lifecycle(
    plaid_db, seed_item
):
    from .test_link import Vault
    from local_budget.connectors.plaid.client import PlaidError

    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ("added", fact()))
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["state"] == "active"
    vault = Vault()
    vault.put("test-ref-test-item", "FABRICATED_ACCESS")

    class Bad:
        bytes_received = 0

        def call(self, *args, **kwargs):
            raise PlaidError("INSTITUTION_DOWN")

    with pytest.raises(PlaidError):
        sync.sync_item(plaid_db, item, Bad(), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["state"] == "active"
        assert store.public_status(conn)[0]["state"] == "temporary_error"


def test_application_budget_expiry_rolls_back_before_cursor_commit(
    plaid_db, seed_item, monkeypatch
):
    from local_budget.connectors.plaid import derived
    from local_budget.connectors.plaid.client import PlaidError

    item = seed_item()
    enabled(plaid_db, item)
    clock = [0]
    monkeypatch.setattr(sync, "_monotonic", lambda: clock[0])
    real = derived.categories

    def expensive(*args, **kwargs):
        result = real(*args, **kwargs)
        clock[0] = 301
        return result

    monkeypatch.setattr(derived, "categories", expensive)
    with pytest.raises(PlaidError, match="sync_deadline"):
        with db.connect(plaid_db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            sync.apply_batch(
                conn,
                item,
                SyncBatch(
                    None,
                    "next",
                    (("added", fact()),),
                    "HISTORICAL_UPDATE_COMPLETE",
                    None,
                ),
                expected_generation=2,
                deadline=300,
            )
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["cursor"] is None
        for table in [
            "transactions",
            "plaid_transactions",
            "plaid_changes",
            "plaid_sync_runs",
            "plaid_status_public",
        ]:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_credentials_health_does_not_change_private_lifecycle(plaid_db, seed_item):
    from local_budget.connectors.plaid.credentials import CredentialError

    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        sync.record_failure(conn, item, CredentialError("credential_locked"), now=NOW)
        assert store.item(conn, item)["state"] == "disabled"
        assert store.public_status(conn)[0]["state"] == "credential_locked"


@pytest.mark.parametrize("removed_while_pending", [False, True])
def test_adopted_preboundary_pending_to_posted_preserves_approval(
    plaid_db, seed_item, removed_while_pending
):
    item = seed_item()
    enabled(plaid_db, item)
    historical = fact("historical", posted_date="2026-09-30")
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'history','2026-09-30',-1200,'SHOP','now')"
        )
        store.adopt_transaction(
            conn,
            item_id=item,
            provider_transaction_id="historical",
            local_txn_id=1,
            fact=historical,
        )
    apply(
        plaid_db,
        item,
        ("modified", fact("historical", posted_date="2026-09-30", pending=True)),
    )
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT txn_id,status FROM transactions").fetchone()[:] == (
            1,
            "pending",
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM effective_txns WHERE status='posted'"
            ).fetchone()[0]
            == 0
        )
    if removed_while_pending:
        apply(plaid_db, item, ("removed", "historical"), cursor="next")
    apply(plaid_db, item, ("modified", historical), cursor="next")
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT txn_id,status FROM transactions").fetchone()[:] == (
            1,
            "posted",
        )
        assert (
            conn.execute(
                "SELECT SUM(amount_cents) FROM effective_txns WHERE status='posted'"
            ).fetchone()[0]
            == -1200
        )
        assert conn.execute(
            "SELECT local_txn_id,active_owner,disposition FROM plaid_transactions"
        ).fetchone()[:] == (1, 1, "adopted")
