"""Phase 0 — db.agent_connect() column-level authorizer (design §1).

The agent/skill layer's ONLY door into budget.db. write=False denies every
write; write=True allows ONLY the derived columns {category,subcategory,
category_source} on transactions plus the app-config tables {category_rules,
budgets,settings}. Imported facts + status + every unlisted table/column are
immutable to skills; raw_ofx/payee/memo/acct_hash/inbox_files.filename/
import_runs PII columns are read-denied (the statement aborts).
"""
from __future__ import annotations

import sqlite3

import pytest

from local_budget import db


@pytest.fixture
def seeded_db(tmp_path):
    """A real budget.db with one account + one transaction (txn_id=1)."""
    path = tmp_path / "budget.db"
    db.init_schema(path)
    with db.connect(path) as c:
        c.execute(
            "INSERT INTO accounts (account_id, institution, acct_last4, acct_hash) "
            "VALUES (1, 'BANK', '1234', 'hash-1')"
        )
        c.execute(
            "INSERT INTO transactions "
            "(txn_id, account_id, fitid, posted_date, amount_cents, status, "
            " txn_type, payee, memo, merchant_norm, category, raw_ofx, imported_at) "
            "VALUES (1, 1, 'F1', '2026-01-15', -500, 'posted', 'DEBIT', "
            "'STARBUCKS 1234567', 'memo', 'STARBUCKS', 'Uncategorized', "
            "'<acct>1234567</acct>', '2026-01-15T00:00:00')"
        )
    return path


# --- read side ---
def test_agent_connect_reads_merchant_norm_and_category(seeded_db):
    # Supersedes decision #4: raw payee/memo are now read-denied (see below);
    # merchant_norm + the derived columns remain the agent's readable surface.
    with db.agent_connect(seeded_db) as c:
        row = c.execute(
            "SELECT merchant_norm, category FROM transactions LIMIT 1").fetchone()
    assert row is not None and row["merchant_norm"] == "STARBUCKS"


@pytest.mark.parametrize("col", ["payee", "memo"])
def test_agent_connect_denies_payee_memo_read(seeded_db, col):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"SELECT {col} FROM transactions LIMIT 1").fetchall()


def test_agent_connect_denies_raw_ofx_read(seeded_db):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("SELECT raw_ofx FROM transactions LIMIT 1").fetchall()


def test_agent_connect_denies_acct_hash_read(seeded_db):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("SELECT acct_hash FROM accounts LIMIT 1").fetchall()


def test_select_star_on_transactions_aborts(seeded_db):       # design M1
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("SELECT * FROM transactions LIMIT 1").fetchall()


@pytest.mark.parametrize(
    "tbl,col",
    [("inbox_files", "filename"), ("import_runs", "source_name"), ("import_runs", "error_message")],
)
def test_read_deny_covers_all_pii_columns(seeded_db, tbl, col):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"SELECT {col} FROM {tbl} LIMIT 1").fetchall()


# --- write side: read-only connection denies ALL writes ---
def test_readonly_conn_denies_category_update(seeded_db):
    with db.agent_connect(seeded_db) as c:                     # write=False default
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE transactions SET category='X' WHERE txn_id=1")


# --- write side: write connection allows derived, denies facts/status/tables ---
def test_write_conn_allows_category_update(seeded_db):
    with db.agent_connect(seeded_db, write=True) as c:
        c.execute("UPDATE transactions SET category='Groceries' WHERE txn_id=1")
    with db.connect(seeded_db) as c:
        assert c.execute("SELECT category FROM transactions WHERE txn_id=1").fetchone()[0] == "Groceries"


@pytest.mark.parametrize("col", ["subcategory", "category_source"])
def test_write_conn_allows_other_derived_cols(seeded_db, col):
    with db.agent_connect(seeded_db, write=True) as c:
        c.execute(f"UPDATE transactions SET {col}='x' WHERE txn_id=1")


@pytest.mark.parametrize(
    "col", ["amount_cents", "posted_date", "payee", "memo", "status", "txn_type", "merchant_norm"]
)
def test_write_conn_denies_fact_update(seeded_db, col):
    with db.agent_connect(seeded_db, write=True) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(f"UPDATE transactions SET {col}='x' WHERE txn_id=1")


def test_write_conn_denies_transactions_insert_delete(seeded_db):
    with db.agent_connect(seeded_db, write=True) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM transactions WHERE txn_id=1")


def test_write_conn_allows_category_rules_budgets_settings(seeded_db):  # all 3 write tables
    with db.agent_connect(seeded_db, write=True) as c:
        c.execute("INSERT INTO category_rules (pattern, category, source, priority) VALUES ('Z','Y','manual',5)")
        c.execute("INSERT INTO budgets (category, limit_cents, effective_from) VALUES ('Groceries', 50000, '2026-01')")
        c.execute("INSERT INTO settings (key, value) VALUES ('k','v') ON CONFLICT(key) DO UPDATE SET value='v'")


def test_write_conn_denies_unlisted_table(seeded_db):         # default-deny, design S1
    with db.agent_connect(seeded_db, write=True) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("INSERT INTO merchant_aliases (pattern, canonical, source) VALUES ('a','b','manual')")


def test_agent_connect_denies_attach(seeded_db):
    with db.agent_connect(seeded_db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("ATTACH DATABASE ':memory:' AS x")
