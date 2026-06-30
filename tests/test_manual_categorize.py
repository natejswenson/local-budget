"""Manual merchant categorization + user-defined categories."""
from __future__ import annotations

import pytest

from local_budget import categories, db
from local_budget.categorize import manual
from local_budget.ingest import importer

from ofx_fixtures import write_ofx


def _seed(tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "wf.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "A1", "name": "HARDWARE CO 1465"},
        {"trntype": "DEBIT", "dtposted": "20260610", "amount": "-25.00", "fitid": "A2", "name": "HARDWARE CO 1465"},
    ]))


def test_set_merchant_category_pins_and_updates(data_dir, tmp_path):
    _seed(tmp_path)
    n = manual.set_merchant_category("HARDWARE CO", "Shopping")
    assert n == 2   # substring match catches "HARDWARE CO 1465"
    with db.connect() as conn:
        rows = conn.execute("SELECT category, category_source FROM transactions").fetchall()
    assert all(r["category"] == "Shopping" and r["category_source"] == "manual" for r in rows)


def test_manual_rule_wins_and_persists_for_future_imports(data_dir, tmp_path):
    _seed(tmp_path)
    categories.add_custom_category("Home Improvement")
    manual.set_merchant_category("HARDWARE CO", "Home Improvement")
    # a NEW import of the same merchant should auto-apply the manual rule
    importer.import_file(write_ofx(tmp_path / "wf2.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260701", "amount": "-9.00", "fitid": "A3", "name": "HARDWARE CO 1465"}]),
        detect_near_duplicates=False)
    with db.connect() as conn:
        cat = conn.execute("SELECT category FROM transactions WHERE fitid='A3'").fetchone()["category"]
    assert cat == "Home Improvement"


def test_add_custom_category(data_dir, tmp_path):
    db.init_schema()
    categories.add_custom_category("Kid Activities")
    assert "Kid Activities" in categories.spend_categories()
    assert "Kid Activities" in categories.all_categories()
    # custom categories count as spend
    assert categories.is_spend("Kid Activities")
    # idempotent / case-insensitive dedup
    categories.add_custom_category("kid activities")
    assert sorted(categories.custom_categories()) == ["Kid Activities"]


def test_set_unknown_category_rejected(data_dir, tmp_path):
    _seed(tmp_path)
    with pytest.raises(manual.CategorizeError):
        manual.set_merchant_category("HARDWARE CO", "Nonsense Category")
    # but a custom one is accepted
    categories.add_custom_category("Home Improvement")
    assert manual.set_merchant_category("HARDWARE CO", "Home Improvement") == 2


def test_set_transaction_category_single_row(data_dir, tmp_path):
    # Per-transaction categorize (no rule) — e.g. individual checks.
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "wf.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-100.00", "fitid": "C1", "name": "CHECK"},
        {"trntype": "DEBIT", "dtposted": "20260604", "amount": "-50.00", "fitid": "C2", "name": "CHECK"}]))
    manual.set_merchant_category("CHECK", "Random")
    # categorize ONE check to Housing without touching the other
    with db.connect() as conn:
        tid = conn.execute("SELECT txn_id FROM transactions WHERE fitid='C1'").fetchone()[0]
    manual.set_transaction_category(tid, "Housing")
    with db.connect() as conn:
        rows = {r["fitid"]: r["category"] for r in conn.execute("SELECT fitid, category FROM transactions").fetchall()}
    assert rows["C1"] == "Housing" and rows["C2"] == "Random"  # only the one changed
