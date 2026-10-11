from dataclasses import FrozenInstanceError
import sqlite3
import pytest
from local_budget import db


def test_item_starts_disabled_and_namespace_survives_migration(plaid_db, seed_item):
    from local_budget.connectors.plaid import store
    item_id = seed_item()
    with db.connect(plaid_db) as conn:
        row = store.item(conn, item_id)
        assert row['state'] == 'disabled'
        assert row['cursor'] is None
        namespace = conn.execute("SELECT value FROM plaid_meta WHERE key='ledger_namespace'").fetchone()[0]
        assert namespace
    db.init_schema(plaid_db)
    db.init_schema(plaid_db)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item_id) == row
        assert conn.execute("SELECT value FROM plaid_meta WHERE key='ledger_namespace'").fetchone()[0] == namespace
        assert conn.execute('SELECT invalidated_reason FROM normalize_changes').fetchall() == []
        assert conn.execute("SELECT * FROM settings WHERE key='ledger_namespace'").fetchall() == []
        with pytest.raises(KeyError):
            store.item(conn, 999)


def test_identity_unique_per_environment(plaid_db, seed_item):
    seed_item()
    with pytest.raises(sqlite3.IntegrityError):
        seed_item()
    assert seed_item(environment='production')


def test_disabled_maps_can_coexist_but_only_one_enabled_source(plaid_db, seed_item):
    a, b = seed_item('a'), seed_item('b')
    with db.connect(plaid_db) as conn:
        for item_id in (a, b):
            conn.execute('INSERT INTO plaid_accounts(item_id,provider_account_id,local_account_id) VALUES(?,?,1)', (item_id, 'account'))
        conn.execute('UPDATE plaid_accounts SET enabled=1 WHERE item_id=?', (a,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE plaid_accounts SET enabled=1 WHERE item_id=?', (b,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM accounts WHERE account_id=1')


def test_transaction_mapping_scoped_identity_and_restricted_deletion(plaid_db, seed_item):
    a, b = seed_item('a'), seed_item('b')
    with db.connect(plaid_db) as conn:
        conn.execute("INSERT INTO transactions(txn_id,account_id,fitid,posted_date,amount_cents,imported_at) VALUES(1,1,'fixture','2026-10-01',-100,'2026-10-01T00:00:00+00:00')")
        for item_id in (a, b):
            conn.execute('INSERT INTO plaid_accounts(item_id,provider_account_id) VALUES(?,?)', (item_id, 'account'))
            conn.execute('INSERT INTO plaid_transactions(item_id,transaction_id,provider_account_id,local_txn_id,active_owner) VALUES(?,?,?,1,0)', (item_id, 'txn', 'account'))
        conn.execute('UPDATE plaid_transactions SET active_owner=1 WHERE item_id=?', (a,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('UPDATE plaid_transactions SET active_owner=1 WHERE item_id=?', (b,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM transactions WHERE txn_id=1')


def test_safe_status_uses_explicit_projection(plaid_db, seed_item):
    from local_budget.connectors.plaid import store
    seed_item('private-item-sentinel')
    with db.connect(plaid_db) as conn:
        conn.execute('INSERT INTO plaid_status_public(local_account_id,state) VALUES(1,?)', ('healthy',))
        status = store.public_status(conn)
        assert status == [dict(local_account_id=1, state='healthy', last_attempt_at=None,
                               last_commit_at=None, provider_updated_at=None, readiness=None,
                               review_count=0, safe_error_code=None)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE plaid_status_public SET state='active'")


def test_private_dtos_frozen_and_hide_provider_identifiers():
    from local_budget.connectors.plaid.types import TxnFact, SyncBatch, SyncResult
    fact = TxnFact('secret-txn', 'secret-account', '2026-10-01', -100, 'TEST', 'TEST', False)
    batch = SyncBatch('secret-old-cursor', 'secret-new-cursor', (('added', fact),), 'ready', 'request')
    assert fact.currency == 'USD'
    for value in ('secret-txn', 'secret-account', 'secret-old-cursor', 'secret-new-cursor'):
        assert value not in repr(fact) + repr(batch)
    with pytest.raises(FrozenInstanceError):
        fact.amount_cents = 2
    assert SyncResult('success').inserted == 0


def test_user_handle_and_namespace_stable_across_items(plaid_db, seed_item):
    from local_budget.connectors.plaid import store
    a, b = seed_item('a'), seed_item('b')
    with db.connect(plaid_db) as conn:
        assert store.item(conn, a)['user_handle'] == store.item(conn, b)['user_handle'] == store.user_handle(conn)
        assert store.namespace(conn)


def test_existing_normalization_history_survives_additive_migration(plaid_db):
    with db.connect(plaid_db) as conn:
        conn.execute('ALTER TABLE normalize_changes DROP COLUMN invalidated_reason')
        conn.execute("INSERT INTO normalize_changes(batch_id,txn_id,old_canonical) VALUES(1,1,'TEST')")
    db.init_schema(plaid_db)
    with db.connect(plaid_db) as conn:
        row = conn.execute('SELECT old_canonical,invalidated_reason FROM normalize_changes').fetchone()
        assert tuple(row) == ('TEST', None)


def test_backup_retains_private_identity_and_metadata(plaid_db, seed_item):
    from local_budget import backup
    from local_budget.connectors.plaid import store
    item_id = seed_item()
    with db.connect(plaid_db) as conn:
        identity = (store.namespace(conn), store.user_handle(conn))
        original = store.item(conn, item_id)
    destination = backup.backup()
    with db.connect(destination) as conn:
        assert (store.namespace(conn), store.user_handle(conn)) == identity
        assert store.item(conn, item_id) == original
