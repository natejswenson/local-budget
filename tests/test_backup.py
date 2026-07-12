"""Backup destination allowlist (design §7.11/S2/M3) + restore recovers
categories/budgets (AC8/M4)."""
from __future__ import annotations

import sqlite3

import pytest

from local_budget import backup, budgets, db
from local_budget.ingest import importer

from ofx_fixtures import write_ofx


def _seed(tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(
        tmp_path / "stmt.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1", "name": "WALMART"}]))
    budgets.set_limit("Groceries", 4000)


def test_backup_default_under_data_dir(data_dir, tmp_path):
    _seed(tmp_path)
    dest = backup.backup()
    assert dest.exists()
    assert str(dest).startswith(str(data_dir.resolve()))


def test_backup_refuses_outside_allowlist(data_dir, tmp_path):
    _seed(tmp_path)
    with pytest.raises(backup.BackupError):
        backup.backup(str(tmp_path / "cloud_synced" / "leak.db"))


def test_backup_allows_configured_backup_root(data_dir, tmp_path):
    _seed(tmp_path)
    root = tmp_path / "mybackups"
    root.mkdir()
    db.set_setting("backup_root", str(root))
    dest = backup.backup(str(root / "b.db"))
    assert dest.exists()


def test_restore_recovers_categories_and_budgets(data_dir, tmp_path):
    # AC8/M4: a restored masked-DB backup recovers transactions + categories + budgets.
    _seed(tmp_path)
    dest = backup.backup()
    restored = sqlite3.connect(dest)
    try:
        cat = restored.execute("SELECT category FROM transactions").fetchone()[0]
        lim = restored.execute("SELECT limit_cents FROM budgets").fetchone()[0]
    finally:
        restored.close()
    assert cat == "Groceries"
    assert lim == 4000
