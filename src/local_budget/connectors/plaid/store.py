"""Private persistence and explicitly allowlisted public connection health."""
from datetime import datetime, timezone
import sqlite3

from .types import SyncBatch, TxnFact


def add_item(conn: sqlite3.Connection, *, environment: str,
             provider_item_id: str, credential_ref: str) -> int:
    """Create a disabled connection within the caller's transaction."""
    row = conn.execute(
        'INSERT INTO plaid_items(environment,provider_item_id,credential_ref,user_handle,created_at) '
        'VALUES(?,?,?,?,?) RETURNING item_id',
        (environment, provider_item_id, credential_ref, user_handle(conn),
         datetime.now(timezone.utc).isoformat(timespec='seconds')),
    ).fetchone()
    return row[0]


def item(conn: sqlite3.Connection, item_id: int) -> dict:
    """Internal metadata only; callers must not log or publicly serialize it."""
    row = conn.execute('SELECT * FROM plaid_items WHERE item_id=?', (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    return dict(row)


def public_status(conn: sqlite3.Connection) -> list[dict]:
    """Read health without provider calls or exposing private identifiers."""
    return [dict(row) for row in conn.execute(
        'SELECT local_account_id,state,last_attempt_at,last_commit_at,'
        'provider_updated_at,readiness,review_count,safe_error_code '
        'FROM plaid_status_public ORDER BY local_account_id'
    )]


def namespace(conn: sqlite3.Connection) -> str:
    """The persisted ledger identity, retained in consistent database backups."""
    return conn.execute("SELECT value FROM plaid_meta WHERE key='ledger_namespace'").fetchone()[0]


def user_handle(conn: sqlite3.Connection) -> str:
    """Stable opaque user identity shared by links in this ledger."""
    return conn.execute("SELECT value FROM plaid_meta WHERE key='user_handle'").fetchone()[0]


def add_link_session(conn: sqlite3.Connection, *, session_id: str, mode: str,
                     item_id: int | None, credential_ref: str, expires_at: str,
                     environment: str | None = None) -> None:
    """Persist a nonsecret receipt; new links explicitly bind their environment."""
    if mode not in {'link', 'update'} or (mode == 'update') != (item_id is not None):
        raise ValueError('invalid_session')
    if item_id is not None:
        item_environment = item(conn, item_id)['environment']
        if environment is not None and environment != item_environment:
            raise ValueError('invalid_environment')
        environment = item_environment
    if environment not in {'sandbox', 'production'}:
        raise ValueError('invalid_environment')
    conn.execute(
        'INSERT INTO plaid_link_sessions '
        '(session_id,environment,mode,item_id,credential_ref,expires_at,created_at) '
        'VALUES(?,?,?,?,?,?,?)',
        (session_id, environment, mode, item_id, credential_ref, expires_at,
         datetime.now(timezone.utc).isoformat(timespec='seconds')),
    )


# All mapping mutators require caller-owned Item locks and BEGIN IMMEDIATE
# BEFORE the first dependent read, held through caller commit/rollback. They
# never start/commit transactions or acquire/release locks themselves. The
# in_transaction guard is structural only: it cannot prove an immediate reserve.
def _require_transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise ValueError('transaction_required')


def _account(conn, item_id, provider_account_id) -> dict:
    row = conn.execute('SELECT * FROM plaid_accounts WHERE item_id=? AND provider_account_id=?',
                       (item_id, provider_account_id)).fetchone()
    if row is None:
        raise ValueError('unknown_account')
    return dict(row)


def discover_accounts(conn: sqlite3.Connection, *, item_id: int, accounts: list[dict]) -> None:
    """Normalize /accounts/get accounts; never persist names, balances or raw data.

    Caller holds Item lock and immediate transaction. Existing mappings remain
    intact; changed compatibility metadata disables them and invalidates review.
    """
    _require_transaction(conn)
    item(conn, item_id)
    for account in accounts:
        provider_id = account['account_id']
        account_type = (account.get('subtype') if account.get('type') == 'depository'
                        else 'credit' if account.get('type') == 'credit' else None)
        if account_type not in {'checking', 'savings', 'money market', 'credit'}:
            account_type = None
        currency = account.get('balances', {}).get('iso_currency_code') or 'UNKNOWN'
        mask = account.get('mask')
        existing = conn.execute('SELECT * FROM plaid_accounts WHERE item_id=? AND provider_account_id=?',
                                (item_id, provider_id)).fetchone()
        changed = existing is not None and (existing['currency'], existing['account_type']) != (currency, account_type)
        conn.execute('INSERT INTO plaid_accounts(item_id,provider_account_id,currency,account_type,mask) '
                     'VALUES(?,?,?,?,?) ON CONFLICT(item_id,provider_account_id) DO UPDATE SET '
                     'currency=excluded.currency,account_type=excluded.account_type,mask=excluded.mask',
                     (item_id, provider_id, currency, account_type, mask))
        if changed:
            conn.execute('UPDATE plaid_accounts SET enabled=0,history_approved=0 WHERE item_id=? AND provider_account_id=?',
                         (item_id, provider_id))
            conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1 WHERE item_id=?', (item_id,))


def create_local_account(conn: sqlite3.Connection, *, item_id: int, provider_account_id: str) -> int:
    """Create an unmapped provider-only identity, never derive identity from mask."""
    import hashlib
    import hmac
    import json
    from ... import db

    _require_transaction(conn)
    account = _account(conn, item_id, provider_account_id)
    if account['currency'] != 'USD' or account['account_type'] is None:
        raise ValueError('unsupported_account')
    identity = json.dumps(['plaid', namespace(conn), item(conn, item_id)['environment'],
                           item(conn, item_id)['provider_item_id'], provider_account_id])
    account_hash = hmac.new(db.get_or_create_local_key(), identity.encode(), hashlib.sha256).hexdigest()
    conn.execute('INSERT OR IGNORE INTO accounts(institution,acct_type,acct_last4,acct_hash,created_at) VALUES(?,?,?,?,?)',
                 ('Plaid', account['account_type'], account['mask'], account_hash,
                  datetime.now(timezone.utc).isoformat(timespec='seconds')))
    return conn.execute('SELECT account_id FROM accounts WHERE acct_hash=?', (account_hash,)).fetchone()[0]


def _assert_account_type(conn: sqlite3.Connection, local_account_id: int, provider_type: str) -> None:
    local = conn.execute('SELECT acct_type FROM accounts WHERE account_id=?', (local_account_id,)).fetchone()
    local_type = (local['acct_type'] or '').lower() if local else ''
    if local_type in {'creditcard', 'card'}:
        local_type = 'credit'
    if local_type != provider_type:
        raise ValueError('account_type_mismatch')


def map_account(conn: sqlite3.Connection, *, item_id: int, provider_account_id: str,
                local_account_id: int, currency: str, start_date: str) -> None:
    """Set an explicitly supplied inclusive boundary; always disable until review."""
    from datetime import date

    _require_transaction(conn)
    try:
        if date.fromisoformat(start_date).isoformat() != start_date:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('invalid_start_date') from None
    account = _account(conn, item_id, provider_account_id)
    if currency != 'USD' or account['currency'] != currency or account['account_type'] is None:
        raise ValueError('unsupported_account')
    _assert_account_type(conn, local_account_id, account['account_type'])
    if conn.execute('SELECT 1 FROM plaid_accounts WHERE local_account_id=? AND enabled=1 '
                    'AND NOT (item_id=? AND provider_account_id=?)',
                    (local_account_id, item_id, provider_account_id)).fetchone():
        raise ValueError('active_source')
    conn.execute('UPDATE plaid_accounts SET local_account_id=?,start_date=?,enabled=0,history_approved=0 '
                 'WHERE item_id=? AND provider_account_id=?',
                 (local_account_id, start_date, item_id, provider_account_id))
    conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1 WHERE item_id=?', (item_id,))


def eligible(fact: TxnFact, account: dict) -> bool:
    return (account['enabled'] == 1 and not fact.pending and fact.currency == 'USD'
            and account['start_date'] is not None and fact.posted_date >= account['start_date'])


def preview(conn: sqlite3.Connection, item_id: int, batch: SyncBatch) -> dict:
    """Read a completed SyncBatch without writing facts, reviews or cursor.

    Results contain financial/private IDs for interactive review only. Candidates
    are advisory. Replayed events collapse by provider ID, never by charge facts.
    """
    current = item(conn, item_id)
    if current['cursor'] != batch.original_cursor:
        raise ValueError('stale_cursor')
    facts = {}
    for operation, value in batch.events:
        if operation == 'removed':
            facts.pop(value, None)
        elif operation in {'added', 'modified'} and isinstance(value, TxnFact):
            facts[value.transaction_id] = value
        else:
            raise ValueError('invalid_batch')
    rows = []
    for fact in facts.values():
        account = conn.execute('SELECT * FROM plaid_accounts WHERE item_id=? AND provider_account_id=?',
                               (item_id, fact.account_id)).fetchone()
        reason = None
        if account is None or account['local_account_id'] is None or account['start_date'] is None:
            reason = 'unmapped'
        elif fact.pending:
            reason = 'pending'
        elif fact.currency != 'USD' or account['currency'] != 'USD':
            reason = 'currency'
        elif fact.posted_date < account['start_date']:
            reason = 'before_start'
        owned = conn.execute('SELECT local_txn_id FROM plaid_transactions WHERE item_id=? AND transaction_id=? '
                             'AND active_owner=1', (item_id, fact.transaction_id)).fetchone()
        candidates = []
        if reason is None and owned is None:
            candidates = [r[0] for r in conn.execute(
                "SELECT txn_id FROM transactions WHERE account_id=? AND posted_date=? AND amount_cents=? "
                "AND merchant_norm=? AND status='posted' ORDER BY txn_id",
                (account['local_account_id'], fact.posted_date, fact.amount_cents, fact.merchant_norm))]
        rows.append(dict(transaction_id=fact.transaction_id, provider_account_id=fact.account_id,
                         posted_date=fact.posted_date, amount_cents=fact.amount_cents,
                         merchant_norm=fact.merchant_norm, candidate_txn_ids=candidates, excluded_reason=reason))
    dates = [r['posted_date'] for r in rows]
    return dict(mapping_generation=current['mapping_generation'], readiness=batch.readiness,
                returned_coverage=dict(first_date=min(dates) if dates else None,
                                       last_date=max(dates) if dates else None),
                transactions=rows, unresolved_overlap_count=sum(bool(r['candidate_txn_ids']) for r in rows),
                incoming_total_cents=sum(r['amount_cents'] for r in rows if r['excluded_reason'] is None))


def enable_account(conn: sqlite3.Connection, *, item_id: int, provider_account_id: str,
                   reviewed_generation: int, batch: SyncBatch) -> None:
    """Activate after fresh full history review inside caller's protected transaction."""
    _require_transaction(conn)
    if item(conn, item_id)['mapping_generation'] != reviewed_generation:
        raise ValueError('stale_generation')
    if batch.readiness != 'HISTORICAL_UPDATE_COMPLETE':
        raise ValueError('history_incomplete')
    account = _account(conn, item_id, provider_account_id)
    if account['local_account_id'] is None or account['start_date'] is None or account['currency'] != 'USD' or account['account_type'] is None:
        raise ValueError('unmapped_account')
    _assert_account_type(conn, account['local_account_id'], account['account_type'])
    review = preview(conn, item_id, batch)
    if any(r['candidate_txn_ids'] for r in review['transactions'] if r['provider_account_id'] == provider_account_id):
        raise ValueError('unresolved_overlap')
    if conn.execute('SELECT 1 FROM plaid_accounts WHERE local_account_id=? AND enabled=1 '
                    'AND NOT (item_id=? AND provider_account_id=?)',
                    (account['local_account_id'], item_id, provider_account_id)).fetchone():
        raise ValueError('active_source')
    conn.execute('UPDATE plaid_accounts SET enabled=1,history_approved=1 WHERE item_id=? AND provider_account_id=?',
                 (item_id, provider_account_id))
    conn.execute('UPDATE plaid_items SET mapping_generation=mapping_generation+1 WHERE item_id=?', (item_id,))


def save_fact(conn, *, item_id, fact, local_txn_id=None, active_owner=0, disposition,
              split_needs_review=0):
    """Retain normalized private observations; caller owns locks and transaction."""
    conn.execute('INSERT INTO plaid_transactions(item_id,transaction_id,provider_account_id,'
                 'local_txn_id,active_owner,disposition,posted_date,amount_cents,merchant_norm,payee,pending,currency,split_needs_review,review_required) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id,transaction_id) DO UPDATE SET '
                 'provider_account_id=excluded.provider_account_id,local_txn_id=excluded.local_txn_id,'
                 'active_owner=excluded.active_owner,disposition=excluded.disposition,posted_date=excluded.posted_date,'
                 'amount_cents=excluded.amount_cents,merchant_norm=excluded.merchant_norm,payee=excluded.payee,'
                 'pending=excluded.pending,currency=excluded.currency,split_needs_review=excluded.split_needs_review,'
                 'review_required=excluded.review_required',
                 (item_id, fact.transaction_id, fact.account_id, local_txn_id, active_owner, disposition,
                  fact.posted_date, fact.amount_cents, fact.merchant_norm, fact.payee, fact.pending, fact.currency,
                  split_needs_review, split_needs_review))


def adopt_transaction(conn: sqlite3.Connection, *, item_id: int,
                      provider_transaction_id: str, local_txn_id: int, fact: TxnFact) -> None:
    """Bind one fresh reviewed exact match. Caller holds Item/intake locks and immediate."""
    _require_transaction(conn)
    if provider_transaction_id != fact.transaction_id or fact.pending or fact.currency != 'USD':
        raise ValueError('stale_review')
    if item(conn, item_id)['state'] not in {'disabled', 'active'}:
        raise ValueError('item_disabled')
    account = _account(conn, item_id, fact.account_id)
    if account['currency'] != 'USD' or account['account_type'] is None or account['local_account_id'] is None:
        raise ValueError('unsupported_account')
    _assert_account_type(conn, account['local_account_id'], account['account_type'])
    if conn.execute('SELECT 1 FROM plaid_accounts WHERE local_account_id=? AND enabled=1 '
                    'AND NOT (item_id=? AND provider_account_id=?)',
                    (account['local_account_id'], item_id, fact.account_id)).fetchone():
        raise ValueError('active_source')
    local = conn.execute('SELECT * FROM transactions WHERE txn_id=?', (local_txn_id,)).fetchone()
    if (local is None or local['status'] != 'posted' or local['account_id'] != account['local_account_id']
            or any(local[key] != getattr(fact, key) for key in ('posted_date', 'amount_cents', 'merchant_norm'))):
        raise ValueError('stale_review')
    if conn.execute('SELECT 1 FROM plaid_transactions WHERE local_txn_id=? OR (item_id=? AND transaction_id=? AND local_txn_id IS NOT NULL)',
                    (local_txn_id, item_id, provider_transaction_id)).fetchone():
        raise ValueError('already_owned')
    save_fact(conn, item_id=item_id, fact=fact, local_txn_id=local_txn_id, active_owner=1, disposition='adopted')
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    run = conn.execute("INSERT INTO plaid_sync_runs(item_id,started_at,completed_at,outcome) VALUES(?,?,?,'adopted') RETURNING run_id", (item_id, stamp, stamp)).fetchone()[0]
    conn.execute("INSERT INTO plaid_changes(run_id,local_txn_id,event_kind,created_at) VALUES(?,?,'adopted',?)", (run, local_txn_id, stamp))


class ProviderOwnershipError(ValueError):
    """Safe source-protection code suitable for CLI/intake boundaries."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def is_provider_owned(conn: sqlite3.Connection, txn_id: int) -> bool:
    # Retired mappings still protect facts required by recovery.
    return conn.execute('SELECT 1 FROM plaid_transactions WHERE local_txn_id=? LIMIT 1',
                        (txn_id,)).fetchone() is not None


def assert_file_import_allowed(conn: sqlite3.Connection, rows: list[tuple[int, str]]) -> None:
    """Caller holds intake lock and immediate reservation for the complete file."""
    for account_id, posted_date in rows:
        if conn.execute('SELECT 1 FROM plaid_accounts WHERE local_account_id=? '
                        'AND enabled=1 AND start_date<=? LIMIT 1',
                        (account_id, posted_date)).fetchone():
            raise ProviderOwnershipError('source_overlap')
