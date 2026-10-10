from decimal import Decimal

import pytest

from local_budget.connectors.plaid.client import PlaidError, normalize_txn


def row(**changes):
    return dict(transaction_id='p1', account_id='a1', date='2026-10-01',
                amount=Decimal('12.34'), pending=False, iso_currency_code='USD',
                name='COFFEE 123456789', **{}) | changes


@pytest.mark.parametrize('amount,cents', [(Decimal('12.34'), -1234),
                                        (Decimal('-12.34'), 1234), (0, 0)])
def test_exact_ledger_sign_and_sanitization(amount, cents):
    fact = normalize_txn(row(amount=amount))
    assert fact.amount_cents == cents
    assert fact.posted_date == '2026-10-01'
    assert fact.transaction_id == 'p1' and fact.account_id == 'a1'
    assert '123456789' not in fact.payee + fact.merchant_norm


@pytest.mark.parametrize('changes', [
    {'amount': Decimal('0.001')}, {'amount': Decimal('NaN')},
    {'amount': Decimal('Infinity')}, {'amount': True}, {'amount': 0.29},
    {'amount': '12.34'}, {'transaction_id': ''}, {'account_id': None},
    {'date': '2026-02-30'}, {'date': '20261001'}, {'date': None},
    {'pending': 'false'}, {'pending': None}, {'iso_currency_code': 'EUR'},
    {'iso_currency_code': None}, {'name': {'secret': 'SENTINEL'}},
])
def test_invalid_facts_fail_safely(changes):
    with pytest.raises(PlaidError) as caught:
        normalize_txn(row(**changes))
    assert caught.value.code == 'INVALID_TRANSACTION'
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert 'SENTINEL' not in repr(caught.value)


def test_pending_is_retained_for_application_filtering():
    assert normalize_txn(row(pending=True)).pending is True


def test_hostile_payee_is_bounded_redacted_and_without_controls():
    fact = normalize_txn(row(name='\x00\x1b[31m' + 'A' * 1000 + ' 4111/2222/3333/4444'))
    assert len(fact.payee) <= 200
    assert '\x00' not in fact.payee and '\x1b' not in fact.payee
    assert '4111' not in fact.payee


@pytest.mark.parametrize('missing', ['transaction_id', 'account_id'])
def test_missing_identifier_keys_fail_safely(missing):
    transaction = row()
    del transaction[missing]
    with pytest.raises(PlaidError) as caught:
        normalize_txn(transaction)
    assert caught.value.code == 'INVALID_TRANSACTION'
    assert caught.value.__cause__ is None and caught.value.__context__ is None
