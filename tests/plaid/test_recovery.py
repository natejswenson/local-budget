from local_budget import backup, db
from local_budget.connectors.plaid import store
from .test_apply import apply, enabled
from .test_mapping import fact


def test_backup_restore_older_cursor_replays_stable_facts_namespace(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('t1')), next_cursor='c1')
    destination = backup.backup()
    apply(plaid_db, item, ('added', fact('t2')), cursor='c1', next_cursor='c2')
    def snapshot(path):
        with db.connect(path) as conn:
            return (store.namespace(conn), [tuple(r) for r in conn.execute('SELECT txn_id,fitid,amount_cents,status FROM transactions ORDER BY txn_id')])
    expected = snapshot(plaid_db)
    apply(destination, item, ('added', fact('t1')), ('added', fact('t2')), cursor='c1', next_cursor='c2')
    assert snapshot(destination) == expected
    with db.connect(destination) as conn:
        assert store.item(conn, item)['cursor'] == 'c2'


def test_invalid_field_stops_automatic_replay_without_reset(plaid_db, seed_item):
    from datetime import datetime, timezone
    from local_budget.connectors.plaid import sync
    from local_budget.connectors.plaid.client import PlaidError
    from .test_lifecycle import Client, Vault
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('t1')), next_cursor='saved')
    with db.connect(plaid_db) as conn:
        ref = store.item(conn, item)['credential_ref']
    client = Client(PlaidError('INVALID_FIELD'))
    import pytest
    with pytest.raises(PlaidError, match='INVALID_FIELD'):
        sync.sync_item(plaid_db, item, client, Vault(ref), now=datetime.now(timezone.utc))
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['state'] == 'recovery_required'
        assert store.item(conn, item)['cursor'] == 'saved'
    with pytest.raises(sync.SyncStateError, match='item_disabled'):
        sync.sync_item(plaid_db, item, client, Vault(ref), now=datetime.now(timezone.utc))
    assert len(client.calls) == 1


def test_reviewed_rebuild_replay_and_stale_rejection(plaid_db, seed_item):
    import pytest
    from local_budget.connectors.plaid import lifecycle
    from local_budget.connectors.plaid.types import SyncBatch
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('t1')), next_cursor='saved')
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.set_lifecycle(conn, item, 'recovery_required')
        current = store.item(conn, item)
        batch = SyncBatch('saved', 'rebuilt', (('added', fact('t1')), ('added', fact('t2', merchant_norm='OTHER'))), 'HISTORICAL_UPDATE_COMPLETE', None)
        review = store.preview(conn, item, batch)
        result = lifecycle.apply_rebuild(conn, item, batch, snapshot=current, reviewed=review)
        assert result.inserted == 1
        assert store.item(conn, item)['cursor'] == 'rebuilt'
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 2
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.set_lifecycle(conn, item, 'recovery_required')
        with pytest.raises(ValueError, match='stale_review'):
            lifecycle.apply_rebuild(conn, item, batch, snapshot=current, reviewed=review)
        assert store.item(conn, item)['state'] == 'recovery_required'
        assert store.item(conn, item)['cursor'] == 'rebuilt'


def test_rebuild_requires_complete_no_overlap(plaid_db, seed_item):
    import pytest
    from local_budget.connectors.plaid import lifecycle
    from local_budget.connectors.plaid.types import SyncBatch
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('owned')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.set_lifecycle(conn, item, 'recovery_required')
        current = store.item(conn, item)
        for batch in [SyncBatch('next', 'full', (), 'INITIAL_UPDATE_COMPLETE', None),
                      SyncBatch('next', 'full', (('added', fact('overlap')),), 'HISTORICAL_UPDATE_COMPLETE', None)]:
            with pytest.raises(ValueError, match='history_incomplete|unresolved_overlap'):
                lifecycle.apply_rebuild(conn, item, batch, snapshot=current, reviewed=store.preview(conn, item, batch))
            assert store.item(conn, item)['state'] == 'recovery_required'


def test_rebuild_keeps_absent_history_and_rolls_back_on_failure(plaid_db, seed_item):
    import pytest
    from local_budget.connectors.plaid import lifecycle, sync
    from local_budget.connectors.plaid.types import SyncBatch
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('old-history')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.set_lifecycle(conn, item, 'recovery_required')
        snapshot = store.item(conn, item)
        invalid = SyncBatch('next', 'bad', (('added', fact('invalid', amount_cents=True)),), 'HISTORICAL_UPDATE_COMPLETE', None)
        with pytest.raises(sync.SyncStateError, match='invalid_fact'):
            lifecycle.apply_rebuild(conn, item, invalid, snapshot=snapshot, reviewed=store.preview(conn, item, invalid))
        assert store.item(conn, item)['state'] == 'recovery_required'
        assert store.item(conn, item)['cursor'] == 'next'
        batch = SyncBatch('next', 'rebuilt', (), 'HISTORICAL_UPDATE_COMPLETE', None)
        lifecycle.apply_rebuild(conn, item, batch, snapshot=snapshot, reviewed=store.preview(conn, item, batch))
        assert conn.execute('SELECT COUNT(*) FROM transactions WHERE status=\'posted\'').fetchone()[0] == 1
