"""Manual merchant categorization + user-defined categories."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from local_budget import categories, db
from local_budget.categorize import manual
from local_budget.cli import main
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
    manual.set_merchant_category("CHECK", "Random", confirm_random=True)
    # categorize ONE check to Housing without touching the other
    with db.connect() as conn:
        tid = conn.execute("SELECT txn_id FROM transactions WHERE fitid='C1'").fetchone()[0]
    manual.set_transaction_category(tid, "Housing")
    with db.connect() as conn:
        rows = {r["fitid"]: r["category"] for r in conn.execute("SELECT fitid, category FROM transactions").fetchall()}
    assert rows["C1"] == "Housing" and rows["C2"] == "Random"  # only the one changed


def test_set_merchant_category_random_requires_confirm(data_dir, tmp_path):
    _seed(tmp_path)
    with pytest.raises(manual.CategorizeError):
        manual.set_merchant_category("HARDWARE CO", "Random")
    assert manual.set_merchant_category("HARDWARE CO", "Random", confirm_random=True) == 2
    # every other category is unaffected by the guard
    assert manual.set_merchant_category("HARDWARE CO", "Shopping") == 2


def test_set_transaction_category_random_requires_confirm(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "wf.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-100.00", "fitid": "C1", "name": "CHECK"}]))
    with db.connect() as conn:
        tid = conn.execute("SELECT txn_id FROM transactions WHERE fitid='C1'").fetchone()[0]
    with pytest.raises(manual.CategorizeError):
        manual.set_transaction_category(tid, "Random")
    manual.set_transaction_category(tid, "Random", confirm_random=True)
    with db.connect() as conn:
        cat = conn.execute("SELECT category FROM transactions WHERE txn_id=?", (tid,)).fetchone()["category"]
    assert cat == "Random"


def test_remove_category_rejects_mismatched_floor_direction(data_dir, tmp_path):
    _seed(tmp_path)
    categories.add_custom_category("Home Improvement")
    categories.mark_floor_category("Home Improvement")
    with pytest.raises(manual.CategorizeError):
        manual.remove_category("Home Improvement", "Shopping")
    # same-direction merge still works (both ceiling here)
    categories.add_custom_category("Kid Activities")
    result = manual.remove_category("Kid Activities", "Shopping")
    assert result["moved_txns"] == 0


def test_remove_category_clears_floor_marking_so_recreated_category_is_not_floor(data_dir, tmp_path):
    # A removed floor category must not leave its name floor-typed behind — otherwise
    # re-creating a category with the same name later silently inherits stale floor
    # semantics (flips over/under-budget meaning with no indication why).
    _seed(tmp_path)
    categories.add_custom_category("Investments")
    categories.mark_floor_category("Investments")
    categories.add_custom_category("Savings")
    categories.mark_floor_category("Savings")
    assert categories.is_floor("Investments")

    manual.remove_category("Investments", "Savings")  # merge into another floor category
    assert not categories.is_floor("Investments")

    # re-creating a category with the same (now-hidden) name must NOT come back floor-typed
    categories.add_custom_category("Investments")
    assert not categories.is_floor("Investments")


# ── CLI: Random-category confirmation (must not trap the user in a retry loop) ──
def test_cli_review_confirms_random_then_applies(data_dir, tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(main, ["review"], input="Random\ny\n")
    assert result.exit_code == 0, result.output
    with db.connect() as conn:
        rows = conn.execute("SELECT category FROM transactions").fetchall()
    assert all(r["category"] == "Random" for r in rows)


def test_cli_review_declining_random_reprompts_instead_of_trapping(data_dir, tmp_path):
    _seed(tmp_path)
    # decline the Random confirmation, then answer with a real category — must
    # not infinite-loop or crash.
    result = CliRunner().invoke(main, ["review"], input="Random\nn\nShopping\n")
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
    with db.connect() as conn:
        rows = conn.execute("SELECT category FROM transactions").fetchall()
    assert all(r["category"] == "Shopping" for r in rows)


def test_cli_set_category_random_requires_confirm_flag(data_dir, tmp_path):
    _seed(tmp_path)
    result = CliRunner().invoke(main, ["set-category", "HARDWARE CO", "Random"])
    assert result.exit_code != 0
    result = CliRunner().invoke(main, ["set-category", "HARDWARE CO", "Random", "--confirm-random"])
    assert result.exit_code == 0, result.output
    with db.connect() as conn:
        rows = conn.execute("SELECT category FROM transactions").fetchall()
    assert all(r["category"] == "Random" for r in rows)
