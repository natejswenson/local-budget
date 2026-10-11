"""Recoverable host connection operations. Never return provider or secret data."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4

from local_budget import db, inbox_adapter
from . import store, sync
from .client import PlaidClient, PlaidError
from .credentials import CredentialError, KeychainCredentials
from .locks import item_locks
from .types import TxnFact


@contextmanager
def _write(path):
    # The outer operation owns Item locks through commit, including remote calls.
    with inbox_adapter.intake_lock() as acquired:
        if not acquired:
            raise sync.SyncStateError('intake_busy')
        with db.connect(path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn


def _cleanup(vault, handles):
    for handle in set(handles):
        try:
            vault.delete(handle)
        except CredentialError as error:
            if error.code != 'credential_missing':
                return False
        except Exception:
            return False
    return True


def _failure(path, item_id, state, error):
    code = error.code if isinstance(error, (CredentialError, PlaidError)) else 'lifecycle_failed'
    with _write(path) as conn:
        store.set_lifecycle(conn, item_id, state, safe_error_code=code)
    return state


def disconnect(db_path: Path, item_id: int, client: PlaidClient, vault: KeychainCredentials) -> str:
    """Persist local disable before remove; ambiguous removal retains credentials."""
    with item_locks(db_path, [item_id]) as acquired:
        if not acquired:
            raise sync.SyncStateError('item_busy')
        with _write(db_path) as conn:
            current = store.item(conn, item_id)
            if current['state'] == 'disconnected':
                return 'disconnected'
            rotation = conn.execute('SELECT * FROM plaid_rotation_receipts WHERE item_id=?', (item_id,)).fetchone()
            confirmed = current['state'] == 'cleanup_needed' and current['safe_error_code'] == 'removal_confirmed'
            if not confirmed:
                conn.execute('UPDATE plaid_accounts SET enabled=0 WHERE item_id=?', (item_id,))
                conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1 WHERE item_id=?', (item_id,))
                store.set_lifecycle(conn, item_id, 'disconnect_pending')
        if not confirmed:
            try:
                token = vault.get(current['credential_ref'], interactive=True)
                response = client.call('/item/remove', {'access_token': token}, deadline=time.monotonic()+300)
                # HTTP-success sanitized object, never INVALID_ACCESS_TOKEN alone.
                if not isinstance(response, dict) or not isinstance(response.get('request_id'), str):
                    raise PlaidError('INVALID_RESPONSE')
            except Exception as error:
                return _failure(db_path, item_id, 'disconnect_pending', error)
            with _write(db_path) as conn:
                store.set_lifecycle(conn, item_id, 'cleanup_needed', safe_error_code='removal_confirmed')
        handles = [current['credential_ref']]
        if rotation:
            handles.extend([rotation['old_ref'], rotation['new_ref']])
        if not _cleanup(vault, handles):
            return 'cleanup_needed'
        with _write(db_path) as conn:
            store.set_lifecycle(conn, item_id, 'disconnected')
            conn.execute('DELETE FROM plaid_rotation_receipts WHERE item_id=?', (item_id,))
        return 'disconnected'


def _switch_rotation(path, item_id, receipt):
    with _write(path) as conn:
        current = store.item(conn, item_id)
        if current['credential_ref'] not in {receipt['old_ref'], receipt['new_ref']}:
            raise ValueError('credential_changed')
        conn.execute('UPDATE plaid_items SET credential_ref=? WHERE item_id=?', (receipt['new_ref'], item_id))
        conn.execute("UPDATE plaid_rotation_receipts SET state='switched' WHERE item_id=?", (item_id,))
        store.set_lifecycle(conn, item_id, 'cleanup_needed', safe_error_code='rotation_cleanup')


def rotate_token(db_path: Path, item_id: int, client: PlaidClient, vault: KeychainCredentials) -> str:
    """Resume known local stages only; never repeat an ambiguous invalidation."""
    with item_locks(db_path, [item_id]) as acquired:
        if not acquired:
            raise sync.SyncStateError('item_busy')
        with _write(db_path) as conn:
            current = store.item(conn, item_id)
            row = conn.execute('SELECT * FROM plaid_rotation_receipts WHERE item_id=?', (item_id,)).fetchone()
            new_request = row is None
            if new_request:
                if current['state'] not in {'active', 'disabled'}:
                    return current['state']
                receipt = dict(old_ref=current['credential_ref'], new_ref=uuid4().hex,
                               resume_state=current['state'], state='requested')
                conn.execute('INSERT INTO plaid_rotation_receipts(item_id,old_ref,new_ref,resume_state,state,created_at) VALUES(?,?,?,?,?,?)',
                             (item_id, receipt['old_ref'], receipt['new_ref'], receipt['resume_state'], 'requested', datetime.now(timezone.utc).isoformat(timespec='seconds')))
                store.set_lifecycle(conn, item_id, 'recovery_required', safe_error_code='rotation_pending')
            else:
                receipt = dict(row)
                if current['state'] in {'disconnect_pending', 'disconnected', 'replaced'}:
                    return current['state']
        try:
            if new_request:
                token = vault.get(receipt['old_ref'], interactive=True)
                result = client.call('/item/access_token/invalidate', {'access_token': token}, deadline=time.monotonic()+300)
                value = result.get('new_access_token') if isinstance(result, dict) else None
                if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                    raise PlaidError('INVALID_RESPONSE')
                vault.put(receipt['new_ref'], value)
            if receipt['state'] != 'switched':
                # This also recovers a crash between Keychain put and DB receipt.
                vault.get(receipt['new_ref'], interactive=True)
                with _write(db_path) as conn:
                    conn.execute("UPDATE plaid_rotation_receipts SET state='stored' WHERE item_id=?", (item_id,))
                _switch_rotation(db_path, item_id, receipt)
        except Exception as error:
            return _failure(db_path, item_id, 'recovery_required', error)
        if not _cleanup(vault, [receipt['old_ref']]):
            return 'cleanup_needed'
        with _write(db_path) as conn:
            store.set_lifecycle(conn, item_id, receipt['resume_state'])
            conn.execute('DELETE FROM plaid_rotation_receipts WHERE item_id=?', (item_id,))
        return receipt['resume_state']


def transfer_ownership(conn, *, old_item_id: int, new_item_id: int,
                       matches: dict[str, int], reviewed_generation: int) -> None:
    """Caller holds sorted Item locks, intake lock, and immediate through commit.

    New normalized facts are excluded candidates. Explicit exact matches transfer
    identity; both source mappings stay disabled until ordinary history approval.
    """
    store._require_transaction(conn)
    old, new = store.item(conn, old_item_id), store.item(conn, new_item_id)
    if new['mapping_generation'] != reviewed_generation:
        raise ValueError('stale_generation')
    if (old_item_id == new_item_id or old['environment'] != new['environment']
            or old['state'] in {'disconnected', 'disconnect_pending', 'cleanup_needed'}
            or new['state'] != 'disabled'):
        raise ValueError('invalid_replacement')
    if not matches or len(set(matches.values())) != len(matches):
        raise ValueError('duplicate_match')
    prepared = []
    for provider_id, local_id in matches.items():
        incoming = conn.execute('SELECT * FROM plaid_transactions WHERE item_id=? AND transaction_id=?', (new_item_id, provider_id)).fetchone()
        owner = conn.execute('SELECT * FROM plaid_transactions WHERE item_id=? AND local_txn_id=? AND active_owner=1', (old_item_id, local_id)).fetchone()
        local = conn.execute('SELECT * FROM transactions WHERE txn_id=?', (local_id,)).fetchone()
        if incoming is None or incoming['disposition'] != 'excluded' or incoming['local_txn_id'] is not None or incoming['active_owner'] or owner is None or local is None:
            raise ValueError('stale_review')
        value = TxnFact(provider_id, incoming['provider_account_id'], incoming['posted_date'], incoming['amount_cents'], incoming['merchant_norm'], incoming['payee'], bool(incoming['pending']), incoming['currency'])
        sync._validate_fact(value)
        account = store._account(conn, new_item_id, value.account_id)
        store._assert_account_type(conn, local['account_id'], account['account_type'])
        if (value.pending or local['status'] != 'posted' or account['enabled']
                or account['currency'] != 'USD' or account['start_date'] is None
                or account['local_account_id'] != local['account_id']
                or any(local[key] != getattr(value, key) for key in ('posted_date', 'amount_cents', 'merchant_norm'))):
            raise ValueError('stale_review')
        if conn.execute('SELECT 1 FROM plaid_accounts WHERE local_account_id=? AND enabled=1 AND item_id NOT IN (?,?)', (local['account_id'], old_item_id, new_item_id)).fetchone():
            raise ValueError('active_source')
        prepared.append((provider_id, local_id, owner))
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    run = conn.execute("INSERT INTO plaid_sync_runs(item_id,started_at,completed_at,outcome) VALUES(?,?,?,'replaced') RETURNING run_id", (new_item_id, stamp, stamp)).fetchone()[0]
    for provider_id, local_id, owner in prepared:
        conn.execute('UPDATE plaid_transactions SET active_owner=0 WHERE item_id=? AND transaction_id=?', (old_item_id, owner['transaction_id']))
        conn.execute("UPDATE plaid_transactions SET local_txn_id=?,active_owner=1,disposition='adopted',split_needs_review=?,review_required=? WHERE item_id=? AND transaction_id=?", (local_id, owner['split_needs_review'], owner['review_required'], new_item_id, provider_id))
        conn.execute("INSERT INTO plaid_changes(run_id,local_txn_id,event_kind,created_at) VALUES(?,?,'ownership_transferred',?)", (run, local_id, stamp))
    conn.execute('UPDATE plaid_accounts SET enabled=0 WHERE item_id IN (?,?)', (old_item_id, new_item_id))
    conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1 WHERE item_id IN (?,?)', (old_item_id, new_item_id))
    store.set_lifecycle(conn, old_item_id, 'replaced')
    store.set_lifecycle(conn, new_item_id, 'disabled')


def apply_rebuild(conn, item_id, batch, *, snapshot, reviewed, deadline=None):
    """Commit a confirmed full-fetch rebuild without resetting a cursor first.

    Caller owns Item/intake locks and immediate; absent history is not removal.
    """
    store._require_transaction(conn)
    sync.require_deadline(deadline)
    current = store.item(conn, item_id)
    if (current['state'] != 'recovery_required' or snapshot['state'] != current['state']
            or current['cursor'] != snapshot['cursor']
            or current['mapping_generation'] != snapshot['mapping_generation']):
        raise ValueError('stale_review')
    if batch.readiness != 'HISTORICAL_UPDATE_COMPLETE':
        raise ValueError('history_incomplete')
    if store.preview(conn, item_id, batch) != reviewed:
        raise ValueError('stale_review')
    if reviewed['unresolved_overlap_count']:
        raise ValueError('unresolved_overlap')
    # SAVEPOINT also preserves recovery state if a library caller catches errors.
    conn.execute('SAVEPOINT plaid_rebuild')
    try:
        store.set_lifecycle(conn, item_id, 'disabled')
        result = sync.apply_batch(conn, item_id, batch, expected_generation=current['mapping_generation'], deadline=deadline)
        sync.require_deadline(deadline)
    except Exception:
        conn.execute('ROLLBACK TO plaid_rebuild')
        conn.execute('RELEASE plaid_rebuild')
        raise
    conn.execute('RELEASE plaid_rebuild')
    return result
