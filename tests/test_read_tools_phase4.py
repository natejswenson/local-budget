"""Phase-4 backing read tools — {data, rendered}, correct figures, the
budget_overview None-limit render guard, and redaction-on-read for open_conflicts
+ run_sql (design §3/S6)."""
from __future__ import annotations

import asyncio

import pytest

from local_budget import budgets as budgets_mod
from local_budget import db
from local_budget.agent import tools


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir):
    yield


@pytest.fixture(autouse=True)
def no_network_egress():
    yield


def _call(name, args=None):
    return asyncio.run(tools.SPEC_BY_NAME[name].handler(args or {}))


def _seed():
    db.init_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (account_id, acct_last4, acct_hash, created_at) "
                     "VALUES (1,'1234','h',?)", (db.now_iso(),))
        rows = [
            ("G1", -8000, "Groceries", None, "WALMART"),
            ("I1", 300000, "Income", None, "ACME PAYROLL"),
            ("S1", -1500, "Subscriptions", "Netflix", "NETFLIX"),
            ("D1", -4000, "Dining Out", None, "CHIPOTLE"),   # un-budgeted spend category
        ]
        for fitid, cents, cat, sub, mnorm in rows:
            conn.execute(
                "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
                "txn_type, payee, memo, merchant_norm, category, subcategory, raw_ofx, imported_at) "
                "VALUES (1, ?, '2026-06-03', ?, 'posted', 'DEBIT', ?, 'm', ?, ?, ?, 'raw', ?)",
                (fitid, cents, mnorm, mnorm, cat, sub, db.now_iso()))
    budgets_mod.set_limit("Groceries", 50000)  # Groceries budgeted; Dining Out is not


def test_budget_overview_has_data_and_rendered_with_none_guard():
    _seed()
    r = _call("budget_overview", {"month": "2026-06"})
    assert "categories" in r["data"]
    # Groceries budgeted ($500), Dining Out un-budgeted → its budget/% render as "—" (no crash).
    assert "$500.00" in r["rendered"] and "—" in r["rendered"]
    assert "$80.00" in r["rendered"]  # Groceries spend


def test_income_by_source():
    _seed()
    r = _call("income_by_source", {"month": "2026-06"})
    assert r["data"]["sources"] and "$3,000.00" in r["rendered"]


def test_insights_and_trend_and_subcat():
    _seed()
    assert "Ways to save" in _call("insights", {"month": "2026-06"})["rendered"]
    assert "Monthly trend" in _call("monthly_trend", {})["rendered"]
    assert "Netflix" in _call("subcategory_breakdown", {"category": "Subscriptions", "month": "2026-06"})["rendered"]


def test_review_queue_two_sections():
    _seed()
    r = _call("review_queue")
    assert "Uncategorized merchants" in r["rendered"] and "Checks to review" in r["rendered"]
    assert "merchants" in r["data"] and "checks" in r["data"]


def test_open_conflicts_redacts_incoming_payee():
    _seed()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO import_conflicts (account_id, kind, existing_amount_cents, existing_posted_date, "
            "incoming_amount_cents, incoming_posted_date, incoming_payee, detected_at, resolved) "
            "VALUES (1, 'near_duplicate', -8000, '2026-06-03', -8000, '2026-06-03', "
            "'ZELLE TO ACCT 1234567890', ?, 0)", (db.now_iso(),))
    r = _call("open_conflicts")
    # the account number is redacted in BOTH data and rendered
    assert "1234567890" not in r["rendered"]
    assert all("1234567890" not in str(c.get("incoming_payee")) for c in r["data"]["conflicts"])
    assert "near_duplicate" in r["rendered"]


def test_run_sql_redacts_payee():
    _seed()
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET payee='PAYEE 9876543210' WHERE fitid='G1'")
    r = _call("run_sql", {"query": "SELECT payee FROM transactions WHERE merchant_norm='WALMART'"})
    assert "9876543210" not in str(r["data"]["rows"]) and "9876543210" not in r["rendered"]


def test_all_phase4_read_tools_registered():
    new = {"budget_overview", "income_by_source", "income_transactions", "subcategory_breakdown",
           "insights", "monthly_trend", "review_queue", "open_conflicts"}
    assert new <= set(tools.SPEC_BY_NAME)
