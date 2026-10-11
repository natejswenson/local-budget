import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from local_budget import db, reports
from local_budget.agent import tools
from local_budget.connectors.plaid import cli, store, sync
from .test_apply import enabled
from .test_link import Vault

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def test_recent_attempt_does_not_hide_stale_commit(plaid_db):
    with db.connect(plaid_db) as conn:
        conn.execute('INSERT INTO plaid_status_public(local_account_id,state,last_attempt_at,last_commit_at,readiness) VALUES(1,?,?,?,?)', ('healthy', NOW.isoformat(), (NOW-timedelta(days=3)).isoformat(), 'HISTORICAL_UPDATE_COMPLETE'))
        row = reports.bank_sync_status(conn, now=NOW)[0]
    assert row['stale'] is True
    assert row['provider_updated_at'] is None


@pytest.mark.parametrize('hours,stale', [(47.99, False), (48, True), (49, True)])
def test_stale_boundary(hours, stale):
    assert reports.is_stale(NOW-timedelta(hours=hours), NOW) is stale
    assert reports.is_stale(NOW, NOW, NOW-timedelta(hours=hours)) is stale
    assert reports.is_stale(None, NOW)


def health(provider='test-item', accounts=('a1',), stamp=None):
    item = {'item': {'item_id': provider, 'error': None}, 'status': {'transactions': {'last_successful_update': stamp}}}
    account = {'item': {'item_id': provider}, 'accounts': [dict(account_id=a, type='depository', subtype='checking', mask='1234', balances={'iso_currency_code': 'USD'}) for a in accounts]}
    return item, account


def refresh(conn, item, **kwargs):
    ir, ar = health(**kwargs)
    store.refresh_item_health(conn, item_id=item, item_response=ir, accounts_response=ar, now=NOW)


def test_discovery_revocation_and_top_level_freshness(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        refresh(conn, item, accounts=('new-SENTINEL',), stamp='2026-10-07T00:00:00Z')
        assert store.item(conn, item)['mapping_generation'] == 3
        assert conn.execute("SELECT enabled FROM plaid_accounts WHERE provider_account_id='a1'").fetchone()[0] == 0
        assert conn.execute("SELECT enabled FROM plaid_accounts WHERE provider_account_id='new-SENTINEL'").fetchone()[0] == 0
        row = reports.bank_sync_status(conn, now=NOW)[0]
        assert row['state'] == 'reconnect_required'
        assert row['stale'] is True
        assert row['provider_updated_at'].startswith('2026-10-07')


@pytest.mark.parametrize('malformation', ['wrong_item', 'missing_accounts', 'invalid_account', 'item_error'])
def test_failed_health_does_not_disable_mapping(plaid_db, seed_item, malformation):
    item = seed_item()
    enabled(plaid_db, item)
    ir, ar = health()
    if malformation == 'wrong_item':
        ar['item']['item_id'] = 'OTHER-SENTINEL'
    elif malformation == 'missing_accounts':
        ar.pop('accounts')
    elif malformation == 'invalid_account':
        ar['accounts'] = [{}]
    else:
        ir['item']['error'] = {'error_code': 'ITEM_LOGIN_REQUIRED', 'message': 'SECRET-SENTINEL'}
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(sync.PlaidError):
            store.refresh_item_health(conn, item_id=item, item_response=ir, accounts_response=ar, now=NOW)
        assert conn.execute('SELECT enabled FROM plaid_accounts').fetchone()[0] == 1
        assert store.item(conn, item)['mapping_generation'] == 2


def completed_update(conn, item):
    store.add_link_session(conn, session_id='session', mode='update', item_id=item, credential_ref='SECRET-SENTINEL', expires_at=(NOW+timedelta(minutes=15)).isoformat())
    conn.execute("UPDATE plaid_link_sessions SET completed=1,state='completed'")


def test_reconnect_requires_update_receipt_and_health_retains_cursor(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE plaid_items SET cursor='saved',state='reconnect_required',last_attempt_at='2026-01-01T00:00:00+00:00'")
        refresh(conn, item)
        assert store.item(conn, item)['state'] == 'reconnect_required'
        completed_update(conn, item)
        refresh(conn, item)
        assert store.item(conn, item)['state'] == 'active'
        assert store.item(conn, item)['cursor'] == 'saved'


@pytest.mark.parametrize('state', ['recovery_required', 'disconnect_pending', 'disconnected', 'replaced', 'cleanup_needed'])
def test_health_never_restores_terminal_states(plaid_db, seed_item, state):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.set_lifecycle(conn, item, state)
        completed_update(conn, item)
        refresh(conn, item)
        assert store.item(conn, item)['state'] == state


def test_alternative_health_and_lifecycle_leave_enabled_owner(plaid_db, seed_item):
    owner, alternative = seed_item(), seed_item('alternative-SENTINEL')
    enabled(plaid_db, owner)
    from .test_mapping import mapped
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, alternative)
        sync.apply_batch(conn, owner, sync.SyncBatch(None, 'next', (), 'HISTORICAL_UPDATE_COMPLETE', None), expected_generation=2)
        refresh(conn, alternative, provider='alternative-SENTINEL')
        store.set_lifecycle(conn, alternative, 'disconnect_pending')
        sync.record_failure(conn, alternative, sync.PlaidError('ITEM_LOGIN_REQUIRED'), now=NOW)
        assert store.public_status(conn)[0]['state'] == 'healthy'


def test_tool_cli_public_isolation_and_sentinels(plaid_db, seed_item, monkeypatch):
    seed_item('PROVIDER-SENTINEL')
    with db.connect(plaid_db) as conn:
        conn.execute("INSERT INTO plaid_status_public(local_account_id,state,readiness) VALUES(1,'credential_locked','NOT_READY')")
    with db.agent_connect() as conn:
        for table in ('plaid_items', 'plaid_accounts', 'plaid_link_sessions'):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(f'SELECT * FROM {table}').fetchall()
        assert reports.bank_sync_status(conn, now=NOW)[0]['state'] == 'credential_locked'
    spec = tools.SPEC_BY_NAME['budget_bank_sync_status']
    coroutine = spec.handler({})
    with pytest.raises(StopIteration) as completed:
        coroutine.send(None)
    output = completed.value.value
    monkeypatch.setattr(cli, 'PlaidClient', lambda *a, **k: pytest.fail('provider access'))
    result = CliRunner().invoke(cli.plaid, ['status', '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]['readiness'] == 'NOT_READY'
    for text in (json.dumps(output), result.output, json.dumps(spec.input_schema)):
        assert 'SENTINEL' not in text


class HealthClient:
    bytes_received = 0
    def __init__(self, item_response=None, accounts_response=None):
        self.ir, self.ar = health() if item_response is None else (item_response, accounts_response)
        self.calls = []
    def call(self, endpoint, body, **kwargs):
        self.calls.append((endpoint, kwargs['deadline']))
        if endpoint == '/item/get':
            return self.ir
        if endpoint == '/accounts/get':
            return self.ar
        return dict(added=[], modified=[], removed=[], has_more=False, next_cursor='next', transactions_update_status='NOT_READY')


def test_normal_sync_health_shared_deadline_empty_is_initializing(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    client = HealthClient()
    result = sync.sync_item(plaid_db, item, client, vault, now=NOW)
    assert result.outcome == 'initializing'
    assert {e for e, _ in client.calls} == {'/item/get', '/accounts/get', '/transactions/sync'}
    assert len({d for _, d in client.calls}) == 1
    with db.connect(plaid_db) as conn:
        row = reports.bank_sync_status(conn, now=NOW)[0]
        assert row['readiness'] == 'NOT_READY'
        assert row['provider_updated_at'] is None


def test_sync_recovers_completed_reconnect(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE plaid_items SET state='reconnect_required',cursor='saved',last_attempt_at='2026-01-01T00:00:00+00:00'")
        completed_update(conn, item)
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    sync.sync_item(plaid_db, item, HealthClient(), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['state'] == 'active'
        assert store.item(conn, item)['cursor'] == 'next'


def test_partial_health_failure_preserves_access_and_commit(plaid_db, seed_item, caplog):
    item = seed_item()
    enabled(plaid_db, item)
    from .test_apply import apply
    apply(plaid_db, item, next_cursor='saved')
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    class Partial(HealthClient):
        def call(self, endpoint, body, **kwargs):
            if endpoint == '/accounts/get':
                raise RuntimeError('ACCESS-SENTINEL PROVIDER-SENTINEL')
            return super().call(endpoint, body, **kwargs)
    with pytest.raises(sync.SyncStateError, match='sync_failed') as error:
        sync.sync_item(plaid_db, item, Partial(), vault, now=NOW)
    assert 'SENTINEL' not in str(error.value) + caplog.text
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT enabled FROM plaid_accounts').fetchone()[0] == 1
        current = store.item(conn, item)
        assert current['cursor'] == 'saved'
        assert current['last_commit_at'] is not None
        assert store.public_status(conn)[0]['state'] == 'temporary_error'


def test_health_discovery_changes_generation_without_false_race(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    ir, ar = health(accounts=('a1', 'new'))
    ar['accounts'][0]['balances']['iso_currency_code'] = 'EUR'
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    sync.sync_item(plaid_db, item, HealthClient(ir, ar), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['mapping_generation'] == 3
        assert store.item(conn, item)['cursor'] is None
        assert conn.execute('SELECT SUM(enabled) FROM plaid_accounts').fetchone()[0] == 0


def test_health_generation_race_rolls_back_discovery(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    class Raced(HealthClient):
        def call(self, endpoint, body, **kwargs):
            with db.connect(plaid_db) as conn:
                conn.execute('BEGIN IMMEDIATE')
                if endpoint == '/accounts/get':
                    conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1')
            return super().call(endpoint, body, **kwargs)
    with pytest.raises(sync.SyncStateError, match='mapping_changed'):
        sync.sync_item(plaid_db, item, Raced(), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['cursor'] is None
        assert store.item(conn, item)['provider_updated_at'] is None


def test_retired_owner_cannot_overwrite_replacement(plaid_db, seed_item):
    old, new = seed_item(), seed_item('new-item')
    enabled(plaid_db, old)
    from .test_mapping import mapped
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
        conn.execute('UPDATE plaid_accounts SET enabled=0 WHERE item_id=?', (old,))
        conn.execute('UPDATE plaid_accounts SET enabled=1,history_approved=1 WHERE item_id=?', (new,))
        sync.apply_batch(conn, new, sync.SyncBatch(None, 'new', (), 'HISTORICAL_UPDATE_COMPLETE', None), expected_generation=1)
        refresh(conn, old)
        store.set_lifecycle(conn, old, 'replaced')
        sync.record_failure(conn, old, sync.PlaidError('ITEM_LOGIN_REQUIRED'), now=NOW)
        assert store.public_status(conn)[0]['state'] == 'healthy'
        assert store.public_status(conn)[0]['last_commit_at'] is not None


@pytest.mark.parametrize('state', ['reconnect_required', 'revocation_pending', 'temporary_error', 'credential_missing'])
def test_access_health_does_not_claim_history_completion(plaid_db, state):
    with db.connect(plaid_db) as conn:
        conn.execute('INSERT INTO plaid_status_public(local_account_id,state,last_attempt_at,readiness) VALUES(1,?,?,?)', (state, NOW.isoformat(), 'INITIAL_UPDATE_COMPLETE'))
        row = reports.bank_sync_status(conn, now=NOW)[0]
        assert row['state'] == state
        assert row['readiness'] == 'INITIAL_UPDATE_COMPLETE'
        assert row['stale'] is True


def test_last_enabled_revoked_health_commits_without_transactions(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    from .test_apply import apply
    from .test_mapping import fact
    apply(plaid_db, item, ('added', fact('saved-txn')), next_cursor='saved')
    with db.connect(plaid_db) as conn:
        before = [tuple(r) for r in conn.execute('SELECT * FROM transactions')]
        committed = store.item(conn, item)['last_commit_at']
    ir, ar = health(accounts=('new',), stamp='2026-10-07T00:00:00Z')
    client = HealthClient(ir, ar)
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    result = sync.sync_item(plaid_db, item, client, vault, now=NOW)
    assert result.outcome == 'reconnect_required'
    assert result.inserted == result.updated == result.removed == 0
    assert [e for e, _ in client.calls] == ['/item/get', '/accounts/get']
    with db.connect(plaid_db) as conn:
        current = store.item(conn, item)
        assert current['mapping_generation'] == 3
        assert current['cursor'] == 'saved'
        assert current['last_commit_at'] == committed
        assert current['last_attempt_at'] == NOW.isoformat(timespec='seconds')
        assert [tuple(r) for r in conn.execute('SELECT * FROM transactions')] == before
        assert conn.execute('SELECT SUM(enabled) FROM plaid_accounts').fetchone()[0] == 0
        assert store.public_status(conn)[0]['state'] == 'reconnect_required'


def test_health_deadline_prevents_health_only_commit(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    enabled(plaid_db, item)
    ir, ar = health(accounts=())
    vault = Vault()
    vault.put('test-ref-test-item', 'ACCESS-SENTINEL')
    clock = [0]
    monkeypatch.setattr(sync, '_monotonic', lambda: clock[0])
    class Expired(HealthClient):
        def call(self, endpoint, body, **kwargs):
            result = super().call(endpoint, body, **kwargs)
            if endpoint == '/accounts/get':
                clock[0] = 301
            return result
    with pytest.raises(sync.PlaidError, match='sync_deadline'):
        sync.sync_item(plaid_db, item, Expired(ir, ar), vault, now=NOW)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['mapping_generation'] == 2
        assert conn.execute('SELECT enabled FROM plaid_accounts').fetchone()[0] == 1


def test_verified_update_receipt_cannot_restore_later_reconnect(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE plaid_items SET state='reconnect_required'")
        completed_update(conn, item)
        refresh(conn, item)
        assert store.item(conn, item)['state'] == 'active'
        sync.record_failure(conn, item, sync.PlaidError('ITEM_LOGIN_REQUIRED'), now=NOW)
        refresh(conn, item)
        assert store.item(conn, item)['state'] == 'reconnect_required'


@pytest.mark.parametrize('accounts,exit_code,outcome', [(('a1',), 0, 'healthy'), ((), 1, 'reconnect_required')])
def test_scheduled_reconnect_health_gates_exit(plaid_db, seed_item, monkeypatch, accounts, exit_code, outcome):
    from .test_integration_cli import mock_provider, page
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE plaid_items SET state='reconnect_required',last_attempt_at='2026-01-01T00:00:00+00:00'")
        completed_update(conn, item)
    ir, ar = health(accounts=accounts)
    requests = mock_provider(monkeypatch, [ir, ar, page()])
    result = CliRunner().invoke(cli.plaid, ['sync', '--all', '--non-interactive', '--json'])
    assert result.exit_code == exit_code, result.output
    assert json.loads(result.output)[0]['outcome'] == outcome
    assert [r[0] for r in requests] == ['/item/get', '/accounts/get'] + (['/transactions/sync'] if accounts else [])
    assert 'SENTINEL' not in result.output
