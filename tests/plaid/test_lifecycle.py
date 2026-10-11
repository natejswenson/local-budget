import pytest

from local_budget import db
from local_budget.connectors.plaid import store, sync
from local_budget.connectors.plaid.client import PlaidError
from local_budget.connectors.plaid.credentials import CredentialError
from local_budget.connectors.plaid.lifecycle import disconnect, rotate_token, transfer_ownership
from local_budget.connectors.plaid.types import LIFECYCLE_STATES
from .test_apply import enabled, apply
from .test_mapping import fact, mapped


class Vault:
    def __init__(self, ref):
        self.values = {ref: 'FABRICATED_ACCESS'}
        self.deleted = []
        self.fail_delete = False
        self.fail_put = False

    def get(self, handle, *, interactive=False):
        if handle not in self.values:
            raise CredentialError('credential_missing')
        return self.values[handle]

    def put(self, handle, value):
        if self.fail_put:
            raise CredentialError('credential_locked')
        self.values[handle] = value

    def delete(self, handle):
        if self.fail_delete:
            raise CredentialError('credential_locked')
        self.deleted.append(handle)
        self.values.pop(handle, None)


class Client:
    bytes_received = 0
    def __init__(self, reply=None, hook=None):
        self.reply = reply if reply is not None else {'request_id': 'safe'}
        self.calls = []
        self.hook = hook

    def call(self, endpoint, body, *, deadline=None):
        self.calls.append(endpoint)
        if self.hook:
            self.hook()
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def current(path, item):
    with db.connect(path) as conn:
        return store.item(conn, item)


def test_uncertain_removal_retains_recovery_credential(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('t1')))
    vault = Vault(current(plaid_db, item)['credential_ref'])
    def pending():
        with db.connect(plaid_db) as conn:
            assert store.item(conn, item)['state'] == 'disconnect_pending'
            assert conn.execute('SELECT enabled FROM plaid_accounts').fetchone()[0] == 0
    client = Client(PlaidError('transport_uncertain'), pending)
    assert disconnect(plaid_db, item, client, vault) == 'disconnect_pending'
    assert vault.deleted == []
    assert current(plaid_db, item)['state'] == 'disconnect_pending'
    client.reply = {'request_id': 'safe'}
    assert disconnect(plaid_db, item, client, vault) == 'disconnected'
    assert vault.deleted
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1
        assert store.public_status(conn)[0]['state'] == 'revoked'
    assert disconnect(plaid_db, item, client, vault) == 'disconnected'
    assert len(client.calls) == 2


@pytest.mark.parametrize('reply', [PlaidError('INVALID_ACCESS_TOKEN'), None, {'removed': False}])
def test_only_confirmed_valid_remove_response_cleans_credentials(plaid_db, seed_item, reply):
    item = seed_item()
    vault = Vault(current(plaid_db, item)['credential_ref'])
    client = Client()
    client.reply = reply
    assert disconnect(plaid_db, item, client, vault) == 'disconnect_pending'
    assert vault.deleted == []


def test_cleanup_retry_never_repeats_remote_remove(plaid_db, seed_item):
    item = seed_item()
    vault = Vault(current(plaid_db, item)['credential_ref'])
    vault.fail_delete = True
    client = Client()
    assert disconnect(plaid_db, item, client, vault) == 'cleanup_needed'
    vault.fail_delete = False
    assert disconnect(plaid_db, item, client, vault) == 'disconnected'
    assert client.calls == ['/item/remove']


def test_missing_credential_retains_pending_and_never_links(plaid_db, seed_item):
    item = seed_item()
    vault = Vault('other')
    client = Client()
    assert disconnect(plaid_db, item, client, vault) == 'disconnect_pending'
    assert client.calls == []
    assert current(plaid_db, item)['safe_error_code'] == 'credential_missing'


def test_rotation_new_handle_precedes_switch_and_old_cleanup(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('t1')))
    old = current(plaid_db, item)
    vault = Vault(old['credential_ref'])
    def receipt():
        with db.connect(plaid_db) as conn:
            row = conn.execute('SELECT * FROM plaid_rotation_receipts').fetchone()
            assert row['state'] == 'requested'
            assert store.item(conn, item)['credential_ref'] == old['credential_ref']
    client = Client({'new_access_token': 'FABRICATED_NEW'}, receipt)
    assert rotate_token(plaid_db, item, client, vault) == 'active'
    new = current(plaid_db, item)
    assert new['credential_ref'] != old['credential_ref']
    assert vault.values[new['credential_ref']] == 'FABRICATED_NEW'
    assert vault.deleted == [old['credential_ref']]
    assert new['cursor'] == old['cursor']


def test_rotation_uncertain_or_failed_store_never_blindly_retries(plaid_db, seed_item):
    item = seed_item()
    vault = Vault(current(plaid_db, item)['credential_ref'])
    vault.fail_put = True
    client = Client({'new_access_token': 'FABRICATED_NEW'})
    assert rotate_token(plaid_db, item, client, vault) == 'recovery_required'
    vault.fail_put = False
    assert rotate_token(plaid_db, item, client, vault) == 'recovery_required'
    assert client.calls == ['/item/access_token/invalidate']
    assert vault.deleted == []


def test_rotation_cleanup_recovers_without_provider(plaid_db, seed_item):
    item = seed_item()
    vault = Vault(current(plaid_db, item)['credential_ref'])
    vault.fail_delete = True
    client = Client({'new_access_token': 'FABRICATED_NEW'})
    assert rotate_token(plaid_db, item, client, vault) == 'cleanup_needed'
    vault.fail_delete = False
    assert rotate_token(plaid_db, item, client, vault) == 'disabled'
    assert client.calls == ['/item/access_token/invalidate']


def test_rotation_crash_after_keychain_store_recovers(plaid_db, seed_item, monkeypatch):
    from local_budget.connectors.plaid import lifecycle
    item = seed_item()
    old = current(plaid_db, item)['credential_ref']
    vault = Vault(old)
    client = Client({'new_access_token': 'FABRICATED_NEW'})
    real_switch = lifecycle._switch_rotation
    monkeypatch.setattr(lifecycle, '_switch_rotation', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
    assert rotate_token(plaid_db, item, client, vault) == 'recovery_required'
    assert current(plaid_db, item)['credential_ref'] == old
    monkeypatch.setattr(lifecycle, '_switch_rotation', real_switch)
    assert rotate_token(plaid_db, item, client, vault) == 'disabled'
    assert len(client.calls) == 1


def test_replacement_transfers_only_explicit_unique_exact_candidates(plaid_db, seed_item):
    old = seed_item('old')
    new = seed_item('new')
    enabled(plaid_db, old)
    apply(plaid_db, old, ('added', fact('old-t')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
        store.save_fact(conn, item_id=new, fact=fact('new-t'), disposition='excluded')
        txn = conn.execute('SELECT txn_id FROM transactions').fetchone()[0]
        transfer_ownership(conn, old_item_id=old, new_item_id=new, matches={'new-t': txn}, reviewed_generation=1)
        assert store.item(conn, old)['state'] == 'replaced'
        assert store.item(conn, new)['state'] == 'disabled'
        rows = conn.execute('SELECT item_id,active_owner,local_txn_id FROM plaid_transactions ORDER BY item_id').fetchall()
        assert [(r['active_owner'], r['local_txn_id']) for r in rows] == [(0, txn), (1, txn)]
        assert conn.execute('SELECT enabled FROM plaid_accounts WHERE item_id=?', (new,)).fetchone()[0] == 0
        assert store.is_provider_owned(conn, txn)
    with pytest.raises(sync.SyncStateError, match='item_disabled'):
        apply(plaid_db, old, ('modified', fact('old-t', amount_cents=-99)), cursor='next', generation=3)
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT amount_cents FROM transactions').fetchone()[0] == -1200
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='stale_generation'):
            transfer_ownership(conn, old_item_id=old, new_item_id=new, matches={}, reviewed_generation=1)


def test_enum_and_receipts_private(plaid_db, seed_item):
    assert LIFECYCLE_STATES == frozenset({'disabled','active','reconnect_required','recovery_required','disconnect_pending','disconnected','replaced','cleanup_needed'})
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='invalid_state'):
            store.set_lifecycle(conn, item, 'healthy')
    with db.agent_connect(plaid_db) as conn:
        with pytest.raises(Exception):
            conn.execute('SELECT * FROM plaid_rotation_receipts').fetchall()


def test_rotation_unknown_remote_failure_keeps_receipt(plaid_db, seed_item):
    item = seed_item()
    vault = Vault(current(plaid_db, item)['credential_ref'])
    client = Client(PlaidError('TRANSPORT_ERROR'))
    assert rotate_token(plaid_db, item, client, vault) == 'recovery_required'
    client.reply = {'new_access_token': 'FABRICATED_NEW'}
    assert rotate_token(plaid_db, item, client, vault) == 'recovery_required'
    assert len(client.calls) == 1 and vault.deleted == []
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT state FROM plaid_rotation_receipts').fetchone()[0] == 'requested'


@pytest.mark.parametrize('problem', ['unknown', 'duplicate', 'changed'])
def test_transfer_rejects_invalid_matches_without_partial_writes(plaid_db, seed_item, problem):
    old, new = seed_item('old'), seed_item('new')
    enabled(plaid_db, old)
    apply(plaid_db, old, ('added', fact('old-t')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
        store.save_fact(conn, item_id=new, fact=fact('new-t', amount_cents=-99 if problem == 'changed' else -1200), disposition='excluded')
        tid = conn.execute('SELECT txn_id FROM transactions').fetchone()[0]
        matches = {'unknown': tid} if problem == 'unknown' else {'new-t': tid, 'other': tid} if problem == 'duplicate' else {'new-t': tid}
        with pytest.raises(ValueError, match='stale_review|duplicate_match'):
            transfer_ownership(conn, old_item_id=old, new_item_id=new, matches=matches, reviewed_generation=1)
        assert store.item(conn, old)['state'] == 'active'
        assert conn.execute('SELECT item_id FROM plaid_transactions WHERE active_owner=1').fetchone()[0] == old
        assert conn.execute('SELECT COUNT(*) FROM plaid_changes WHERE event_kind=\'ownership_transferred\'').fetchone()[0] == 0


def test_partial_transfer_can_be_reviewed_again_and_preserves_approval(plaid_db, seed_item):
    from local_budget.connectors.plaid.types import SyncBatch
    old, new = seed_item('old'), seed_item('new')
    enabled(plaid_db, old)
    apply(plaid_db, old, ('added', fact('old-1')), ('added', fact('old-2', merchant_norm='OTHER')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
        store.save_fact(conn, item_id=new, fact=fact('new-1'), disposition='excluded')
        store.save_fact(conn, item_id=new, fact=fact('new-2', merchant_norm='OTHER'), disposition='excluded')
        tids = [r[0] for r in conn.execute('SELECT txn_id FROM transactions ORDER BY txn_id')]
        transfer_ownership(conn, old_item_id=old, new_item_id=new, matches={'new-1': tids[0]}, reviewed_generation=1)
        assert store.item(conn, new)['state'] == 'disabled'
        assert store._account(conn, new, 'a1')['enabled'] == 0
        transfer_ownership(conn, old_item_id=old, new_item_id=new, matches={'new-2': tids[1]}, reviewed_generation=2)
        store.enable_account(conn, item_id=new, provider_account_id='a1', reviewed_generation=3,
                             batch=SyncBatch(None, 'start', (('added', fact('new-1')), ('added', fact('new-2', merchant_norm='OTHER'))), 'HISTORICAL_UPDATE_COMPLETE', None))
    apply(plaid_db, new, ('modified', fact('new-1', pending=True)), generation=4)
    apply(plaid_db, new, ('removed', 'new-1'), cursor='next', generation=4)
    apply(plaid_db, new, ('added', fact('new-1', posted_date='2026-09-30')), cursor='next', generation=4)
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT disposition FROM plaid_transactions WHERE item_id=? AND transaction_id=?', (new, 'new-1')).fetchone()[0] == 'adopted'
        assert conn.execute('SELECT status FROM transactions WHERE txn_id=?', (tids[0],)).fetchone()[0] == 'posted'
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 2


def test_rotation_cannot_resume_after_confirmed_disconnect_cleanup(plaid_db, seed_item):
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('owned')))
    old_ref = current(plaid_db, item)['credential_ref']
    vault = Vault(old_ref)
    vault.fail_delete = True
    client = Client({'new_access_token': 'FABRICATED_NEW'})
    assert rotate_token(plaid_db, item, client, vault) == 'cleanup_needed'
    new_ref = current(plaid_db, item)['credential_ref']
    client.reply = {'request_id': 'safe'}
    assert disconnect(plaid_db, item, client, vault) == 'cleanup_needed'
    before = current(plaid_db, item)
    assert before['safe_error_code'] == 'removal_confirmed'
    with db.connect(plaid_db) as conn:
        receipt = dict(conn.execute('SELECT * FROM plaid_rotation_receipts WHERE item_id=?', (item,)).fetchone())
        assert receipt['state'] == 'switched'
        assert conn.execute('SELECT enabled FROM plaid_accounts WHERE item_id=?', (item,)).fetchone()[0] == 0
    vault.fail_delete = False
    calls = list(client.calls)
    assert rotate_token(plaid_db, item, client, vault) == 'cleanup_needed'
    assert current(plaid_db, item) == before
    assert vault.deleted == []
    assert client.calls == calls
    with db.connect(plaid_db) as conn:
        assert dict(conn.execute('SELECT * FROM plaid_rotation_receipts WHERE item_id=?', (item,)).fetchone()) == receipt
        assert conn.execute('SELECT enabled FROM plaid_accounts WHERE item_id=?', (item,)).fetchone()[0] == 0
        assert conn.execute('SELECT amount_cents,status FROM transactions').fetchone()[:] == (-1200, 'posted')
    assert before['credential_ref'] == new_ref
    assert disconnect(plaid_db, item, client, vault) == 'disconnected'
    assert client.calls == calls == ['/item/access_token/invalidate', '/item/remove']
    assert set(vault.deleted) == {old_ref, new_ref}
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plaid_rotation_receipts').fetchone()[0] == 0
        assert conn.execute('SELECT enabled FROM plaid_accounts WHERE item_id=?', (item,)).fetchone()[0] == 0
