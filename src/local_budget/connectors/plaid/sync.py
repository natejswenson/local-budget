"""Retrieve complete cursor batches without writing ledger facts or credentials."""
from __future__ import annotations

import math
import time

from .client import PlaidClient, PlaidError, _request_id, normalize_txn
from .types import SyncBatch

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
