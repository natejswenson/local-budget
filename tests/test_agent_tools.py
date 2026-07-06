"""Agent MCP tools over budget.db: read-only via db.agent_connect(), run_sql
guard, exception scrub, conflict surfacing (design §1/§5, I11b, I16). Tools are
exercised via the SDK-free ``ToolSpec`` registry — ``SPEC_BY_NAME[name].handler``
returns ``{data, rendered}`` (or ``{error}``) and reads the real `transactions`
table (posted rows).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from local_budget import db
from local_budget.agent import tools
from local_budget.ingest import importer

from ofx_fixtures import write_ofx


@pytest.fixture(autouse=True)
def no_network_egress():
    """Override the conftest socket-block: these tests drive async tool handlers
    via asyncio (which needs the self-pipe socket) but perform NO network I/O —
    every tool reads only the local budget.db."""
    yield


def _call(name, args):
    """Dispatch a registry tool by name and return its raw result dict —
    {data, rendered} for the read tools, {error: msg} for a rejected call."""
    return asyncio.run(tools.SPEC_BY_NAME[name].handler(args))


def _seed(_tmp_path=None):
    """Seed budget.db directly with two posted rows (Groceries spend + Income)
    and one quarantined status='conflict' near-dup, so the agent tools — which
    read `transactions WHERE status='posted'` through db.agent_connect() — see a
    known spend total, income, and a surfaced conflict."""
    db.init_schema()
    rows = [
        # fitid, posted_date, amount_cents, status, txn_type, payee, merchant_norm, category
        ("G1", "2026-06-03", -5000, "posted", "DEBIT", "WALMART", "WALMART", "Groceries"),
        ("I1", "2026-06-01", 200000, "posted", "CREDIT", "ACME PAYROLL", "ACME PAYROLL", "Income"),
        ("G2", "2026-06-04", -5500, "conflict", "DEBIT", "WALMART", "WALMART", "Groceries"),
    ]
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, acct_type, acct_last4, acct_hash, created_at) "
            "VALUES (1, 'WF', 'CHECKING', '1234', 'hash-1', ?)", (db.now_iso(),))
        for fitid, dt, cents, status, ttype, payee, mnorm, cat in rows:
            conn.execute(
                "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
                "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, 'memo', ?, ?, 'rule', 'raw', ?)",
                (fitid, dt, cents, status, ttype, payee, mnorm, cat, db.now_iso()))


def test_month_summary(data_dir, tmp_path):
    _seed(tmp_path)
    res = _call("get_month_summary", {"month": "2026-06"})
    payload = res["data"]
    assert payload["spend_total_cents"] == 5000        # conflict row excluded
    assert payload["income_cents"] == 200000
    # I11b: the quarantined near-dup ($55) is surfaced, not silently dropped.
    assert payload["unresolved_conflicts"] == {"count": 1, "total_cents": 5500}
    # rendered markdown carries the real figures and never raw PII.
    rendered = res["rendered"]
    assert "$50.00" in rendered and "$2,000.00" in rendered
    assert "raw" not in rendered and "hash-1" not in rendered


def test_category_breakdown_nets_refunds_like_month_summary(data_dir, tmp_path):
    # S1: a refund (positive amount) in a spend category must reduce that
    # category's total in BOTH get_category_breakdown and get_month_summary.
    db.init_schema()
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1", "name": "WALMART"},
        {"trntype": "CREDIT", "dtposted": "20260604", "amount": "10.00", "fitid": "R1", "name": "WALMART"},
    ]
    importer.import_file(write_ofx(tmp_path / "wf.qfx", txns))

    summary = _call("get_month_summary", {"month": "2026-06"})["data"]
    bd_res = _call("get_category_breakdown", {"month": "2026-06"})
    breakdown = bd_res["data"]
    bd = {r["category"]: r["spent"] for r in breakdown["breakdown"]}

    # Net Groceries = 5000 - 1000 = 4000 in both tools (refund nets the total down).
    assert summary["spend_by_category"]["Groceries"] == 4000
    assert bd["Groceries"] == 4000
    assert bd["Groceries"] == summary["spend_by_category"]["Groceries"]
    assert "$40.00" in bd_res["rendered"]


def test_month_summary_surfaces_uncategorized(data_dir, tmp_path):
    # S1: an unmatched debit (->Uncategorized) is surfaced AND excluded from
    # the spend total, mirroring how conflicts are surfaced.
    db.init_schema()
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1", "name": "WALMART"},
        {"trntype": "DEBIT", "dtposted": "20260607", "amount": "-42.50", "fitid": "U1", "name": "SOME RANDOM VENDOR XYZ"},
    ]
    importer.import_file(write_ofx(tmp_path / "wf.qfx", txns))
    payload = _call("get_month_summary", {"month": "2026-06"})["data"]
    assert payload["uncategorized_spend"]["count"] == 1
    assert payload["uncategorized_spend"]["total_cents"] == 4250
    assert payload["spend_total_cents"] == 5000  # uncategorized excluded


def test_drill_hint_present_at_all_six_numbered_call_sites(data_dir, tmp_path):
    db.init_schema()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, acct_type, acct_last4, acct_hash, created_at) "
            "VALUES (1, 'WF', 'CHECKING', '1234', 'hash-1', ?)", (db.now_iso(),))
        # Groceries spend so get_month_summary / get_category_breakdown / top_merchants have a row.
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'G1', '2026-06-03', -5000, 'posted', 'DEBIT', 'WALMART', 'memo', "
            "'WALMART', 'Groceries', 'rule', 'raw', ?)", (db.now_iso(),))
        # A merchant recurring across 3 distinct months, so recurring_charges has a row.
        for i, dt in enumerate(["2026-04-05", "2026-05-05", "2026-06-05"]):
            conn.execute(
                "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
                "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
                "VALUES (1, ?, ?, -1500, 'posted', 'DEBIT', 'NETFLIX', 'memo', "
                "'NETFLIX', 'Subscriptions', 'rule', 'raw', ?)", (f"N{i}", dt, db.now_iso()))
        # An uncategorized merchant, so review_queue's merchants table has a row.
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'U1', '2026-06-06', -999, 'posted', 'DEBIT', 'MYSTERY VENDOR', 'memo', "
            "'MYSTERY VENDOR', 'Uncategorized', 'rule', 'raw', ?)", (db.now_iso(),))
        # A transaction filed under Checks, so review_queue's checks table has a row.
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'C1', '2026-06-07', -200, 'posted', 'CHECK', 'CHECK 101', 'memo', "
            "'CHECK 101', 'Checks', 'rule', 'raw', ?)", (db.now_iso(),))

    ms = _call("get_month_summary", {"month": "2026-06"})["rendered"]
    assert "Reply with a row number to see that category's transactions." in ms

    cb = _call("get_category_breakdown", {"month": "2026-06"})["rendered"]
    assert "Reply with a row number to drill into that category's transaction list." in cb

    tm = _call("top_merchants", {"month": "2026-06"})["rendered"]
    assert "Reply with a row number to see that merchant's transactions." in tm

    rc = _call("recurring_charges", {})["rendered"]
    assert "Reply with a row number to see that merchant's transactions." in rc

    rq = _call("review_queue", {})["rendered"]
    assert "Reply with a row number to categorize that merchant." in rq
    assert "Reply with a row number to categorize that transaction." in rq


def test_get_month_summary_pct_column_uses_absolute_value_total(data_dir, tmp_path):
    # A category that nets negative (offsetting debit/refund) must not distort
    # the % column: the denominator is sum(abs(v)), not the signed spend_total.
    # spend = {"Groceries": 5000, "RefundCat": -1000} -> pct_total = 6000, so
    # Groceries = round(5000/6000*100) = 83%, not round(5000/4000*100) = 125%
    # (which is what reusing the signed spend_total would produce).
    db.init_schema()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, acct_type, acct_last4, acct_hash, created_at) "
            "VALUES (1, 'WF', 'CHECKING', '1234', 'hash-1', ?)", (db.now_iso(),))
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'G1', '2026-06-03', -5000, 'posted', 'DEBIT', 'WALMART', 'memo', "
            "'WALMART', 'Groceries', 'rule', 'raw', ?)", (db.now_iso(),))
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'R1', '2026-06-05', 1000, 'posted', 'CREDIT', 'REFUND CO', 'memo', "
            "'REFUND CO', 'RefundCat', 'rule', 'raw', ?)", (db.now_iso(),))
    result = _call("get_month_summary", {"month": "2026-06"})
    rendered = result["rendered"]
    assert "83%" in rendered
    assert "125%" not in rendered


def test_top_merchants_empty_state_exact_string(data_dir, tmp_path):
    # bars() returned "" on empty items, so `or "(no spend)"` used to work.
    # table() returns a truthy header-only table on empty rows, so the
    # if/else rewrite must be exercised directly (no dead-`or` regression).
    db.init_schema()
    result = _call("top_merchants", {"month": "2026-06"})
    assert result["rendered"] == "## Top merchants — 2026-06\n(no spend)"


def test_query_transactions_min_amount_cent_boundary(data_dir, tmp_path):
    # M1: min_amount_dollars=19.99 yields a 1999-cent boundary (not 1998).
    db.init_schema()
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-19.98", "fitid": "A1", "name": "WALMART"},
        {"trntype": "DEBIT", "dtposted": "20260604", "amount": "-19.99", "fitid": "A2", "name": "VOLT CAFE"},
    ]
    importer.import_file(write_ofx(tmp_path / "wf.qfx", txns))
    res = _call("query_transactions", {"min_amount_dollars": 19.99})
    payload = res["data"]
    amounts = {r["amount_cents"] for r in payload["rows"]}
    assert -1999 in amounts        # included at the boundary
    assert -1998 not in amounts    # the -$19.98 txn is excluded
    assert payload["count"] == 1
    assert "$19.99" in res["rendered"]


def test_run_sql_select_ok(data_dir, tmp_path):
    _seed(tmp_path)
    res = _call("run_sql",
                {"query": "SELECT category, amount_cents FROM transactions "
                          "WHERE status='posted' ORDER BY amount_cents"})
    assert "error" not in res
    payload = res["data"]
    assert payload["count"] == 2   # two posted rows; the conflict row is filtered out
    assert payload["truncated"] is False
    assert res["rendered"]


def test_run_sql_rejects_non_select(data_dir, tmp_path):
    _seed(tmp_path)
    res = _call("run_sql", {"query": "DELETE FROM transactions"})
    assert "read-only" in res["error"]


def test_run_sql_rejects_forbidden_keyword(data_dir, tmp_path):
    _seed(tmp_path)
    for q in ("SELECT 1; DROP TABLE transactions",
              "WITH x AS (SELECT 1) INSERT INTO transactions VALUES (1)",
              "SELECT * FROM transactions; ATTACH DATABASE 'x' AS y"):
        res = _call("run_sql", {"query": q})
        assert "error" in res, q


def test_run_sql_exception_scrubbed(data_dir, tmp_path):
    # A query that errors must not leak SQLite value/constraint text (I16).
    _seed(tmp_path)
    res = _call("run_sql", {"query": "SELECT nonexistent_column FROM transactions"})
    assert res["error"] == "query failed (rejected or invalid)"
    assert "nonexistent_column" not in json.dumps(res)


def test_query_transactions_filters(data_dir, tmp_path):
    _seed(tmp_path)
    payload = _call("query_transactions", {"category": "Groceries"})["data"]
    assert payload["count"] == 1                       # the conflict row is not status='posted'
    assert payload["rows"][0]["merchant_norm"] == "WALMART"
    assert payload["rows"][0]["account_last4"] == "1234"   # via the accounts JOIN
