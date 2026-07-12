"""Add / remove (merge) spend categories — global vocabulary + safe removal."""
from __future__ import annotations

import json

import pytest

from local_budget import budgets, categories, db
from local_budget.categorize import manual, rules


def _hide(name):
    """Mark a category hidden by writing the setting directly. (Replaces the
    removed `categories.hide_category`, superseded by remove_category's
    merge-on-remove — these tests still exercise the live filtering / unhide /
    skip-hidden paths that read the `hidden_categories` setting.)"""
    db.set_setting("hidden_categories", json.dumps([name]))


def _seed_txn(conn, date_, cents, category, merchant="M", subcategory=None, status="posted"):
    conn.execute(
        "INSERT INTO accounts (institution,acct_type,acct_last4,acct_hash,own_account,created_at) "
        "SELECT 'BANK','CHK','1','h',1,'2026-01-01' WHERE NOT EXISTS (SELECT 1 FROM accounts)")
    aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.execute(
        "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
        "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
        "VALUES (?,?,?,?,?,'M','M','M',?,?,?,'x','',?,1)",
        (aid, f"t{n}", date_, cents, status, merchant, category, subcategory, "2026-06-20"))


# ── vocabulary: hidden subtraction + protected guards ────────────────────────
def test_hidden_subtracts_from_vocabulary(data_dir):
    db.init_schema()
    assert "Dining Out" in categories.spend_categories()       # builtin, present
    _hide("Dining Out")
    assert "Dining Out" not in categories.spend_categories()   # builtin, now hidden
    assert "Dining Out" not in categories.all_categories()
    assert "Dining Out" not in categories.llm_assignable()


def test_add_unhides_case_insensitively(data_dir):
    db.init_schema()
    _hide("Dining Out")
    assert "Dining Out" not in categories.spend_categories()
    # re-adding under DIFFERENT case must restore the hidden builtin (not vanish / dup)
    categories.add_custom_category("dining out")
    assert "Dining Out" in categories.spend_categories()
    assert "dining out" not in categories.custom_categories()  # not added as a custom dup


# ── resurrection guard: seed_builtin_rules skips hidden categories ───────────
def test_seed_builtin_rules_skips_hidden(data_dir):
    db.init_schema()
    _hide("Dining Out")   # Dining Out has a builtin rule (VOLT)
    with db.connect() as conn:
        rules.seed_builtin_rules(conn)
        # the VOLT→Dining Out builtin rule must NOT be seeded for a hidden category
        n = conn.execute("SELECT COUNT(*) FROM category_rules WHERE category='Dining Out'").fetchone()[0]
    assert n == 0


# ── remove = merge ───────────────────────────────────────────────────────────
def test_remove_category_merges_everything(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-05-01", -5000, "Dining Out", merchant="VOLT CLUB")
        _seed_txn(conn, "2026-05-02", -3000, "Dining Out", subcategory="Tournaments")
        _seed_txn(conn, "2026-04-01", -2000, "Entertainment")
        # a categorization rule + a conflict-status row also reference the category
        conn.execute("INSERT INTO category_rules (pattern,category,priority,source,created_at) "
                     "VALUES ('VOLT','Dining Out',5,'manual','x')")
        _seed_txn(conn, "2026-05-03", -1000, "Dining Out", merchant="X", status="conflict")
    budgets.set_limit("Dining Out", 40000)
    budgets.set_limit("Entertainment", 10000)

    r = manual.remove_category("Dining Out", "Entertainment")

    assert r["moved_txns"] == 3 and r["merged_budget"] is True   # 3 Dining Out rows (2 posted + 1 conflict)
    assert r["summed_limit_cents"] == 50000           # 40000 + 10000, summed
    assert "Dining Out" not in categories.spend_categories()
    with db.connect() as conn:
        def _count(table):
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE category='Dining Out'").fetchone()[0]
        assert _count("transactions") == 0 and _count("category_rules") == 0 and _count("budgets") == 0
        # original Entertainment (1) + 3 moved (incl. the NON-posted conflict row) = 4
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='Entertainment'").fetchone()[0] == 4
        # budget summed into a today-dated active row
        assert budgets.active_limits(conn).get(("Entertainment", None)) == 50000
        # rule re-pointed (not resurrected) even after a simulated next-import re-seed
        rules.seed_builtin_rules(conn)
        assert conn.execute("SELECT category FROM category_rules WHERE pattern='VOLT'").fetchone()[0] == "Entertainment"
        assert conn.execute("SELECT COUNT(*) FROM category_rules WHERE category='Dining Out'").fetchone()[0] == 0


def test_remove_custom_category_deletes_from_custom_list(data_dir):
    db.init_schema()
    categories.add_custom_category("Lawyer")
    assert "Lawyer" in categories.custom_categories()
    manual.remove_category("Lawyer", "Fees")
    assert "Lawyer" not in categories.custom_categories()
    assert "Lawyer" not in categories.spend_categories()


def test_remove_conserves_dollars(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-05-01", -5000, "Dining Out")
        _seed_txn(conn, "2026-05-02", -2000, "Entertainment")
        before = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions").fetchone()[0]
    manual.remove_category("Dining Out", "Entertainment")
    with db.connect() as conn:
        after = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions").fetchone()[0]
        ent = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE category='Entertainment'").fetchone()[0]
    assert before == after == ent == -7000   # merge moves dollars, never loses them


def test_remove_validation(data_dir):
    db.init_schema()
    with pytest.raises(manual.CategorizeError):
        manual.remove_category("Dining Out", "Dining Out")        # self-merge
    with pytest.raises(manual.CategorizeError):
        manual.remove_category("Income", "Entertainment")         # protected source
    with pytest.raises(manual.CategorizeError):
        manual.remove_category("Dining Out", "Random")            # protected target
    with pytest.raises(manual.CategorizeError):
        manual.remove_category("NotACategory", "Entertainment")   # unknown source
    with pytest.raises(manual.CategorizeError):
        manual.remove_category("Dining Out", "NotACategory")      # unknown target
