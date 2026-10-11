import time
from decimal import Decimal

import pytest

from local_budget.connectors.plaid import sync
from local_budget.connectors.plaid.client import PlaidError
from local_budget.connectors.plaid.sync import check_fetch_limits, fetch_batch


def row(txn_id='txn', amount=1):
    return dict(transaction_id=txn_id, account_id='account', date='2026-10-01',
                amount=Decimal(amount), pending=False, iso_currency_code='USD', name='SHOP')


def page(**changes):
    return dict(added=[], modified=[], removed=[], has_more=False, next_cursor='final',
                transactions_update_status='HISTORICAL_UPDATE_COMPLETE', **{}) | changes


class Client:
    bytes_received = 0

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def call(self, endpoint, body, *, deadline=None):
        self.calls.append((endpoint, body, deadline))
        self.bytes_received += 100
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def fetch(client, cursor='original'):
    return fetch_batch(client, 'FABRICATED_ACCESS_TOKEN', cursor=cursor,
                       deadline=time.monotonic() + 300)


def test_restart_discards_events_and_intermediate_cursor():
    api = Client([page(added=[row('discarded')], has_more=True, next_cursor='partial'),
                  PlaidError('TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'), page()])
    batch = fetch(api)
    assert [call[1]['cursor'] for call in api.calls] == ['original', 'partial', 'original']
    assert batch.original_cursor == 'original' and batch.next_cursor == 'final'
    assert batch.events == () and api.bytes_received == 300 and api.updates_received == 1
    assert api.pagination_restarts == 1
    assert all(call[0] == '/transactions/sync' and call[1]['count'] == 500 for call in api.calls)


def test_ordered_observations_across_pages_and_none_cursor():
    api = Client([page(added=[row()], has_more=True, next_cursor='partial'),
                  page(modified=[row(amount=2)], removed=[{'transaction_id': 'other'}])])
    batch = fetch(api, None)
    assert 'cursor' not in api.calls[0][1]
    assert [(kind, fact if isinstance(fact, str) else fact.amount_cents)
            for kind, fact in batch.events] == [('added', -100), ('modified', -200), ('removed', 'other')]


@pytest.mark.parametrize('status,expected', [('NOT_READY', 'NOT_READY'),
    ('INITIAL_UPDATE_COMPLETE', 'INITIAL_UPDATE_COMPLETE'),
    ('HISTORICAL_UPDATE_COMPLETE', 'HISTORICAL_UPDATE_COMPLETE'),
    ('TRANSACTIONS_UPDATE_STATUS_UNKNOWN', 'unknown'), ('future', 'unknown'), (None, 'unknown')])
def test_empty_completed_cursor_and_safe_readiness(status, expected):
    batch = fetch(Client([page(next_cursor='', transactions_update_status=status)]), None)
    assert batch.next_cursor == '' and batch.readiness == expected


@pytest.mark.parametrize('changes', [dict(has_more=True, next_cursor='original'),
    dict(has_more=True, next_cursor=''), dict(has_more='yes'), dict(next_cursor=None),
    dict(added={}), dict(removed=[{}]), dict(removed=[{'transaction_id': 'bad\n'}])])
def test_malformed_or_nonadvancing_page_rejected(changes):
    with pytest.raises(PlaidError) as caught:
        fetch(Client([page(**changes)]))
    assert caught.value.code == 'INVALID_RESPONSE'


@pytest.mark.parametrize('key', ['added', 'modified', 'removed', 'has_more', 'next_cursor'])
def test_missing_required_key(key):
    response = page()
    del response[key]
    with pytest.raises(PlaidError):
        fetch(Client([response]))


def test_cursor_cycle_rejected():
    with pytest.raises(PlaidError):
        fetch(Client([page(has_more=True, next_cursor='a'),
                      page(has_more=True, next_cursor='b'), page(has_more=True, next_cursor='a')]))


@pytest.mark.parametrize('changes', [dict(added=[row()], modified=[row(amount=2)]),
    dict(added=[row()], removed=[{'transaction_id': 'txn'}]), dict(added=[row(), row()])])
def test_ambiguous_same_page_id_rejected(changes):
    with pytest.raises(PlaidError) as caught:
        fetch(Client([page(**changes)]))
    assert caught.value.code == 'INVALID_RESPONSE'


def test_completed_cursor_can_stay_same():
    assert fetch(Client([page(next_cursor='original')])).next_cursor == 'original'


@pytest.mark.parametrize('updates,byte_count,expired,code', [(50000, 64*1024*1024, False, None),
    (50001, 0, False, 'sync_limit'), (0, 64*1024*1024+1, False, 'sync_limit'),
    (0, 0, True, 'sync_deadline')])
def test_exact_limits(updates, byte_count, expired, code):
    if code:
        with pytest.raises(PlaidError) as caught:
            check_fetch_limits(updates=updates, response_bytes=byte_count, deadline=10,
                               now=10 if expired else 9)
        assert caught.value.code == code
    else:
        check_fetch_limits(updates=updates, response_bytes=byte_count, deadline=10, now=9)


def test_shared_update_budget_across_fetches_including_discarded_pages():
    api = Client([page(added=[row()], has_more=True, next_cursor='part'),
                  PlaidError('TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'), page(),
                  page(added=[row('later')])])
    api.updates_received = 49999
    fetch(api)
    with pytest.raises(PlaidError) as caught:
        fetch(api)
    assert caught.value.code == 'sync_limit' and api.updates_received == 50001


def test_shared_restart_budget_across_fetches():
    mutation = PlaidError('TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION')
    api = Client([mutation, mutation, mutation, page(), mutation])
    fetch(api)
    with pytest.raises(PlaidError) as caught:
        fetch(api)
    assert caught.value.code == mutation.code and api.pagination_restarts == 3


def test_expiry_prevents_request_and_checks_after_page(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(sync, '_monotonic', lambda: now[0])
    api = Client([page()])
    with pytest.raises(PlaidError):
        fetch_batch(api, 'token', cursor=None, deadline=0)
    assert not api.calls
    original = api.call
    def call(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 10
        return result
    api.call = call
    with pytest.raises(PlaidError) as caught:
        fetch_batch(api, 'token', cursor=None, deadline=10)
    assert caught.value.code == 'sync_deadline'


def test_byte_budget_after_page():
    api = Client([page()])
    api.bytes_received = 64*1024*1024 - 99
    with pytest.raises(PlaidError) as caught:
        fetch(api)
    assert caught.value.code == 'sync_limit'


@pytest.mark.parametrize('request_id', ['FABRICATED_ACCESS_TOKEN', 'x'*65, 'bad\n', 'é', 2])
def test_fake_client_metadata_cannot_bypass_sanitization(request_id):
    assert fetch(Client([page(request_id=request_id)])).request_id is None


def test_safe_request_id_and_provider_error_preserved():
    assert fetch(Client([page(request_id='request-1')])).request_id == 'request-1'
    with pytest.raises(PlaidError) as caught:
        fetch(Client([PlaidError('INVALID_FIELD')]))
    assert caught.value.code == 'INVALID_FIELD'


def test_second_fetch_cannot_extend_operation_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(sync, '_monotonic', lambda: now[0])
    api = Client([page()])
    fetch_batch(api, 'token', cursor=None, deadline=10)
    now[0] = 10
    with pytest.raises(PlaidError) as caught:
        fetch_batch(api, 'token', cursor='saved', deadline=1000)
    assert caught.value.code == 'sync_deadline' and len(api.calls) == 1


def test_discarded_page_bytes_still_enforce_limit():
    api = Client([page(has_more=True, next_cursor='part'),
                  PlaidError('TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION'), page()])
    api.bytes_received = 64*1024*1024 - 250
    with pytest.raises(PlaidError) as caught:
        fetch(api)
    assert caught.value.code == 'sync_limit' and len(api.calls) == 3


def test_real_client_retries_share_budget_bytes_and_fetch_counters(monkeypatch):
    import httpx

    from local_budget.connectors.plaid import client as client_module
    from local_budget.connectors.plaid.client import PlaidClient

    monkeypatch.setattr(client_module, '_sleep', lambda seconds: None)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) in (1, 3, 4, 5):
            return httpx.Response(503, content=b'{}')
        return httpx.Response(200, json=page(added=[row(amount=1) | {'amount': 1}]))
    with PlaidClient('client', 'secret', 'sandbox', transport=httpx.MockTransport(handler),
                     jitter=lambda: 0.25) as api:
        fetch(api, None)
        first_bytes = api.bytes_received
        with pytest.raises(PlaidError):
            fetch(api)
        assert len(calls) == 5 and api.bytes_received == first_bytes + 6
        assert api.updates_received == 1
