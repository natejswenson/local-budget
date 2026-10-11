"""Retrieve bounded cursor batches and commit ledger corrections atomically."""
from __future__ import annotations

import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .client import PlaidClient, PlaidError, _request_id, normalize_txn
from .types import SyncBatch, SyncResult
from .credentials import KeychainCredentials

_monotonic = time.monotonic
_READINESS = frozenset({'NOT_READY', 'INITIAL_UPDATE_COMPLETE', 'HISTORICAL_UPDATE_COMPLETE'})


def check_fetch_limits(*, updates: int, response_bytes: int,
                       deadline: float, now: float) -> None:
    if now >= deadline:
        raise PlaidError('sync_deadline')
    if updates > 50_000 or response_bytes > 64 * 1024 * 1024:
        raise PlaidError('sync_limit')


def _identifier(value) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 256
            and all(ord(c) >= 32 and ord(c) != 127 for c in value))


def fetch_batch(client: PlaidClient, access_token: str, *, cursor: str | None,
                deadline: float) -> SyncBatch:
    """Restart pagination from the original cursor, sharing all operation budgets."""
    now = _monotonic()
    if type(deadline) not in (int, float) or not math.isfinite(deadline):
        raise PlaidError('INVALID_REQUEST')
    end = min(deadline, now + 300, getattr(client, '_deadline', deadline),
              getattr(client, '_sync_deadline', deadline))
    client._sync_deadline = end
    client.updates_received = getattr(client, 'updates_received', 0)
    client.pagination_restarts = getattr(client, 'pagination_restarts', 0)
    current = cursor
    seen_cursors = {cursor}
    events = []
    while True:
        check_fetch_limits(updates=client.updates_received,
                           response_bytes=client.bytes_received, deadline=end, now=_monotonic())
        body = {'access_token': access_token, 'count': 500}
        if current is not None:
            body['cursor'] = current
        restart = False
        try:
            page = client.call('/transactions/sync', body, deadline=end)
        except PlaidError as error:
            if (error.code != 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'
                    or client.pagination_restarts >= 3):
                raise
            restart = True
        if restart:
            client.pagination_restarts += 1
            current = cursor
            seen_cursors = {cursor}
            events.clear()
            continue
        if (not isinstance(page, dict) or type(page.get('has_more')) is not bool
                or not isinstance(page.get('next_cursor'), str)
                or any(not isinstance(page.get(kind), list)
                       for kind in ('added', 'modified', 'removed'))):
            raise PlaidError('INVALID_RESPONSE')
        client.updates_received += sum(len(page[kind]) for kind in ('added', 'modified', 'removed'))
        check_fetch_limits(updates=client.updates_received,
                           response_bytes=client.bytes_received, deadline=end, now=_monotonic())
        page_ids = set()
        for kind in ('added', 'modified', 'removed'):
            for row in page[kind]:
                if not isinstance(row, dict) or not _identifier(row.get('transaction_id')):
                    raise PlaidError('INVALID_RESPONSE')
                txn_id = row['transaction_id']
                if txn_id in page_ids:
                    raise PlaidError('INVALID_RESPONSE')
                page_ids.add(txn_id)
                events.append((kind, txn_id if kind == 'removed' else normalize_txn(row)))
        next_cursor = page['next_cursor']
        check_fetch_limits(updates=client.updates_received,
                           response_bytes=client.bytes_received, deadline=end, now=_monotonic())
        if not page['has_more']:
            status = page.get('transactions_update_status')
            readiness = status if isinstance(status, str) and status in _READINESS else 'unknown'
            return SyncBatch(cursor, next_cursor, tuple(events), readiness, _request_id(page, body))
        if not next_cursor or next_cursor in seen_cursors:
            raise PlaidError('INVALID_RESPONSE')
        seen_cursors.add(next_cursor)
        current = next_cursor


class SyncStateError(Exception):
    """Stable safe codes only; never include provider facts or exception text."""
    def __init__(self, code: str):
        self.code = code if code in {
            'mapping_changed', 'cursor_changed', 'item_disabled', 'unknown_item',
            'invalid_batch', 'invalid_fact', 'unknown_account', 'account_changed',
            'inactive_owner', 'ownership_changed', 'transaction_required',
            'item_busy', 'intake_busy', 'sync_failed',
        } else 'sync_failed'
        super().__init__(self.code)


def require_generation(item, expected):
    if item['mapping_generation'] != expected:
        raise SyncStateError('mapping_changed')


def require_cursor(item, batch):
    if item['cursor'] != batch.original_cursor:
        raise SyncStateError('cursor_changed')


def final_events(batch):
    """Collapse by provider identity, preserving the final observation only."""
    from .types import TxnFact
    result = {}
    for operation, value in batch.events:
        if operation == 'removed' and _identifier(value):
            result[value] = (operation, value)
        elif operation in {'added', 'modified'} and isinstance(value, TxnFact) and _identifier(value.transaction_id):
            result[value.transaction_id] = (operation, value)
        else:
            raise SyncStateError('invalid_batch')
    return result


def _validate_fact(fact):
    from datetime import date
    try:
        valid = (isinstance(fact.posted_date, str) and date.fromisoformat(fact.posted_date).isoformat() == fact.posted_date
                 and type(fact.amount_cents) is int and -(2**63) <= fact.amount_cents < 2**63
                 and type(fact.pending) is bool and fact.currency == 'USD' and _identifier(fact.account_id)
                 and isinstance(fact.merchant_norm, str) and len(fact.merchant_norm) <= 200
                 and isinstance(fact.payee, str) and len(fact.payee) <= 200)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SyncStateError('invalid_fact')


def _eligible_item(current):
    # New linked Items remain disabled until a mapping is explicitly enabled.
    if current['state'] not in {'disabled', 'active'}:
        raise SyncStateError('item_disabled')


def _facts_json(row):
    import json
    return json.dumps({key: row[key] for key in ('posted_date', 'amount_cents', 'merchant_norm', 'status')})


def apply_batch(conn: sqlite3.Connection, item_id: int, batch: SyncBatch, *, expected_generation: int,
                deadline: float | None = None) -> SyncResult:
    """Caller holds Item/intake locks and BEGIN IMMEDIATE through commit/rollback."""
    import hashlib
    import json
    from datetime import datetime, timezone
    from ...categorize import rules
    from . import store, derived

    require_deadline(deadline)
    if not conn.in_transaction:
        raise SyncStateError('transaction_required')
    try:
        current = store.item(conn, item_id)
    except KeyError:
        raise SyncStateError('unknown_item') from None
    require_generation(current, expected_generation)
    require_cursor(current, batch)
    _eligible_item(current)
    if (not isinstance(batch.next_cursor, str) or batch.readiness not in _READINESS | {'unknown'}):
        raise SyncStateError('invalid_batch')
    final = final_events(batch)
    accounts = {r['provider_account_id']: dict(r) for r in conn.execute('SELECT * FROM plaid_accounts WHERE item_id=?', (item_id,))}
    if not any(a['enabled'] and a['history_approved'] for a in accounts.values()):
        raise SyncStateError('item_disabled')
    for account in accounts.values():
        if account['enabled']:
            compatible = bool(account['history_approved'] and account['local_account_id'] is not None
                              and account['currency'] == 'USD' and account['account_type'] is not None
                              and account['start_date'] is not None)
            if compatible:
                try:
                    store._assert_account_type(conn, account['local_account_id'], account['account_type'])
                except ValueError:
                    compatible = False
            if not compatible:
                raise SyncStateError('account_changed')
    prepared = []
    # Validate every final observation and ownership relationship before writes.
    for provider_id, (operation, value) in final.items():
        require_deadline(deadline)
        mapping = conn.execute('SELECT * FROM plaid_transactions WHERE item_id=? AND transaction_id=?', (item_id, provider_id)).fetchone()
        old = None
        if mapping is not None and mapping['local_txn_id'] is not None:
            if not mapping['active_owner']:
                raise SyncStateError('inactive_owner')
            old = conn.execute('SELECT * FROM transactions WHERE txn_id=?', (mapping['local_txn_id'],)).fetchone()
            if old is None or conn.execute('SELECT 1 FROM plaid_transactions WHERE local_txn_id=? AND active_owner=1 AND NOT(item_id=? AND transaction_id=?)', (mapping['local_txn_id'], item_id, provider_id)).fetchone():
                raise SyncStateError('ownership_changed')
        if operation != 'removed':
            _validate_fact(value)
            account = accounts.get(value.account_id)
            if account is None:
                raise SyncStateError('unknown_account')
            if (account['currency'] != 'USD' or account['account_type'] is None
                    or (mapping is not None and mapping['provider_account_id'] is not None and mapping['provider_account_id'] != value.account_id)):
                raise SyncStateError('account_changed')
            if old is not None and old['account_id'] != account['local_account_id']:
                raise SyncStateError('ownership_changed')
        else:
            account = accounts.get(mapping['provider_account_id']) if mapping else None
        if old is not None and (account is None or not account['enabled'] or not account['history_approved']):
            raise SyncStateError('account_changed')
        prepared.append((provider_id, operation, value, mapping, old, account))
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    run_id = conn.execute('INSERT INTO plaid_sync_runs(item_id,started_at) VALUES(?,?) RETURNING run_id', (item_id, stamp)).fetchone()[0]
    rules.seed_builtin_rules(conn)
    counts = dict(inserted=0, updated=0, removed=0)
    for provider_id, operation, value, mapping, old, account in prepared:
        require_deadline(deadline)
        txn_id = old['txn_id'] if old else None
        archived_splits = archived_matches = None
        split_review = mapping['split_needs_review'] if mapping else 0
        if operation == 'removed':
            if old is not None and old['status'] != 'removed':
                archived_splits, archived_matches, review = derived.invalidate(conn, old, None, status='removed')
                split_review |= review
                conn.execute("UPDATE transactions SET status='removed' WHERE txn_id=?", (txn_id,))
                counts['removed'] += 1
            conn.execute("INSERT INTO plaid_transactions(item_id,transaction_id,disposition) VALUES(?,?,'removed') "
                         "ON CONFLICT(item_id,transaction_id) DO UPDATE SET disposition=CASE WHEN disposition IN ('adopted','pending_adopted','removed_adopted') THEN 'removed_adopted' ELSE 'removed' END,review_required=0,split_needs_review=?",
                         (item_id, provider_id, split_review))
        else:
            adopted = mapping is not None and mapping['disposition'] in {'adopted', 'pending_adopted', 'removed_adopted'}
            eligible = store.eligible(value, account) or (adopted and not value.pending and account['enabled'])
            if eligible:
                disposition = 'adopted' if adopted else 'posted'
            elif value.pending:
                disposition = 'pending_adopted' if adopted else 'pending'
            else:
                disposition = 'excluded'
            status = 'posted' if eligible else 'pending' if value.pending else 'removed'
            if old is not None:
                changed = old['status'] != status or any(old[key] != getattr(value, key) for key in ('posted_date', 'amount_cents', 'merchant_norm', 'payee'))
                if changed:
                    archived_splits, archived_matches, review = derived.invalidate(conn, old, value, status=status)
                    split_review |= review
                    cat, sub, source, canonical = derived.categories(conn, value, old)
                    conn.execute('UPDATE transactions SET posted_date=?,amount_cents=?,merchant_norm=?,payee=?,status=?,category=?,subcategory=?,category_source=?,canonical_merchant=? WHERE txn_id=?',
                                 (value.posted_date, value.amount_cents, value.merchant_norm, value.payee, status, cat, sub, source, canonical, txn_id))
                    counts['updated'] += 1
            elif eligible:
                identity = json.dumps([current['environment'], item_id, provider_id])
                fitid = 'plaid-' + hashlib.sha256(identity.encode()).hexdigest()
                cat, sub, source, canonical = derived.categories(conn, value)
                txn_id = conn.execute('INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,payee,merchant_norm,category,subcategory,category_source,canonical_merchant,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING txn_id',
                                      (account['local_account_id'], fitid, value.posted_date, value.amount_cents, value.payee, value.merchant_norm, cat, sub, source, canonical, stamp)).fetchone()[0]
                counts['inserted'] += 1
            store.save_fact(conn, item_id=item_id, fact=value, local_txn_id=txn_id,
                            active_owner=int(txn_id is not None), disposition=disposition, split_needs_review=split_review)
        after = conn.execute('SELECT * FROM transactions WHERE txn_id=?', (txn_id,)).fetchone() if txn_id else None
        conn.execute('INSERT INTO plaid_changes(run_id,local_txn_id,event_kind,before_facts,after_facts,archived_splits,archived_matches,created_at) VALUES(?,?,?,?,?,?,?,?)',
                     (run_id, txn_id, operation, _facts_json(old) if old else None, _facts_json(after) if after else None, archived_splits, archived_matches, stamp))
    review_count = conn.execute('SELECT COUNT(*) FROM plaid_transactions WHERE item_id=? AND review_required=1', (item_id,)).fetchone()[0]
    outcome = 'healthy' if batch.readiness == 'HISTORICAL_UPDATE_COMPLETE' else 'initializing'
    conn.execute('UPDATE plaid_sync_runs SET completed_at=?,outcome=?,inserted=?,updated=?,removed=?,review_count=?,request_id=?,readiness=? WHERE run_id=?',
                 (stamp, outcome, counts['inserted'], counts['updated'], counts['removed'], review_count, _request_id({'request_id': batch.request_id}, {}), batch.readiness, run_id))
    for account in accounts.values():
        if account['enabled']:
            local_review = conn.execute('SELECT COUNT(*) FROM plaid_transactions WHERE item_id=? AND provider_account_id=? AND review_required=1', (item_id, account['provider_account_id'])).fetchone()[0]
            conn.execute('INSERT INTO plaid_status_public(local_account_id,state,last_attempt_at,last_commit_at,readiness,review_count) VALUES(?,?,?,?,?,?) ON CONFLICT(local_account_id) DO UPDATE SET state=excluded.state,last_attempt_at=excluded.last_attempt_at,last_commit_at=excluded.last_commit_at,readiness=excluded.readiness,review_count=excluded.review_count,safe_error_code=NULL',
                         (account['local_account_id'], outcome, stamp, stamp, batch.readiness, local_review))
    require_deadline(deadline)
    # Last statement: all dependent writes are rolled back if cursor commit fails.
    conn.execute('UPDATE plaid_items SET cursor=?,state=?,last_attempt_at=?,last_commit_at=?,readiness=?,safe_error_code=NULL WHERE item_id=?',
                 (batch.next_cursor, 'active', stamp, stamp, batch.readiness, item_id))
    require_deadline(deadline)
    return SyncResult(outcome, **counts, review_count=review_count)


def fetch_review_batch(client, access_token, *, cursor, deadline):
    """Fresh full history plus saved deltas, sharing one transport and budget."""
    full = fetch_batch(client, access_token, cursor=None, deadline=deadline)
    if cursor is None:
        return full
    delta = fetch_batch(client, access_token, cursor=cursor, deadline=deadline)
    return SyncBatch(cursor, delta.next_cursor, full.events + delta.events, delta.readiness, delta.request_id)


def record_failure(conn, item_id, error, *, now):
    """Separate safe attempt after rollback; preserve cursor and successful times."""
    from .credentials import CredentialError
    from . import store
    code = error.code if isinstance(error, (PlaidError, CredentialError, SyncStateError)) else 'sync_failed'
    state = ('reconnect_required' if code in {'ITEM_LOGIN_REQUIRED', 'ITEM_LOCKED', 'INVALID_ACCESS_TOKEN', 'USER_PERMISSION_REVOKED', 'ACCESS_NOT_GRANTED'}
             else code if code in {'credential_locked', 'credential_missing'} else 'temporary_error')
    stamp = now.isoformat(timespec='seconds')
    current = store.item(conn, item_id)
    # A raced lifecycle transition must not be overwritten by an old failure.
    if current['state'] not in {'disabled', 'active'}:
        return
    private_state = ('recovery_required' if code == 'INVALID_FIELD' else
                     'reconnect_required' if state == 'reconnect_required' else current['state'])
    if code == 'INVALID_FIELD':
        state = 'link_recovery_required'
    conn.execute('INSERT INTO plaid_sync_runs(item_id,started_at,completed_at,outcome,safe_error_code) VALUES(?,?,?,\'failed\',?)', (item_id, stamp, stamp, code))
    conn.execute('UPDATE plaid_items SET last_attempt_at=?,state=?,safe_error_code=? WHERE item_id=?', (stamp, private_state, code, item_id))
    for row in conn.execute('SELECT local_account_id FROM plaid_accounts WHERE item_id=? AND enabled=1', (item_id,)):
        conn.execute('INSERT INTO plaid_status_public(local_account_id,state,last_attempt_at,safe_error_code) VALUES(?,?,?,?) ON CONFLICT(local_account_id) DO UPDATE SET state=excluded.state,last_attempt_at=excluded.last_attempt_at,safe_error_code=excluded.safe_error_code', (row[0], state, stamp, code))


def sync_item(db_path: Path, item_id: int, client: PlaidClient, vault: KeychainCredentials, *, now: datetime) -> SyncResult:
    """Fetch under Item lock, then acquire intake and atomically apply + commit."""
    from ... import db, inbox_adapter
    from . import store
    from .credentials import CredentialError
    from .locks import item_locks

    with item_locks(db_path, [item_id]) as acquired:
        if not acquired:
            raise SyncStateError('item_busy')
        try:
            with db.connect(db_path) as conn:
                snapshot = store.item(conn, item_id)
                _eligible_item(snapshot)
                if not conn.execute('SELECT 1 FROM plaid_accounts WHERE item_id=? AND enabled=1 AND history_approved=1', (item_id,)).fetchone():
                    raise SyncStateError('item_disabled')
            token = vault.get(snapshot['credential_ref'], interactive=False)
            batch = fetch_batch(client, token, cursor=snapshot['cursor'], deadline=_monotonic()+300)
            with inbox_adapter.intake_lock() as intake:
                if not intake:
                    raise SyncStateError('intake_busy')
                with db.connect(db_path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    result = apply_batch(conn, item_id, batch, expected_generation=snapshot['mapping_generation'],
                                         deadline=client._sync_deadline)
                    require_deadline(client._sync_deadline)
                return result
        except (PlaidError, CredentialError, SyncStateError) as error:
            failure = error
        except Exception:
            failure = SyncStateError('sync_failed')
        # Do not retain unknown provider/Vault/SQLite exception contexts.
        if failure.code not in {'item_disabled', 'item_busy', 'intake_busy'}:
            try:
                with inbox_adapter.intake_lock() as intake:
                    if intake:
                        with db.connect(db_path) as conn:
                            conn.execute('BEGIN IMMEDIATE')
                            record_failure(conn, item_id, failure, now=now)
            except Exception:
                pass  # Failure health is best effort; preserve the safe primary error.
        raise failure


def require_deadline(deadline: float | None) -> None:
    """Keep retrieval, validation and commit inside the same Item operation budget."""
    if deadline is not None and _monotonic() >= deadline:
        raise PlaidError('sync_deadline')
