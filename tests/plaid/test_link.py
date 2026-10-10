from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from local_budget import db
from local_budget.connectors.plaid import client, credentials, store

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
URL = 'https://secure.plaid.com/link/FABRICATED_BEARER'


class Vault:
    def __init__(self):
        self.values = {}
        self.fail_access = False

    def put(self, handle, secret):
        if self.fail_access and secret == 'FABRICATED_ACCESS':
            raise credentials.CredentialError('credential_locked')
        self.values[handle] = secret

    def get(self, handle, *, interactive=False):
        return self.values[handle]

    def delete(self, handle):
        self.values.pop(handle, None)


@pytest.fixture
def flow(plaid_db, monkeypatch):
    from local_budget.connectors.plaid import link
    vault = Vault()
    requests = []
    replies = {
        '/link/token/create': {'link_token': 'FABRICATED_LINK', 'hosted_link_url': URL,
                               'expiration': (NOW + timedelta(minutes=15)).isoformat()},
        '/link/token/get': {'link_sessions': [{'finished_at': NOW.isoformat(), 'exit': None,
                            'results': {'item_add_results': [{'public_token': 'FABRICATED_PUBLIC'}]}}]},
        '/item/public_token/exchange': {'access_token': 'FABRICATED_ACCESS', 'item_id': 'FABRICATED_ITEM'},
    }
    def transport(request):
        requests.append((request.url.path, json.loads(request.content)))
        result = replies[request.url.path]
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, json=result)
    opened = []
    monkeypatch.setattr(link, 'open_session_url', opened.append)
    with client.PlaidClient('FABRICATED_CLIENT', 'FABRICATED_SECRET', 'sandbox',
                            transport=httpx.MockTransport(transport)) as api:
        yield link, vault, api, requests, replies, opened


def begin(flow, path, **kwargs):
    link, vault, api, *_ = flow
    return link.start(path, api, vault, country='US', now=NOW, **kwargs)


def receipt(path, session):
    with db.connect(path) as conn:
        return dict(conn.execute('SELECT * FROM plaid_link_sessions WHERE session_id=?', (session,)).fetchone())


def test_start_persists_only_handles_before_browser(flow, plaid_db):
    link, vault, api, requests, _, opened = flow
    session = begin(flow, plaid_db)
    row = receipt(plaid_db, session)
    assert row['state'] == 'pending' and row['environment'] == 'sandbox'
    assert vault.get(row['credential_ref']) == 'FABRICATED_LINK'
    assert opened == [URL]
    body = requests[0][1]
    assert body['products'] == ['transactions']
    assert body['transactions'] == {'days_requested': 90}
    assert body['hosted_link'] == {'url_lifetime_seconds': 900, 'is_mobile_app': False}
    with db.connect(plaid_db) as conn:
        assert body['user']['client_user_id'] == store.user_handle(conn)
        dump = '\n'.join(conn.iterdump())
    assert all(secret not in dump for secret in [URL, 'FABRICATED_LINK', 'FABRICATED_SECRET'])


def test_restart_completion_is_idempotent_and_disabled(flow, plaid_db):
    link, vault, api, requests, _, _ = flow
    session = begin(flow, plaid_db)
    item = link.resume(plaid_db, api, vault, session, now=NOW)
    assert link.resume(plaid_db, api, vault, session, now=NOW) == item
    assert [endpoint for endpoint, _ in requests].count('/item/public_token/exchange') == 1
    with db.connect(plaid_db) as conn:
        row = store.item(conn, item)
    assert row['state'] == 'disabled'
    assert vault.get(row['credential_ref']) == 'FABRICATED_ACCESS'


@pytest.mark.parametrize('kind', ['cancel', 'expired', 'multiple', 'multiple_sessions', 'uncertain', 'vault'])
def test_failure_does_not_create_item_or_retry_exchange(flow, plaid_db, kind):
    link, vault, api, requests, replies, _ = flow
    session = begin(flow, plaid_db)
    now = NOW
    if kind == 'cancel':
        replies['/link/token/get']['link_sessions'][0]['exit'] = {'error': None}
        replies['/link/token/get']['link_sessions'][0]['results'] = {'item_add_results': []}
    if kind == 'expired':
        now += timedelta(minutes=16)
    if kind == 'multiple':
        replies['/link/token/get']['link_sessions'][0]['results']['item_add_results'] *= 2
    if kind == 'multiple_sessions':
        replies['/link/token/get']['link_sessions'] *= 2
    if kind == 'uncertain':
        replies['/item/public_token/exchange'] = httpx.ReadTimeout('FABRICATED_SECRET')
    if kind == 'vault':
        vault.fail_access = True
    if kind in {'cancel', 'expired'}:
        assert link.resume(plaid_db, api, vault, session, now=now) is None
    else:
        with pytest.raises(link.LinkError):
            link.resume(plaid_db, api, vault, session, now=now)
    if kind in {'uncertain', 'vault'}:
        assert receipt(plaid_db, session)['state'] == 'recovery_required'
        with pytest.raises(link.LinkError):
            link.resume(plaid_db, api, vault, session, now=now)
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plaid_items').fetchone()[0] == 0
    assert [endpoint for endpoint, _ in requests].count('/item/public_token/exchange') <= 1


def test_wrong_environment_and_unknown_session_do_not_contact_provider(flow, plaid_db):
    link, vault, api, requests, *_ = flow
    session = begin(flow, plaid_db)
    api.environment = 'production'
    for handle in [session, 'missing']:
        with pytest.raises(link.LinkError):
            link.resume(plaid_db, api, vault, handle, now=NOW)
    assert len(requests) == 1


@pytest.mark.parametrize('url', ['http://secure.plaid.com/x', 'https://evil.test/x',
                                'https://user@secure.plaid.com/x', 'https://secure.plaid.com:444/x'])
def test_untrusted_url_never_opens(flow, plaid_db, url):
    link, _, _, _, replies, opened = flow
    replies['/link/token/create']['hosted_link_url'] = url
    with pytest.raises(link.LinkError):
        begin(flow, plaid_db)
    assert opened == []


@pytest.mark.parametrize('days', [0, 731])
def test_history_bounds_reject_before_provider(flow, plaid_db, days):
    link, _, _, requests, *_ = flow
    with pytest.raises(link.LinkError):
        begin(flow, plaid_db, days_requested=days)
    assert requests == []


def test_update_mode_retains_reconnect_state_and_never_exchanges(flow, plaid_db, seed_item):
    link, vault, api, requests, replies, _ = flow
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE plaid_items SET state='reconnect_required' WHERE item_id=?", (item,))
    vault.values['test-ref-test-item'] = 'FABRICATED_OLD_ACCESS'
    session = begin(flow, plaid_db, existing_item=item)
    create = requests[0][1]
    assert create['access_token'] == 'FABRICATED_OLD_ACCESS'
    assert 'products' not in create and 'transactions' not in create
    assert link.resume(plaid_db, api, vault, session, now=NOW) == item
    assert len(requests) == 2
    with db.connect(plaid_db) as conn:
        row = store.item(conn, item)
    assert row['state'] == 'reconnect_required'
    assert row['credential_ref'] == 'test-ref-test-item'


def test_update_cancellation_does_not_claim_completion(flow, plaid_db, seed_item):
    link, vault, api, _, replies, _ = flow
    item = seed_item()
    vault.values['test-ref-test-item'] = 'FABRICATED_OLD_ACCESS'
    session = begin(flow, plaid_db, existing_item=item)
    replies['/link/token/get']['link_sessions'][0]['exit'] = {'error': None}
    assert link.resume(plaid_db, api, vault, session, now=NOW) is None
    assert receipt(plaid_db, session)['state'] == 'cancelled'


@pytest.mark.parametrize('kind', ['completed', 'cancelled', 'expired'])
def test_terminal_receipts_clean_link_credentials_on_restart(flow, plaid_db, kind):
    link, vault, api, _, replies, _ = flow
    session = begin(flow, plaid_db)
    handle = receipt(plaid_db, session)['credential_ref']
    if kind == 'cancelled':
        replies['/link/token/get']['link_sessions'][0]['exit'] = {}
    now = NOW + timedelta(minutes=16) if kind == 'expired' else NOW
    link.resume(plaid_db, api, vault, session, now=now)
    assert handle not in vault.values
    vault.values[handle] = 'FABRICATED_LINK'
    link.resume(plaid_db, api, vault, session, now=now)
    assert handle not in vault.values


def test_browser_failure_returns_discoverable_receipt_handle(flow, plaid_db, monkeypatch):
    link, vault, *_ = flow
    def unavailable(url):
        raise link.LinkError('browser_unavailable')
    monkeypatch.setattr(link, 'open_session_url', unavailable)
    with pytest.raises(link.LinkError) as failure:
        begin(flow, plaid_db)
    assert receipt(plaid_db, failure.value.session_id)['state'] == 'pending'
    assert URL not in str(failure.value)


def test_concurrent_terminal_update_cannot_overwrite_exchange(flow, plaid_db):
    link, *_ = flow
    session = begin(flow, plaid_db)
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE plaid_link_sessions SET state='exchanging' WHERE session_id=?", (session,))
    link._state(plaid_db, session, 'cancelled')
    assert receipt(plaid_db, session)['state'] == 'exchanging'


def test_receipt_failure_cleans_unreferenced_link_credential(flow, plaid_db, monkeypatch):
    link, vault, _, _, _, opened = flow
    def failed(*args, **kwargs):
        raise RuntimeError('FABRICATED_SECRET')
    monkeypatch.setattr(store, 'add_link_session', failed)
    with pytest.raises(link.LinkError) as error:
        begin(flow, plaid_db)
    assert vault.values == {} and opened == []
    assert error.value.__context__ is None


def test_access_write_follows_durable_exchange_intent(flow, plaid_db):
    link, vault, api, *_ = flow
    session = begin(flow, plaid_db)
    original = vault.put
    def checked(handle, secret):
        assert receipt(plaid_db, session)['state'] == 'exchanging'
        original(handle, secret)
    vault.put = checked
    assert link.resume(plaid_db, api, vault, session, now=NOW) is not None


def test_restart_after_exchange_intent_never_calls_provider(flow, plaid_db):
    link, vault, api, requests, *_ = flow
    session = begin(flow, plaid_db)
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE plaid_link_sessions SET state='exchanging' WHERE session_id=?", (session,))
    with pytest.raises(link.LinkError, match='recovery_required'):
        link.resume(plaid_db, api, vault, session, now=NOW)
    assert len(requests) == 1


@pytest.mark.parametrize('value', [None, '', 'malformed', '2026-10-10T11:00:00+00:00'])
def test_invalid_expiration_creates_no_receipt(flow, plaid_db, value):
    link, vault, _, _, replies, opened = flow
    replies['/link/token/create']['expiration'] = value
    with pytest.raises(link.LinkError):
        begin(flow, plaid_db)
    assert vault.values == {} and opened == []


def test_provider_long_expiration_cannot_extend_local_receipt(flow, plaid_db):
    _, _, _, _, replies, _ = flow
    replies['/link/token/create']['expiration'] = (NOW + timedelta(hours=6)).isoformat()
    session = begin(flow, plaid_db)
    assert receipt(plaid_db, session)['expires_at'] == '2026-10-10T12:15:00+00:00'


def test_duplicate_provider_item_requires_recovery(flow, plaid_db, seed_item):
    link, vault, api, *_ = flow
    seed_item('FABRICATED_ITEM')
    session = begin(flow, plaid_db)
    with pytest.raises(link.LinkError, match='recovery_required'):
        link.resume(plaid_db, api, vault, session, now=NOW)
    assert receipt(plaid_db, session)['state'] == 'recovery_required'
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plaid_items').fetchone()[0] == 1
    assert vault.get('access-' + session) == 'FABRICATED_ACCESS'


def test_failed_completion_transaction_never_returns_uncommitted_item(flow, plaid_db):
    link, vault, api, *_ = flow
    session = begin(flow, plaid_db)
    with db.connect(plaid_db) as conn:
        conn.execute("CREATE TRIGGER reject_completion BEFORE UPDATE ON plaid_link_sessions "
                     "WHEN NEW.completed=1 BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(link.LinkError, match='recovery_required'):
        link.resume(plaid_db, api, vault, session, now=NOW)
    assert receipt(plaid_db, session)['state'] == 'recovery_required'
    assert vault.get('access-' + session) == 'FABRICATED_ACCESS'
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plaid_items').fetchone()[0] == 0


def test_recovery_persistence_failure_drops_raw_secret_context(flow, plaid_db):
    link, vault, api, *_ = flow
    session = begin(flow, plaid_db)
    def failed_write(handle, secret):
        raise RuntimeError('FABRICATED_ACCESS')
    vault.put = failed_write
    with db.connect(plaid_db) as conn:
        conn.execute("CREATE TRIGGER reject_recovery BEFORE UPDATE ON plaid_link_sessions "
                     "WHEN NEW.state='recovery_required' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(link.LinkError) as failure:
        link.resume(plaid_db, api, vault, session, now=NOW)
    assert failure.value.__context__ is None
    assert receipt(plaid_db, session)['state'] == 'exchanging'


def test_empty_url_userinfo_is_rejected(flow, plaid_db):
    link, _, _, _, replies, opened = flow
    replies['/link/token/create']['hosted_link_url'] = 'https://@secure.plaid.com/x'
    with pytest.raises(link.LinkError):
        begin(flow, plaid_db)
    assert opened == []
