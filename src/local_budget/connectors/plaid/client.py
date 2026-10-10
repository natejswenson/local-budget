"""Restricted, operation-scoped Plaid transport and internal fact conversion."""
from __future__ import annotations

import json
import re
import time
from datetime import date
from decimal import Decimal

import httpx

from local_budget import money, sanitize

from .types import TxnFact

HOSTS = {'sandbox': 'https://sandbox.plaid.com',
         'production': 'https://production.plaid.com'}
ENDPOINTS = frozenset({'/link/token/create', '/link/token/get',
    '/item/public_token/exchange', '/accounts/get', '/item/get',
    '/transactions/sync', '/item/remove', '/item/access_token/invalidate'})
_READS = frozenset({'/link/token/get', '/accounts/get', '/item/get', '/transactions/sync'})
_ERROR_CODES = frozenset({'ITEM_LOGIN_REQUIRED', 'ITEM_LOCKED', 'ITEM_NOT_SUPPORTED',
    'ITEM_NO_ERROR', 'INVALID_ACCESS_TOKEN', 'INVALID_PUBLIC_TOKEN', 'INVALID_LINK_TOKEN',
    'INVALID_CREDENTIALS', 'INSTITUTION_DOWN', 'INSTITUTION_NOT_RESPONDING',
    'INTERNAL_SERVER_ERROR', 'RATE_LIMIT_EXCEEDED', 'PRODUCT_NOT_READY',
    'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION', 'NO_ACCOUNTS',
    'ACCESS_NOT_GRANTED', 'USER_PERMISSION_REVOKED', 'INVALID_FIELD'})
_LOCAL_CODES = frozenset({'INVALID_ENVIRONMENT', 'INVALID_ENDPOINT', 'INVALID_REQUEST',
    'DEADLINE_EXCEEDED', 'RESPONSE_TOO_LARGE', 'INVALID_RESPONSE', 'PROVIDER_ERROR',
    'TRANSPORT_ERROR', 'INVALID_TRANSACTION', 'sync_deadline', 'sync_limit'})
_REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_monotonic = time.monotonic
_sleep = time.sleep


class PlaidError(Exception):
    """Only an allowlisted code and bounded diagnostic metadata leave transport."""

    def __init__(self, code: str, request_id: str | None = None,
                 http_status: int | None = None):
        self.code = (code if isinstance(code, str) and code in _ERROR_CODES | _LOCAL_CODES
                     else 'PROVIDER_ERROR')
        self.request_id = (request_id if isinstance(request_id, str)
                           and _REQUEST_ID.fullmatch(request_id) else None)
        self.http_status = (http_status if type(http_status) is int
                            and 100 <= http_status <= 599 else None)
        super().__init__(self.code)


def _request_id(payload: dict, body: dict) -> str | None:
    value = payload.get('request_id')
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        return None
    sensitive = ('secret', 'client_id', 'access_token', 'public_token', 'link_token')
    if any(isinstance(body.get(key), str) and body[key] and body[key] in value
           for key in sensitive):
        return None
    return value


def _reject_constant(value: str):
    raise ValueError('invalid JSON number')


class PlaidClient:
    """One lifetime shares retries, bytes and a five-minute operation deadline."""

    def __init__(self, client_id: str, secret: str, environment: str, *, transport=None):
        if environment not in HOSTS:
            raise PlaidError('INVALID_ENVIRONMENT')
        self.environment = environment
        self._credentials = {'client_id': client_id, 'secret': secret}
        self.bytes_received = 0
        self._retries_remaining = 3
        self._deadline = _monotonic() + 300
        self._http = httpx.Client(transport=transport, verify=True, trust_env=False,
                                  follow_redirects=False, headers={'Plaid-Version': '2020-09-14'},
                                  timeout=httpx.Timeout(30, connect=10, pool=10, write=10))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._http.close()
        self._credentials.clear()

    def call(self, endpoint: str, body: dict, *, deadline: float | None = None) -> dict:
        if endpoint not in ENDPOINTS:
            raise PlaidError('INVALID_ENDPOINT')
        if not isinstance(body, dict):
            raise PlaidError('INVALID_REQUEST')
        end = min(self._deadline, deadline) if deadline is not None else self._deadline
        # Credentials always come from this client, never caller-supplied overrides.
        request_body = body | self._credentials
        while True:
            remaining = end - _monotonic()
            if remaining <= 0:
                raise PlaidError('DEADLINE_EXCEEDED')
            result, retryable, retry_after = self._attempt(endpoint, request_body, end, remaining)
            if isinstance(result, dict):
                return result
            if endpoint not in _READS or not retryable or not self._retries_remaining:
                raise result
            self._retries_remaining -= 1
            remaining = end - _monotonic()
            if remaining <= retry_after:
                raise PlaidError('DEADLINE_EXCEEDED')
            _sleep(retry_after)

    def _attempt(self, endpoint: str, body: dict, end: float, remaining: float):
        """Return sanitized failures; never raise within a secret-bearing except block."""
        timeout = httpx.Timeout(min(30, remaining), connect=min(10, remaining),
                                pool=min(10, remaining), write=min(10, remaining))
        try:
            with self._http.stream('POST', HOSTS[self.environment] + endpoint,
                                   json=body, timeout=timeout) as response:
                parts = []
                for chunk in response.iter_bytes():
                    self.bytes_received += len(chunk)
                    if self.bytes_received > MAX_RESPONSE_BYTES:
                        return PlaidError('RESPONSE_TOO_LARGE'), False, 0
                    if _monotonic() >= end:
                        return PlaidError('DEADLINE_EXCEEDED'), False, 0
                    parts.append(chunk)
                if _monotonic() >= end:
                    return PlaidError('DEADLINE_EXCEEDED'), False, 0
                raw = b''.join(parts)
                status = response.status_code
                transient = status in {429, 500, 502, 503, 504}
                delay = 1.0
                retry_header = response.headers.get('retry-after', '')
                if re.fullmatch(r'\d{1,10}', retry_header):
                    delay = min(30.0, float(retry_header))
                payload = None
                try:
                    payload = json.loads(raw, parse_float=Decimal,
                                         parse_constant=_reject_constant)
                except (ValueError, UnicodeError, RecursionError):
                    pass
                if _monotonic() >= end:
                    return PlaidError('DEADLINE_EXCEEDED'), False, 0
                if not isinstance(payload, dict):
                    return PlaidError('INVALID_RESPONSE', http_status=status), transient, delay
                if status < 200 or status >= 300:
                    code = payload.get('error_code')
                    if not isinstance(code, str) or code not in _ERROR_CODES:
                        code = 'PROVIDER_ERROR'
                    return PlaidError(code, _request_id(payload, body), status), transient, delay
                return payload, False, 0
        except httpx.RequestError:
            return PlaidError('TRANSPORT_ERROR'), True, 1.0
        except (TypeError, ValueError, RuntimeError):
            return PlaidError('INVALID_REQUEST'), False, 0


def normalize_txn(row: dict) -> TxnFact:
    """Validate provider facts and invert Plaid's positive-spending amount sign."""
    result = None
    try:
        txn_id, account_id = row['transaction_id'], row['account_id']
        if not all(isinstance(value, str) and value.strip() and len(value) <= 256
                   and all(ord(c) >= 32 and ord(c) != 127 for c in value)
                   for value in (txn_id, account_id)):
            raise ValueError('invalid identifier')
        posted = row['date']
        if not isinstance(posted, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', posted):
            raise ValueError('invalid date')
        date.fromisoformat(posted)
        if type(row['pending']) is not bool or row['iso_currency_code'] != 'USD':
            raise ValueError('invalid transaction')
        amount = row['amount']
        if type(amount) not in (int, Decimal):
            raise ValueError('invalid amount')
        cents = -money.cents_from_amount_str(str(amount))
        name = row.get('merchant_name') or row.get('name') or ''
        if not isinstance(name, str):
            raise ValueError('invalid payee')
        # Sanitize before truncating so long digit runs cannot become short fragments.
        payee = sanitize.strip_p2p_names(sanitize.redact_account_numbers(name) or '')
        payee = ' '.join(''.join(c for c in payee if c.isprintable() or c.isspace()).split())[:200]
        result = TxnFact(txn_id, account_id, posted, cents,
                         sanitize.merchant_norm(payee), payee, row['pending'])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        pass
    if result is None:
        raise PlaidError('INVALID_TRANSACTION')
    return result
