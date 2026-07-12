"""Security regression net (design §7.12) — the one-DB model.

Each test pins a security-at-rest invariant so a future refactor can't quietly
reintroduce a money-data leak. The agent authorizer allow/deny MATRIX lives in
test_agent_connect.py; this file keeps only the guarantees unique to
confidentiality-at-rest (PII redaction on import, no full account number stored)
plus one light read-deny smoke check via db.agent_connect().
"""
from __future__ import annotations

import sqlite3

import pytest

from local_budget import db, sanitize
from local_budget.ingest import importer

from ofx_fixtures import write_ofx

# A full account number that appears in the OFX account header, and a 16-digit
# card-style number embedded in a transaction memo (the raw text the importer
# stores in transactions.raw_ofx).
FULL_ACCT = "9876543210"
MEMO_ACCT = "4111222233334444"


def _import_one(tmp_path):
    db.init_schema()
    p = write_ofx(
        tmp_path / "stmt.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00",
          "fitid": "G1", "name": "WALMART", "memo": f"ACCT {MEMO_ACCT}"}],
        acctid=FULL_ACCT,
    )
    return importer.import_file(p)


# ── I14: account-number redaction at import (raw_ofx) ─────────────────────────
def test_redact_account_numbers_unit():
    # The security primitive: any >=7-digit run is replaced with [REDACTED].
    out = sanitize.redact_account_numbers(f"ACCT {MEMO_ACCT} ref 12")
    assert MEMO_ACCT not in out
    assert sanitize.REDACTED in out
    assert "12" in out                     # a short (<7) run survives, by design


def test_import_redacts_account_number_in_raw_ofx(data_dir, tmp_path):
    _import_one(tmp_path)
    with db.connect() as conn:
        raw = conn.execute(
            "SELECT raw_ofx FROM transactions WHERE fitid='G1'").fetchone()["raw_ofx"]
    assert MEMO_ACCT not in raw            # the 16-digit run never reaches storage
    assert sanitize.REDACTED in raw


def test_import_redacts_account_number_in_stored_payee_memo(data_dir, tmp_path):
    # I14 extended to the payee/memo columns: raw_ofx was redacted at import but
    # payee/memo were stored verbatim, leaving an unredacted copy of any
    # embedded account-style run at rest. merchant_norm derivation still runs on
    # the raw text, so grouping is unchanged.
    _import_one(tmp_path)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payee, memo, merchant_norm FROM transactions WHERE fitid='G1'"
        ).fetchone()
    assert MEMO_ACCT not in (row["memo"] or "")
    assert sanitize.REDACTED in row["memo"]
    assert row["payee"] == "WALMART"            # no digits → untouched
    assert row["merchant_norm"] == "WALMART"    # grouping key unchanged


# ── S7: no full account number at rest in accounts ───────────────────────────
def test_accounts_store_only_last4_and_hash_never_full_number(data_dir, tmp_path):
    _import_one(tmp_path)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM accounts").fetchone()
    assert row["acct_last4"] == FULL_ACCT[-4:]          # last4 only
    assert row["acct_hash"]                              # HMAC present
    # The full account number must not appear in ANY accounts column.
    for value in dict(row).values():
        assert FULL_ACCT not in str(value)


# ── I12: agent connection read-deny smoke (matrix lives elsewhere) ────────────
# Supersedes decision #4 (payee/memo were agent-readable): raw payee/memo carry
# untruncated counterparty text (Zelle/Venmo names, full statement strings), so
# they are read-denied like raw_ofx — merchant_norm is the agent's merchant text.
def test_agent_connect_blocks_pii_columns_including_payee_memo(data_dir, tmp_path):
    _import_one(tmp_path)
    with db.agent_connect() as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT raw_ofx FROM transactions LIMIT 1").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT acct_hash FROM accounts LIMIT 1").fetchall()
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("SELECT payee, memo FROM transactions LIMIT 1").fetchall()
        # the sanitized merchant text stays readable.
        row = conn.execute("SELECT merchant_norm FROM transactions LIMIT 1").fetchone()
    assert row is not None and row["merchant_norm"]
