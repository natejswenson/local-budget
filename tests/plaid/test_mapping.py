from dataclasses import replace

import pytest

from local_budget import db
from local_budget.connectors.plaid import store
from local_budget.connectors.plaid.types import SyncBatch, TxnFact


def discovery(conn, item, account='a1', **changes):
    payload = dict(account_id=account, type='depository', subtype='checking',
                   mask='1234', balances={'iso_currency_code': 'USD'})
    payload.update(changes)
    store.discover_accounts(conn, item_id=item, accounts=[payload])


def mapped(conn, item):
    discovery(conn, item)
    store.map_account(conn, item_id=item, provider_account_id='a1',
                      local_account_id=1, currency='USD', start_date='2026-10-01')


def batch(*facts, readiness='HISTORICAL_UPDATE_COMPLETE'):
    return SyncBatch(None, 'next', tuple(('added', f) for f in facts), readiness, None)


def fact(id='t1', **changes):
    return replace(TxnFact(id, 'a1', '2026-10-01', -1200, 'SHOP', 'Shop', False), **changes)


def test_discovery_mapping_disabled_and_generation(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item)
        row = conn.execute('SELECT * FROM plaid_accounts').fetchone()
        assert row['local_account_id'] is None and not row['enabled']
        mapped(conn, item)
        assert store.item(conn, item)['mapping_generation'] == 1
        assert conn.execute('SELECT enabled,start_date FROM plaid_accounts').fetchone()[:] == (0, '2026-10-01')
        mapped(conn, item)
        assert store.item(conn, item)['mapping_generation'] == 2


@pytest.mark.parametrize('changes,currency,start', [
    ({'balances': {'iso_currency_code': 'EUR'}}, 'USD', '2026-10-01'),
    ({'type': 'credit', 'subtype': 'credit card'}, 'USD', '2026-10-01'),
    ({'type': 'investment'}, 'USD', '2026-10-01'),
    ({}, 'EUR', '2026-10-01'), ({}, 'USD', ''), ({}, 'USD', '2026-02-30'),
])
def test_mapping_rejects_incompatible_or_missing_boundary(plaid_db, seed_item, changes, currency, start):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item, **changes)
        with pytest.raises(ValueError):
            store.map_account(conn, item_id=item, provider_account_id='a1',
                              local_account_id=1, currency=currency, start_date=start)
        assert store.item(conn, item)['mapping_generation'] == 0


def test_same_mask_provider_accounts_have_distinct_hmac_identity(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item)
        discovery(conn, item, account='a2')
        first = store.create_local_account(conn, item_id=item, provider_account_id='a1')
        second = store.create_local_account(conn, item_id=item, provider_account_id='a2')
        assert first != second
        assert first == store.create_local_account(conn, item_id=item, provider_account_id='a1')
        rows = conn.execute('SELECT acct_hash,acct_last4 FROM accounts WHERE account_id IN (?,?)', (first, second)).fetchall()
        assert rows[0]['acct_hash'] != rows[1]['acct_hash']
        assert all(row['acct_last4'] == '1234' for row in rows)


def test_preview_is_read_only_and_keeps_identical_charges(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        conn.execute("INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv1','2026-10-01',-1200,'SHOP','now')")
        before = conn.total_changes
        review = store.preview(conn, item, batch(fact(), fact('t2'), fact('old', posted_date='2026-09-30'), fact('pending', pending=True)))
        assert conn.total_changes == before
        assert review['unresolved_overlap_count'] == 2
        assert [r['transaction_id'] for r in review['transactions']] == ['t1', 't2', 'old', 'pending']
        assert review['transactions'][0]['candidate_txn_ids'] == [1]
        assert review['transactions'][2]['excluded_reason'] == 'before_start'
        assert review['transactions'][3]['excluded_reason'] == 'pending'
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1
        assert store.item(conn, item)['cursor'] is None
        with pytest.raises(ValueError, match='unresolved_overlap'):
            store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=1, batch=batch(fact()))


def test_activation_generation_history_and_exclusive_source(plaid_db, seed_item):
    item, other = seed_item(), seed_item('other')
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        with pytest.raises(ValueError, match='stale_generation'):
            store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=0, batch=batch())
        with pytest.raises(ValueError, match='history_incomplete'):
            store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=1, batch=batch(readiness='NOT_READY'))
        store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=1, batch=batch(fact()))
        account = dict(conn.execute('SELECT * FROM plaid_accounts').fetchone())
        assert account['history_approved'] == 1
        assert store.eligible(fact(), account)
        assert not store.eligible(fact(pending=True), account)
        assert not store.eligible(fact(currency='EUR'), account)
        assert not store.eligible(fact(posted_date='2026-09-30'), account)
        discovery(conn, other)
        store.map_account(conn, item_id=other, provider_account_id='a1', local_account_id=1, currency='USD', start_date='2026-10-01')
        assert store._account(conn, other, 'a1')['enabled'] == 0
        with pytest.raises(ValueError, match='active_source'):
            store.enable_account(conn, item_id=other, provider_account_id='a1', reviewed_generation=1, batch=batch())


def test_mutators_require_caller_transaction(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        with pytest.raises(ValueError, match='transaction_required'):
            discovery(conn, item)


@pytest.mark.parametrize('local_type', ['CREDITCARD', 'card'])
def test_credit_mapping_and_unknown_local_type(plaid_db, seed_item, local_type):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item, type='credit', subtype='credit card')
        conn.execute("UPDATE accounts SET acct_type=? WHERE account_id=1", (local_type,))
        store.map_account(conn, item_id=item, provider_account_id='a1', local_account_id=1,
                          currency='USD', start_date='2026-10-01')
        conn.execute('UPDATE accounts SET acct_type=NULL WHERE account_id=1')
        with pytest.raises(ValueError, match='account_type_mismatch'):
            store.map_account(conn, item_id=item, provider_account_id='a1', local_account_id=1,
                              currency='USD', start_date='2026-10-01')


def test_map_cli_creates_disabled_mapping(plaid_db, seed_item):
    from click.testing import CliRunner
    from local_budget.connectors.plaid.cli import plaid
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        discovery(conn, item)
    result = CliRunner().invoke(plaid, ['map', str(item), 'a1', '--local-account', '1', '--start-date', '2026-10-01'])
    assert result.exit_code == 0, result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT enabled FROM plaid_accounts').fetchone()[0] == 0


def test_discovery_change_disables_and_invalidates_review(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=1, batch=batch())
        discovery(conn, item, balances={'iso_currency_code': 'EUR'})
        row = conn.execute('SELECT * FROM plaid_accounts').fetchone()
        assert (row['enabled'], row['history_approved'], row['currency']) == (0, 0, 'EUR')
        assert store.item(conn, item)['mapping_generation'] == 3


def test_activation_rechecks_other_source_and_preview_cursor(plaid_db, seed_item):
    item, other = seed_item(), seed_item('other')
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        mapped(conn, other)
        store.enable_account(conn, item_id=other, provider_account_id='a1', reviewed_generation=1, batch=batch())
        with pytest.raises(ValueError, match='active_source'):
            store.enable_account(conn, item_id=item, provider_account_id='a1', reviewed_generation=1, batch=batch())
        conn.execute("UPDATE plaid_items SET cursor='committed' WHERE item_id=?", (item,))
        with pytest.raises(ValueError, match='stale_cursor'):
            store.preview(conn, item, batch())


def test_preview_event_replay_and_returned_coverage(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        events = (('added', fact()), ('modified', fact(amount_cents=-2000)),
                  ('added', fact('t2')), ('removed', 't2'))
        review = store.preview(conn, item, SyncBatch(None, 'next', events, 'HISTORICAL_UPDATE_COMPLETE', None))
        assert review['incoming_total_cents'] == -2000
        assert review['returned_coverage'] == {'first_date': '2026-10-01', 'last_date': '2026-10-01'}
        assert len(review['transactions']) == 1


def test_activation_rejects_rediscovered_incompatible_type(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, item)
        discovery(conn, item, subtype='savings')
        generation = store.item(conn, item)['mapping_generation']
        with pytest.raises(ValueError, match='account_type_mismatch'):
            store.enable_account(conn, item_id=item, provider_account_id='a1',
                                 reviewed_generation=generation, batch=batch())
        assert conn.execute('SELECT enabled FROM plaid_accounts').fetchone()[0] == 0
