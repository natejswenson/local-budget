import sqlite3
import pytest
from local_budget import db

PRIVATE = ('plaid_meta', 'plaid_items', 'plaid_link_sessions', 'plaid_accounts',
           'plaid_transactions', 'plaid_sync_runs', 'plaid_changes')


@pytest.mark.parametrize('table', PRIVATE)
@pytest.mark.parametrize('projection', ['*', 'COUNT(*)'])
def test_private_tables_denied(plaid_db, table, projection):
    # A missing table is not evidence of an authorizer boundary.
    with db.connect(plaid_db) as conn:
        conn.execute(f'SELECT {projection} FROM {table}').fetchall()
    with db.agent_connect(plaid_db) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(f'SELECT {projection} FROM {table}').fetchall()


def test_private_cursor_is_not_agent_readable(plaid_db, seed_item):
    seed_item()
    with db.agent_connect(plaid_db) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute('SELECT cursor FROM plaid_items').fetchall()


@pytest.mark.parametrize('query', [
    'SELECT p.cursor FROM accounts a JOIN plaid_items p ON p.item_id=a.account_id',
    'SELECT * FROM private_view',
    'SELECT future_private FROM plaid_items',
    "UPDATE settings SET value='bad' WHERE EXISTS (SELECT 1 FROM plaid_items)",
])
def test_indirect_and_future_reads_denied(plaid_db, query):
    with db.connect(plaid_db) as conn:
        conn.execute('ALTER TABLE plaid_items ADD COLUMN future_private TEXT')
        conn.execute('CREATE VIEW private_view AS SELECT cursor FROM plaid_items')
    with db.agent_connect(plaid_db, write=True) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(query).fetchall()


@pytest.mark.parametrize('table', PRIVATE + ('plaid_status_public',))
def test_connector_tables_not_agent_writable(plaid_db, table):
    with db.connect(plaid_db) as conn:
        conn.execute(f'SELECT COUNT(*) FROM {table}')
    with db.agent_connect(plaid_db, write=True) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(f'DELETE FROM {table}')


def test_public_status_agent_readable(plaid_db):
    with db.agent_connect(plaid_db) as conn:
        assert conn.execute('SELECT * FROM plaid_status_public').fetchall() == []
