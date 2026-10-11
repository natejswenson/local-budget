import json
from decimal import Decimal

import httpx
import pytest

from local_budget.connectors.plaid import client as module
from local_budget.connectors.plaid.client import PlaidClient, PlaidError, normalize_txn


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(module, '_sleep', lambda seconds: None)


def client(handler, environment='sandbox'):
    return PlaidClient('CLIENT_SENTINEL', 'SECRET_SENTINEL', environment,
                       transport=httpx.MockTransport(handler))


def test_fixed_host_credentials_decimal_and_timeout():
    def handler(request):
        assert str(request.url) == 'https://sandbox.plaid.com/transactions/sync'
        assert json.loads(request.content) == {'client_id': 'CLIENT_SENTINEL',
                                              'secret': 'SECRET_SENTINEL', 'count': 500}
        assert request.extensions['timeout'] == dict(connect=10, pool=10, write=10, read=30)
        return httpx.Response(200, content=b'{"amount":0.29}')
    with client(handler) as api:
        payload = api.call('/transactions/sync', {'count': 500})
        assert payload['amount'] == Decimal('0.29')
        assert normalize_txn(dict(transaction_id='t', account_id='a', date='2026-10-01',
                                  pending=False, iso_currency_code='USD', name='x',
                                  amount=payload['amount'])).amount_cents == -29
        assert api.bytes_received == len(b'{"amount":0.29}')
        assert api.environment == 'sandbox'
        assert 'SENTINEL' not in repr(api)


@pytest.mark.parametrize('endpoint', ['https://evil.test/accounts/get', '//evil.test',
                                     '/accounts/get?x=1', '/accounts/get/../item/get',
                                     '/unknown'])
def test_endpoint_rejected_before_transport(endpoint):
    def handler(request):
        pytest.fail('invalid destination reached transport')
    with client(handler) as api, pytest.raises(PlaidError):
        api.call(endpoint, {})


def test_invalid_environment_is_safe():
    with pytest.raises(PlaidError) as caught:
        client(lambda request: httpx.Response(200), 'SECRET_SENTINEL')
    assert 'SENTINEL' not in repr(caught.value)


@pytest.mark.parametrize('status,body', [
    (400, b'{"error_code":"SECRET_SENTINEL","error_message":"SECRET_SENTINEL", "request_id":"bad secret"}'),
    (200, b'SECRET_SENTINEL'), (200, b'[]'), (200, b'{"amount":NaN}'),
    (302, b'SECRET_SENTINEL'),
])
def test_safe_untrusted_responses(status, body):
    with client(lambda request: httpx.Response(status, content=body,
                headers={'location': 'https://evil.test'})) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/accounts/get', {})
    error = caught.value
    assert 'SENTINEL' not in repr(error) + str(error) + repr(vars(error))
    assert error.__cause__ is None and error.__context__ is None


def test_allowed_error_metadata():
    with client(lambda request: httpx.Response(400, json={
            'error_code': 'ITEM_LOGIN_REQUIRED', 'request_id': 'request-1'})) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/accounts/get', {})
    assert caught.value.code == 'ITEM_LOGIN_REQUIRED'
    assert caught.value.request_id == 'request-1'
    assert caught.value.http_status == 400


def test_shared_retry_budget_and_cumulative_bytes():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) in (1, 3, 4, 5, 6) else 200, content=b'{}')
    with client(handler) as api:
        api.call('/accounts/get', {})
        with pytest.raises(PlaidError):
            api.call('/transactions/sync', {})
        assert len(calls) == 5
        assert api.bytes_received == 10
        with pytest.raises(PlaidError):
            api.call('/item/get', {})
        assert len(calls) == 6  # exhausted budget does not prevent initial request


@pytest.mark.parametrize('endpoint', ['/item/public_token/exchange', '/item/access_token/invalidate',
                                     '/item/remove', '/link/token/create'])
def test_ambiguous_mutations_are_not_retried(endpoint):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('SECRET_SENTINEL', request=request)
    with client(handler) as api, pytest.raises(PlaidError) as caught:
        api.call(endpoint, {})
    assert len(calls) == 1
    assert caught.value.code == 'TRANSPORT_ERROR'
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert 'SENTINEL' not in str(caught.value)


def test_deadline_caps_timeouts_and_checks_stream(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{'
            now[0] = 13.0
            yield b'}'
    def handler(request):
        assert request.extensions['timeout'] == dict(connect=2, pool=2, write=2, read=2)
        return httpx.Response(200, stream=Stream())
    with client(handler) as api, pytest.raises(PlaidError) as caught:
        api.call('/accounts/get', {}, deadline=12.0)
    assert caught.value.code == 'DEADLINE_EXCEEDED'


def test_expired_deadline_makes_no_request(monkeypatch):
    monkeypatch.setattr(module, '_monotonic', lambda: 10.0)
    with client(lambda request: pytest.fail('expired request')) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/accounts/get', {}, deadline=9.0)
    assert caught.value.code == 'DEADLINE_EXCEEDED'


def test_retry_after_is_bounded(monkeypatch):
    sleeps = []
    monkeypatch.setattr(module, '_sleep', sleeps.append)
    with client(lambda request: httpx.Response(429, content=b'{}',
                                              headers={'Retry-After': '9999999'})) as api:
        with pytest.raises(PlaidError):
            api.call('/accounts/get', {})
    assert len(sleeps) == 3 and all(0 <= value <= 30 for value in sleeps)


def test_stream_limit_before_json_decode(monkeypatch):
    monkeypatch.setattr(module, 'MAX_RESPONSE_BYTES', 5)
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'1234'
            yield b'5678'
            pytest.fail('read past byte limit')
    with client(lambda request: httpx.Response(200, stream=Stream())) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/accounts/get', {})
        assert api.bytes_received == 8
    assert caught.value.code == 'RESPONSE_TOO_LARGE'


def test_production_host_and_credentials_cannot_be_overridden():
    def handler(request):
        assert str(request.url) == 'https://production.plaid.com/item/get'
        assert json.loads(request.content)['secret'] == 'SECRET_SENTINEL'
        return httpx.Response(200, json={})
    with client(handler, 'production') as api:
        assert api.call('/item/get', {'secret': 'override'}) == {}


def test_operation_deadline_is_shared_between_calls(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})
    with client(handler) as api:
        api.call('/accounts/get', {})
        now[0] = 301.0
        with pytest.raises(PlaidError) as caught:
            api.call('/item/get', {})
    assert len(calls) == 1 and caught.value.code == 'DEADLINE_EXCEEDED'


def test_transport_read_failures_are_bounded_and_safe():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError('SECRET_SENTINEL', request=request)
    with client(handler) as api, pytest.raises(PlaidError) as caught:
        api.call('/accounts/get', {})
    assert len(calls) == 4
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_client_disables_environment_proxies_and_enforces_tls(monkeypatch):
    original = httpx.Client
    observed = {}
    def build(**options):
        observed.update(options)
        return original(**options)
    monkeypatch.setattr(module.httpx, 'Client', build)
    with client(lambda request: httpx.Response(200, json={})) as api:
        assert api.call('/item/get', {}) == {}
    assert observed['verify'] is True
    assert observed['trust_env'] is False
    assert observed['follow_redirects'] is False


def test_decoding_failure_does_not_retain_response_exception():
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'first'
            raise httpx.DecodingError('SECRET_SENTINEL')
    with client(lambda request: httpx.Response(200, stream=Stream())) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/item/public_token/exchange', {})
        assert api.bytes_received == 5
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert 'SENTINEL' not in str(caught.value)


def test_exception_constructor_only_retains_safe_codes_and_metadata():
    error = PlaidError('SECRET_SENTINEL', 'SECRET SENTINEL', 'SECRET_SENTINEL')
    assert error.code == 'PROVIDER_ERROR'
    assert error.request_id is None and error.http_status is None
    assert 'SENTINEL' not in str(error) + repr(error) + repr(vars(error))
    assert PlaidError('sync_deadline').code == 'sync_deadline'
    assert PlaidError('sync_limit').code == 'sync_limit'


def test_version_header_and_request_id_matching_credentials_are_discarded():
    def handler(request):
        assert request.headers['Plaid-Version'] == '2020-09-14'
        return httpx.Response(400, json={'error_code': 'INVALID_FIELD',
                                         'request_id': 'SECRET_SENTINEL'})
    with client(handler) as api, pytest.raises(PlaidError) as caught:
        api.call('/accounts/get', {})
    assert caught.value.code == 'INVALID_FIELD'
    assert caught.value.request_id is None


def test_deadline_checked_after_json_conversion(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    original = module.json.loads
    def decode(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 2.0
        return result
    monkeypatch.setattr(module.json, 'loads', decode)
    with client(lambda request: httpx.Response(200, content=b'{}')) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/accounts/get', {}, deadline=1.0)
    assert caught.value.code == 'DEADLINE_EXCEEDED'


def test_explicit_link_deadline_keeps_same_client_alive_after_five_minutes(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) in (1, 4, 5, 6) else 200, content=b'{}')
    with PlaidClient('client', 'secret', 'sandbox', deadline=910.0,
                     transport=httpx.MockTransport(handler)) as api:
        api.call('/link/token/get', {})
        now[0] = 311.0
        assert api.call('/link/token/get', {}) == {}
        assert api.bytes_received == 6
        with pytest.raises(PlaidError):
            api.call('/link/token/get', {})
        assert len(calls) == 6 and api.bytes_received == 12
        now[0] = 911.0
        with pytest.raises(PlaidError) as caught:
            api.call('/link/token/get', {})
    assert caught.value.code == 'DEADLINE_EXCEEDED' and len(calls) == 6


def test_call_deadline_only_shortens_explicit_operation_deadline(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b'{}')
    with PlaidClient('client', 'secret', 'sandbox', deadline=910.0,
                     transport=httpx.MockTransport(handler)) as api:
        now[0] = 400.0
        with pytest.raises(PlaidError):
            api.call('/link/token/get', {}, deadline=399.0)
        assert api.call('/link/token/get', {}, deadline=600.0) == {}
        now[0] = 911.0
        with pytest.raises(PlaidError):
            api.call('/link/token/get', {}, deadline=1200.0)
    assert len(calls) == 1


@pytest.mark.parametrize('bound', [float('nan'), float('inf'), float('-inf'),
                                 'SECRET_SENTINEL', True, 9.0, 10.0])
def test_invalid_constructor_deadline_is_safe(monkeypatch, bound):
    monkeypatch.setattr(module, '_monotonic', lambda: 10.0)
    with pytest.raises(PlaidError) as caught:
        PlaidClient('client', 'secret', 'sandbox', deadline=bound,
                    transport=httpx.MockTransport(lambda request: pytest.fail('invalid request')))
    assert caught.value.code == 'INVALID_REQUEST'
    assert 'SENTINEL' not in repr(caught.value)


def test_explicit_operation_deadline_is_capped_to_fifteen_minutes(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    with PlaidClient('client', 'secret', 'sandbox', deadline=10000.0,
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'{}'))) as api:
        now[0] = 909.0
        assert api.call('/link/token/get', {}) == {}
        now[0] = 910.0
        with pytest.raises(PlaidError) as caught:
            api.call('/link/token/get', {})
    assert caught.value.code == 'DEADLINE_EXCEEDED'


@pytest.mark.parametrize('request_id', ['SECRET_SENTINEL', 'CLIENT_SENTINEL',
    'FABRICATED_ACCESS_TOKEN', 'x'*65, 'bad\n', 'é'])
def test_success_request_id_is_sanitized(request_id):
    with client(lambda request: httpx.Response(200, json={'request_id': request_id})) as api:
        assert api.call('/transactions/sync', {'access_token': 'FABRICATED_ACCESS_TOKEN'})['request_id'] is None


def test_injected_retry_jitter_and_deadline_without_waiting(monkeypatch):
    now = [0.0]
    sleeps = []
    calls = []
    monkeypatch.setattr(module, '_monotonic', lambda: now[0])
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    monkeypatch.setattr(module, '_sleep', sleep)
    def handler(request):
        calls.append(request)
        return httpx.Response(503, content=b'{}')
    with PlaidClient('client', 'secret', 'sandbox', transport=httpx.MockTransport(handler),
                     jitter=lambda: 0.25) as api:
        with pytest.raises(PlaidError) as caught:
            api.call('/transactions/sync', {}, deadline=2.0)
    assert sleeps == [1.25] and len(calls) == 2
    assert caught.value.code == 'DEADLINE_EXCEEDED' and now[0] < 2.0
