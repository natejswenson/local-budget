"""Host-only Hosted Link receipts; credentials and bearer URLs stay private."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from local_budget import db
from . import store
from .client import PlaidClient
from .credentials import KeychainCredentials


class LinkError(Exception):
    """Safe local lifecycle failure without provider response details."""

    def __init__(self, code='link_failed', *, session_id=None):
        self.session_id = session_id
        self.code = code if code in {'link_failed', 'invalid_session', 'invalid_link',
                                    'recovery_required', 'browser_unavailable', 'item_busy'} else 'link_failed'
        super().__init__(self.code)


def _url(url):
    valid = False
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'secure.plaid.com'
                 and parsed.port in (None, 443)
                 and parsed.username is None and parsed.password is None
                 and not any(c.isspace() or ord(c) < 32 for c in url))
    except (TypeError, ValueError):
        pass
    if not valid:
        raise LinkError('invalid_link')
    return url


def open_session_url(url: str) -> None:
    """Use native Launch Services, never place bearer URLs in process argv."""
    _url(url)
    opened = False
    if sys.platform == 'darwin':
        try:
            from AppKit import NSWorkspace
            from Foundation import NSURL
            opened = NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception:
            pass
    if not opened:
        raise LinkError('browser_unavailable')


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 4096


def _expiry(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is not None:
            return result
    except (AttributeError, TypeError, ValueError):
        pass
    raise LinkError('invalid_link')


def _start(db_path: Path, client: PlaidClient, vault: KeychainCredentials, *, country: str, now: datetime,
          days_requested: int = 90, existing_item: int | None = None) -> str:
    if (country != 'US' or now.tzinfo is None or type(days_requested) is not int
            or not 1 <= days_requested <= 730
            or (client.environment == 'production' and days_requested < 30)):
        raise LinkError('invalid_link')
    with db.connect(db_path) as conn:
        body = {'client_name': 'Local Budget', 'language': 'en', 'country_codes': [country],
                'user': {'client_user_id': store.user_handle(conn)},
                'hosted_link': {'url_lifetime_seconds': 900, 'is_mobile_app': False}}
        if existing_item is None:
            body.update(products=['transactions'], transactions={'days_requested': days_requested})
        else:
            item = store.item(conn, existing_item)
            if item['environment'] != client.environment:
                raise LinkError('invalid_session')
            body['access_token'] = vault.get(item['credential_ref'], interactive=True)
    response = client.call('/link/token/create', body, deadline=time.monotonic() + 900)
    if not _text(response.get('link_token')):
        raise LinkError('invalid_link')
    url = _url(response.get('hosted_link_url'))
    expires = min(_expiry(response.get('expiration')), now + timedelta(minutes=15))
    if expires <= now:
        raise LinkError('invalid_link')
    session, credential = uuid4().hex, uuid4().hex
    persisted = False
    try:
        vault.put(credential, response['link_token'])
        with db.connect(db_path) as conn:
            store.add_link_session(conn, session_id=session, mode='link' if existing_item is None else 'update',
                                   item_id=existing_item, credential_ref=credential,
                                   expires_at=expires.isoformat(), environment=client.environment)
        persisted = True
    except Exception:
        pass
    if not persisted:
        _cleanup(vault, credential)
        raise LinkError()
    launched = True
    try:
        open_session_url(url)
    except LinkError:
        launched = False
    if not launched:
        raise LinkError('browser_unavailable', session_id=session)
    return session


def _cleanup(vault, handle):
    # A restart repeats idempotent cleanup if the host Keychain was unavailable.
    try:
        vault.delete(handle)
    except Exception:
        pass


def _state(path, session, state, *, completed=0, item_id=None):
    expected = 'exchanging' if state == 'recovery_required' else 'pending'
    with db.connect(path) as conn:
        return conn.execute('UPDATE plaid_link_sessions SET state=?,completed=?,item_id=COALESCE(?,item_id) '
                            'WHERE session_id=? AND state=? AND completed=0',
                            (state, completed, item_id, session, expected)).rowcount == 1


def _resume(db_path: Path, client: PlaidClient, vault: KeychainCredentials,
           session_id: str, *, now: datetime) -> int | None:
    with db.connect(db_path) as conn:
        row = conn.execute('SELECT * FROM plaid_link_sessions WHERE session_id=?', (session_id,)).fetchone()
        if row is None or row['environment'] != client.environment or now.tzinfo is None:
            raise LinkError('invalid_session')
        receipt = dict(row)
    if receipt['completed']:
        _cleanup(vault, receipt['credential_ref'])
        return receipt['item_id']
    if receipt['state'] in {'exchanging', 'recovery_required'}:
        raise LinkError('recovery_required')
    if receipt['state'] in {'cancelled', 'expired'}:
        _cleanup(vault, receipt['credential_ref'])
        return None
    remaining = (_expiry(receipt['expires_at']) - now).total_seconds()
    if remaining <= 0:
        if not _state(db_path, session_id, 'expired'):
            return _resume(db_path, client, vault, session_id, now=now)
        _cleanup(vault, receipt['credential_ref'])
        return None
    response = client.call('/link/token/get', {'link_token': vault.get(receipt['credential_ref'], interactive=True)},
                           deadline=time.monotonic() + remaining)
    sessions = response.get('link_sessions')
    if not isinstance(sessions, list) or len(sessions) > 1:
        raise LinkError('invalid_link')
    if not sessions:
        return None
    session = sessions[0]
    if not isinstance(session, dict):
        raise LinkError('invalid_link')
    if session.get('exit') is not None:
        if not _state(db_path, session_id, 'cancelled'):
            return _resume(db_path, client, vault, session_id, now=now)
        _cleanup(vault, receipt['credential_ref'])
        return None
    if not session.get('finished_at'):
        return None
    results = session.get('results')
    additions = results.get('item_add_results') if isinstance(results, dict) else None
    if not isinstance(additions, list) or len(additions) > 1:
        raise LinkError('invalid_link')
    if receipt['mode'] == 'update':
        if not _state(db_path, session_id, 'completed', completed=1):
            return _resume(db_path, client, vault, session_id, now=now)
        _cleanup(vault, receipt['credential_ref'])
        return receipt['item_id']
    if receipt['mode'] != 'link' or len(additions) != 1 or not isinstance(additions[0], dict):
        raise LinkError('invalid_link')
    public_token = additions[0].get('public_token')
    if not _text(public_token):
        raise LinkError('invalid_link')
    # Claim exchange durably before sending a non-idempotent request.
    with db.connect(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        claimed = conn.execute("UPDATE plaid_link_sessions SET state='exchanging' "
                               "WHERE session_id=? AND state='pending' AND completed=0", (session_id,)).rowcount
        if claimed != 1:
            raise LinkError('recovery_required')
    item_id = None
    saved = False
    try:
        exchanged = client.call('/item/public_token/exchange', {'public_token': public_token},
                                deadline=time.monotonic() + remaining)
        if not _text(exchanged.get('access_token')) or not _text(exchanged.get('item_id')):
            raise LinkError('invalid_link')
        # Stable recovery handle derived solely from the local receipt.
        access_ref = 'access-' + session_id
        vault.put(access_ref, exchanged['access_token'])
        with db.connect(db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            item_id = store.add_item(conn, environment=receipt['environment'],
                                     provider_item_id=exchanged['item_id'], credential_ref=access_ref)
            conn.execute("UPDATE plaid_link_sessions SET state='completed',completed=1,item_id=? "
                         'WHERE session_id=?', (item_id, session_id))
        saved = True
    except Exception:
        pass
    if not saved:
        try:
            _state(db_path, session_id, 'recovery_required')
        except Exception:
            pass  # Durable exchanging intent still prevents any blind retry.
        raise LinkError('recovery_required')
    _cleanup(vault, receipt['credential_ref'])
    return item_id


def start(db_path: Path, client: PlaidClient, vault: KeychainCredentials, *, country: str, now: datetime,
          days_requested: int = 90, existing_item: int | None = None) -> str:
    from .locks import item_locks
    with item_locks(db_path, [] if existing_item is None else [existing_item]) as acquired:
        if not acquired:
            raise LinkError('item_busy')
        return _start(db_path, client, vault, country=country, now=now,
                      days_requested=days_requested, existing_item=existing_item)


def resume(db_path: Path, client: PlaidClient, vault: KeychainCredentials,
           session_id: str, *, now: datetime) -> int | None:
    from .locks import item_locks
    with db.connect(db_path) as conn:
        row = conn.execute('SELECT mode,item_id FROM plaid_link_sessions WHERE session_id=?', (session_id,)).fetchone()
    ids = [row['item_id']] if row is not None and row['mode'] == 'update' else []
    with item_locks(db_path, ids) as acquired:
        if not acquired:
            raise LinkError('item_busy')
        return _resume(db_path, client, vault, session_id, now=now)
