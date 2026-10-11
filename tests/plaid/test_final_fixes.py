"""Regression coverage for the final architecture review's cross-contract cases."""
import json

import pytest
from click.testing import CliRunner

from local_budget import db, splits
from local_budget.connectors.plaid import cli, store, sync
from .test_apply import apply, enabled
from .test_mapping import batch, discovery, fact, mapped
from .test_status import NOW, HealthClient, completed_update, health
from .test_link import Vault
from .test_integration_cli import page, transaction


@pytest.mark.parametrize('operation', ['modified', 'removed'])
@pytest.mark.parametrize('fresh', [False, True])
def test_partial_access_defers_owned_events_until_review(plaid_db, seed_item, operation, fresh):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item, 'a2')
        conn.execute("INSERT INTO accounts(account_id,acct_type,acct_hash) VALUES(2,'checking','second')")
        store.map_account(conn, item_id=item, provider_account_id='a2', local_account_id=2, currency='USD', start_date='2026-10-01')
        store.enable_account(conn, item_id=item, provider_account_id='a2', reviewed_generation=3,
                             batch=sync.SyncBatch('next', 'next', (), 'HISTORICAL_UPDATE_COMPLETE', None))
        conn.execute("UPDATE plaid_transactions SET disposition='adopted',split_needs_review=1,review_required=1")
    ir, ar = health(accounts=('a2',))
    class Partial(HealthClient):
        def call(self, endpoint, body, **kwargs):
            if endpoint == '/transactions/sync':
                result = page([transaction('t2', account_id='a2')], cursor='advanced')
                result[operation] = [transaction('t1', amount=16)] if operation == 'modified' else [dict(transaction_id='t1')]
                return result
            return super().call(endpoint, body, **kwargs)
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    sync.sync_item(plaid_db, item, Partial(ir, ar), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['cursor'] == 'advanced'
        assert store._account(conn, item, 'a1')['enabled'] == 0
        assert conn.execute('SELECT amount_cents,status FROM transactions WHERE txn_id=1').fetchone()[:] == (-1200, 'posted')
        assert store.public_status(conn)[0]['state'] == 'reconnect_required'
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 2
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item)
        events = (('modified', fact(amount_cents=-1800)),) if fresh else ()
        reviewed = sync.SyncBatch('advanced', 'reactivated', events, 'HISTORICAL_UPDATE_COMPLETE', None)
        generation = store.item(conn, item)['mapping_generation']
        review = store.preview(conn, item, reviewed)
        if not fresh and operation == 'removed':
            assert review['removed_local_txn_ids'] == [1]
        store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=generation, batch=reviewed)
        sync.apply_batch(conn, item, reviewed, expected_generation=generation+1)
        row = conn.execute('SELECT amount_cents,status FROM transactions WHERE txn_id=1').fetchone()
        assert row[:] == ((-1800, 'posted') if fresh else (-1600, 'posted') if operation == 'modified' else (-1200, 'removed'))
        mapping = conn.execute('SELECT * FROM plaid_transactions WHERE local_txn_id=1').fetchone()
        assert 'adopted' in mapping['disposition'] and mapping['split_needs_review'] == 1
        assert mapping['deferred_event'] is None


@pytest.mark.parametrize('scheduled', [False, True])
def test_completed_reconnect_without_enabled_mapping_is_recoverable(plaid_db, seed_item, monkeypatch, scheduled):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()), next_cursor='saved')
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        stamp = store.item(conn, item)['last_commit_at']
        conn.execute("UPDATE plaid_items SET state='reconnect_required',last_attempt_at='2026-01-01'")
        conn.execute('UPDATE plaid_accounts SET enabled=0,history_approved=0')
        completed_update(conn, item)
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    client = HealthClient(*health(accounts=('new',)))
    if scheduled:
        monkeypatch.setattr(cli, '_host', lambda: None)
        monkeypatch.setattr(cli, '_configured', lambda *a, **k: ({'client_id': 'client'}, vault, 'secret'))
        from contextlib import nullcontext
        monkeypatch.setattr(cli, 'PlaidClient', lambda *a: nullcontext(client))
        result = CliRunner().invoke(cli.plaid, ['sync', '--all', '--non-interactive', '--json'])
        assert json.loads(result.output)[0]['outcome'] == 'awaiting_history_review'
    else:
        result = sync.sync_item(plaid_db, item, client, vault, now=NOW)
        assert result.outcome == 'awaiting_history_review'
    assert [e for e, _ in client.calls] == ['/item/get', '/accounts/get']
    with db.connect(plaid_db) as conn:
        current = store.item(conn, item)
        assert (current['state'], current['cursor'], current['last_commit_at']) == ('disabled', 'saved', stamp)
        assert conn.execute('SELECT SUM(enabled) FROM plaid_accounts').fetchone()[0] == 0
        assert conn.execute('SELECT state FROM plaid_link_sessions').fetchone()[0] == 'health_verified'
        sync._eligible_item(current)
        assert not store.reconnect_completed(conn, current)


def test_adopted_equal_charge_does_not_block_distinct_same_item_purchase(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        conn.execute("INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')")
        store.adopt_transaction(conn, item_id=item, provider_transaction_id='t1', local_txn_id=1, fact=fact())
        history = batch(fact(), fact('t2'))
        assert store.preview(conn, item, history)['unresolved_overlap_count'] == 0
        store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=1, batch=history)
        sync.apply_batch(conn, item, history, expected_generation=2)
        assert conn.execute('SELECT COUNT(*),SUM(amount_cents) FROM transactions').fetchone()[:] == (2, -2400)


@pytest.mark.parametrize('change', ['initial', 'remap', 'rediscover'])
def test_mapping_changes_project_health_without_fabricating_times(plaid_db, seed_item, change):
    item = seed_item()
    if change != 'initial':
        enabled(plaid_db, item)
        apply(plaid_db, item)
    with db.connect(plaid_db) as conn:
        prior = store.public_status(conn)
        conn.execute('BEGIN IMMEDIATE')
        if change == 'rediscover':
            discovery(conn, item, balances={'iso_currency_code': 'EUR'})
        else:
            mapped(conn, item)
        current = store.public_status(conn)[0]
        assert current['state'] == 'awaiting_history_review'
        for key in ('last_commit_at', 'last_attempt_at', 'provider_updated_at'):
            assert current[key] == (prior[0][key] if prior else None)


@pytest.mark.parametrize('changes', [{'merchant_norm': 'DIFFERENT'}, {'amount_cents': 1200}])
def test_manual_category_review_persists_without_splits(plaid_db, seed_item, changes):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()))
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE transactions SET category='Shopping',category_source='manual'")
    corrected = fact(**changes)
    apply(plaid_db, item, ('modified', corrected), cursor='next')
    apply(plaid_db, item, ('added', corrected), cursor='next')
    with db.connect(plaid_db) as conn:
        assert store.public_status(conn)[0]['review_count'] == 1
        assert conn.execute('SELECT category FROM transactions').fetchone()[0] == 'Shopping'
        assert conn.execute('SELECT category_needs_review,split_needs_review,review_required FROM plaid_transactions').fetchone()[:] == (1, 0, 1)


def needs_review(path, item):
    enabled(path, item)
    apply(path, item, ('added', fact('PROVIDER-SENTINEL')))
    with db.connect(path) as conn:
        splits.apply(conn, 1, [dict(amount_cents=-1200, category='Shopping')])
    apply(path, item, ('modified', fact('PROVIDER-SENTINEL', amount_cents=-1600)), cursor='next')


def test_host_review_lists_and_acknowledges_current_unsplit_facts(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    needs_review(plaid_db, item)
    monkeypatch.setattr(cli, '_host', lambda: None)
    monkeypatch.setattr(cli, '_interactive', lambda: None)
    runner = CliRunner()
    listing = runner.invoke(cli.plaid, ['review', str(item)])
    assert listing.exit_code == 0, listing.output
    assert '1600' in listing.output and 'split' in listing.output and 'Local transaction 1' in listing.output
    assert 'PROVIDER-SENTINEL' not in listing.output
    result = runner.invoke(cli.plaid, ['review', str(item), '--txn', '1'], input='y\n')
    assert result.exit_code == 0, result.output
    with db.connect(plaid_db) as conn:
        assert store.public_status(conn)[0]['review_count'] == 0
        assert conn.execute('SELECT split_needs_review,review_required FROM plaid_transactions').fetchone()[:] == (0, 0)
        assert conn.execute('SELECT amount_cents FROM transactions').fetchone()[0] == -1600


@pytest.mark.parametrize('race', ['facts', 'splits', 'generation', 'ownership'])
def test_review_rejects_changes_after_display(plaid_db, seed_item, monkeypatch, race):
    item = seed_item()
    needs_review(plaid_db, item)
    monkeypatch.setattr(cli, '_host', lambda: None)
    monkeypatch.setattr(cli, '_interactive', lambda: None)
    def confirm(*a, **kw):
        with db.connect(plaid_db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if race == 'facts':
                conn.execute("UPDATE transactions SET category='Groceries'")
            elif race == 'splits':
                splits.apply(conn, 1, [dict(amount_cents=-1600, category='Shopping')])
            elif race == 'generation':
                conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1')
            else:
                conn.execute('UPDATE plaid_transactions SET active_owner=0')
        return True
    monkeypatch.setattr(cli.click, 'confirm', confirm)
    result = CliRunner().invoke(cli.plaid, ['review', str(item), '--txn', '1'])
    assert result.exit_code != 0
    assert 'review_changed' in result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT review_required FROM plaid_transactions').fetchone()[0] == 1


def test_unchanged_replay_adds_run_but_no_correction_event(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()))
    apply(plaid_db, item, ('modified', fact()), cursor='next')
    apply(plaid_db, item, ('added', fact()), cursor='next')
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plaid_sync_runs').fetchone()[0] == 3
        assert conn.execute('SELECT COUNT(*) FROM plaid_changes').fetchone()[0] == 1


@pytest.mark.parametrize('valid', [False, True])
def test_review_requires_valid_repaired_splits(plaid_db, seed_item, monkeypatch, valid):
    item = seed_item()
    needs_review(plaid_db, item)
    monkeypatch.setattr(cli, '_host', lambda: None)
    monkeypatch.setattr(cli, '_interactive', lambda: None)
    with db.connect(plaid_db) as conn:
        splits.apply(conn, 1, [dict(amount_cents=-1600, category='Shopping')])
        if not valid:
            conn.execute('UPDATE txn_splits SET amount_cents=-1500')
    result = CliRunner().invoke(cli.plaid, ['review', str(item), '--txn', '1'], input='y\n')
    assert result.exit_code == (0 if valid else 1), result.output
    if not valid:
        assert 'invalid_splits' in result.output
    with db.connect(plaid_db) as conn:
        assert store.public_status(conn)[0]['review_count'] == (0 if valid else 1)


def test_review_acknowledgment_requires_interactive_confirmation(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    needs_review(plaid_db, item)
    monkeypatch.setattr(cli, '_host', lambda: None)
    result = CliRunner().invoke(cli.plaid, ['review', str(item), '--txn', '1'])
    assert result.exit_code != 0
    assert 'interactive' in result.output.lower()
    result = CliRunner().invoke(cli.plaid, ['review', str(item), '--txn', '1', '--yes'])
    assert result.exit_code != 0 and 'No such option' in result.output
    with db.connect(plaid_db) as conn:
        assert store.public_status(conn)[0]['review_count'] == 1


def test_review_prompt_holds_item_lock_but_not_intake_or_writer(plaid_db, seed_item, monkeypatch):
    from local_budget import inbox_adapter
    from local_budget.connectors.plaid.locks import item_locks
    item = seed_item()
    needs_review(plaid_db, item)
    monkeypatch.setattr(cli, '_host', lambda: None)
    monkeypatch.setattr(cli, '_interactive', lambda: None)
    def confirm(*a, **kw):
        with item_locks(plaid_db, [item]) as acquired:
            assert not acquired
        with inbox_adapter.intake_lock() as intake:
            assert intake
            with db.connect(plaid_db) as conn:
                conn.execute('BEGIN IMMEDIATE')
        return True
    monkeypatch.setattr(cli.click, 'confirm', confirm)
    result = CliRunner().invoke(cli.plaid, ['review', str(item), '--txn', '1'])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize('legacy_nullable', [False, True])
def test_private_additive_migration_preserves_existing_ownership(plaid_db, seed_item, legacy_nullable):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()))
    with db.connect(plaid_db) as conn:
        conn.execute('ALTER TABLE plaid_transactions DROP COLUMN category_needs_review')
        conn.execute('ALTER TABLE plaid_transactions DROP COLUMN deferred_event')
        if not legacy_nullable:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='plaid_transactions'").fetchone()[0]
            sql = sql.replace('CREATE TABLE plaid_transactions', 'CREATE TABLE old_map').replace('provider_account_id TEXT,', 'provider_account_id TEXT NOT NULL,')
            conn.execute(sql)
            conn.execute('INSERT INTO old_map SELECT * FROM plaid_transactions')
            conn.execute('DROP TABLE plaid_transactions')
            conn.execute('ALTER TABLE old_map RENAME TO plaid_transactions')
    db.init_schema(plaid_db)
    db.init_schema(plaid_db)
    with db.connect(plaid_db) as conn:
        mapping = conn.execute('SELECT * FROM plaid_transactions').fetchone()
        assert (mapping['local_txn_id'], mapping['active_owner'], mapping['category_needs_review'], mapping['deferred_event']) == (1, 1, 0, None)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('state,receipt', [('reconnect_required', False), ('disconnected', True), ('recovery_required', True)])
def test_health_only_recovery_needs_fresh_receipt_and_eligible_lifecycle(plaid_db, seed_item, state, receipt):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE plaid_accounts SET enabled=0,history_approved=0')
        conn.execute('UPDATE plaid_items SET state=?', (state,))
        if receipt:
            completed_update(conn, item)
    client = HealthClient()
    with pytest.raises(sync.SyncStateError, match='item_disabled'):
        sync.sync_item(plaid_db, item, client, Vault(), now=NOW)
    assert client.calls == []


@pytest.mark.parametrize('same_item', [True, False])
def test_inactive_or_other_item_ownership_still_requires_overlap_review(plaid_db, seed_item, same_item):
    owner = seed_item()
    enabled(plaid_db, owner)
    apply(plaid_db, owner, ('added', fact()))
    target = owner if same_item else seed_item('replacement')
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if same_item:
            conn.execute('UPDATE plaid_transactions SET active_owner=0')
        else:
            mapped(conn, target)
        current = store.item(conn, target)
        history = sync.SyncBatch(current['cursor'], 'new', (('added', fact('distinct')),), 'HISTORICAL_UPDATE_COMPLETE', None)
        assert store.preview(conn, target, history)['unresolved_overlap_count'] == 1


def test_manual_review_persists_through_removal_and_is_acknowledgeable(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()))
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE transactions SET category='Shopping',category_source='manual'")
    apply(plaid_db, item, ('modified', fact(merchant_norm='NEW')), cursor='next')
    apply(plaid_db, item, ('removed', 't1'), cursor='next')
    monkeypatch.setattr(cli, '_host', lambda: None)
    monkeypatch.setattr(cli, '_interactive', lambda: None)
    result = CliRunner().invoke(cli.plaid, ['review', str(item), '--txn', '1'], input='y\n')
    assert result.exit_code == 0 and 'category' in result.output and 'removed' in result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT category_needs_review,split_needs_review,review_required FROM plaid_transactions').fetchone()[:] == (0, 0, 0)
        assert store.public_status(conn)[0]['review_count'] == 0


def test_verified_reconnect_does_not_mark_disabled_mapping_healthy(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item, 'a2')
        conn.execute("INSERT INTO accounts(account_id,acct_type,acct_hash) VALUES(2,'checking','second')")
        store.map_account(conn, item_id=item, provider_account_id='a2', local_account_id=2, currency='USD', start_date='2026-10-01')
        conn.execute("UPDATE plaid_items SET state='reconnect_required'")
        completed_update(conn, item)
        ir, ar = health(accounts=('a1', 'a2'))
        store.refresh_item_health(conn, item_id=item, item_response=ir, accounts_response=ar, now=NOW)
        assert store.public_status(conn)[1]['state'] == 'awaiting_history_review'


def test_reviewed_rebuild_applies_deferred_removal_and_replay_has_no_audit(plaid_db, seed_item):
    from local_budget.connectors.plaid import lifecycle
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact()))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE plaid_transactions SET deferred_event=?', (json.dumps({'operation': 'removed', 'fact': None}),))
        store.set_lifecycle(conn, item, 'recovery_required')
        snapshot = store.item(conn, item)
        history = sync.SyncBatch('next', 'rebuilt', (), 'HISTORICAL_UPDATE_COMPLETE', None)
        review = store.preview(conn, item, history)
        assert review['removed_local_txn_ids'] == [1]
        lifecycle.apply_rebuild(conn, item, history, snapshot=snapshot, reviewed=review)
        before = conn.execute('SELECT COUNT(*) FROM plaid_changes').fetchone()[0]
        store.set_lifecycle(conn, item, 'recovery_required')
        snapshot = store.item(conn, item)
        replay = sync.SyncBatch('rebuilt', 'replayed', (('removed', 't1'),), 'HISTORICAL_UPDATE_COMPLETE', None)
        lifecycle.apply_rebuild(conn, item, replay, snapshot=snapshot, reviewed=store.preview(conn, item, replay))
        assert conn.execute('SELECT COUNT(*) FROM plaid_changes').fetchone()[0] == before
        assert conn.execute('SELECT status FROM transactions').fetchone()[0] == 'removed'


def test_replacement_preserves_both_review_flags(plaid_db, seed_item):
    from local_budget.connectors.plaid import lifecycle
    owner, replacement = seed_item(), seed_item('replacement')
    enabled(plaid_db, owner)
    apply(plaid_db, owner, ('added', fact()))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, replacement)
        conn.execute('UPDATE plaid_transactions SET category_needs_review=1,split_needs_review=1,review_required=1')
        store.save_fact(conn, item_id=replacement, fact=fact('new'), disposition='excluded')
        lifecycle.transfer_ownership(conn, old_item_id=owner, new_item_id=replacement, matches={'new': 1}, reviewed_generation=1)
        assert conn.execute('SELECT category_needs_review,split_needs_review,review_required FROM plaid_transactions WHERE item_id=?', (replacement,)).fetchone()[:] == (1, 1, 1)


@pytest.mark.parametrize('operation', ['modified', 'removed'])
@pytest.mark.parametrize('action', ['approve', 'decline', 'stale'])
def test_replacement_explicitly_reviews_old_deferred_events(plaid_db, seed_item, monkeypatch, operation, action):
    from dataclasses import asdict
    from .test_integration_cli import mock_provider
    old, new = seed_item('old'), seed_item('new')
    enabled(plaid_db, old)
    apply(plaid_db, old, ('added', fact('old-t')))
    deferred = json.dumps(dict(operation=operation, fact=asdict(fact('old-t', amount_cents=-1900)) if operation == 'modified' else None))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
        conn.execute('UPDATE plaid_transactions SET deferred_event=?,category_needs_review=1,review_required=1', (deferred,))
        conn.execute('UPDATE plaid_accounts SET enabled=0,history_approved=0 WHERE item_id=?', (old,))
    mock_provider(monkeypatch, [page([transaction('new-t')])])
    original = cli._configured
    def configured(env, **kw):
        config, vault, secret = original(env, **kw)
        vault.put('test-ref-new', 'FABRICATED_NEW')
        return config, vault, secret
    monkeypatch.setattr(cli, '_configured', configured)
    if action == 'stale':
        def confirm(message, **kw):
            if message.startswith('Retire'):
                with db.connect(plaid_db) as conn:
                    conn.execute('UPDATE plaid_transactions SET deferred_event=NULL WHERE item_id=?', (old,))
            return True
        monkeypatch.setattr(cli.click, 'confirm', confirm)
    result = CliRunner().invoke(cli.plaid, ['replace', str(old), str(new)], input='1\ny\n' + ('n\n' if action == 'decline' else 'y\n'))
    assert 'Deferred old-source' in result.output
    assert ('-1900 cents' if operation == 'modified' else 'removal') in result.output
    assert result.exit_code == (0 if action == 'approve' else 1), result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT txn_id,amount_cents,status FROM transactions').fetchone()[:] == (1, -1200, 'posted')
        if action == 'approve':
            assert store.item(conn, old)['state'] == 'replaced'
            assert conn.execute('SELECT deferred_event FROM plaid_transactions WHERE item_id=?', (old,)).fetchone()[0] == deferred
            mapping = conn.execute('SELECT * FROM plaid_transactions WHERE item_id=?', (new,)).fetchone()
            assert (mapping['local_txn_id'], mapping['active_owner'], mapping['category_needs_review']) == (1, 1, 1)
            assert mapping['deferred_event'] is None
        else:
            assert store.item(conn, old)['state'] == 'active'
            assert conn.execute('SELECT COUNT(*) FROM plaid_transactions WHERE item_id=?', (new,)).fetchone()[0] == 0
            if action == 'stale':
                assert 'stale_review' in result.output


def test_adoption_uses_deferred_fact_shown_in_review(plaid_db, seed_item, monkeypatch):
    from dataclasses import asdict
    from .test_integration_cli import mock_provider
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        store.save_fact(conn, item_id=item, fact=fact(), disposition='excluded')
        conn.execute('UPDATE plaid_transactions SET deferred_event=?', (json.dumps(dict(operation='modified', fact=asdict(fact(amount_cents=-1600)))),))
        conn.execute("INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1600,'SHOP','now')")
    mock_provider(monkeypatch, [page()])
    result = CliRunner().invoke(cli.plaid, ['adopt', str(item), 't1', '--local-txn', '1'], input='y\n')
    assert result.exit_code == 0, result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT local_txn_id,disposition,amount_cents FROM plaid_transactions').fetchone()[:] == (1, 'adopted', -1600)
