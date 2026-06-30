"""The seeded eval DB builds with the expected tables/rows (NO model calls)."""
from __future__ import annotations

import os
import sqlite3

import eval_seed


def _restore_env(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("LOCAL_BUDGET_DATA_DIR", None)
    else:
        os.environ["LOCAL_BUDGET_DATA_DIR"] = prev


def test_build_eval_db_creates_expected_tables_and_rows(tmp_path):
    prev = os.environ.get("LOCAL_BUDGET_DATA_DIR")
    try:
        db_path = eval_seed.build_eval_db(tmp_path / "evaldb")
        assert db_path.is_absolute() and db_path.exists()

        conn = sqlite3.connect(db_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for expected in ("transactions", "budgets", "import_conflicts",
                             "settings", "accounts"):
                assert expected in tables

            n_txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            assert n_txns == len(eval_seed._TXNS)

            cats = {r[0] for r in conn.execute(
                "SELECT DISTINCT category FROM transactions")}
            assert {"Groceries", "Dining", "Income"} <= cats

            assert conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM import_conflicts WHERE resolved=0").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE category='Income'").fetchone()[0] == 1
            assert conn.execute(
                "SELECT value FROM settings WHERE key='expected_income_cents'"
            ).fetchone()[0] == "500000"
        finally:
            conn.close()
    finally:
        _restore_env(prev)


def test_build_eval_db_resolves_to_absolute_and_is_idempotent(tmp_path):
    prev = os.environ.get("LOCAL_BUDGET_DATA_DIR")
    try:
        target = tmp_path / "evaldb"
        first = eval_seed.build_eval_db(target)
        # Rebuild over the existing DB — row counts stay fixed (not doubled).
        second = eval_seed.build_eval_db(target)
        assert first == second
        conn = sqlite3.connect(second)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM transactions").fetchone()[0] == len(eval_seed._TXNS)
        finally:
            conn.close()
    finally:
        _restore_env(prev)
