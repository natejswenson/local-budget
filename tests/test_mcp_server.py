"""Phase 2 — standalone stdio MCP server (web/mcp_server.py) over the SDK-free
ToolSpec registry. We assert build_server() constructs, that every tool's
input_schema is a JSON-serializable JSON-Schema object, and that the handler
round-trip for a read tool returns the deterministic `rendered` markdown with
the real figures and NO PII (raw_ofx / acct_hash). Driving the low-level Server's
stdio transport is out of scope — the registry + handler round-trip is the surface.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from local_budget import db
from local_budget.agent import tools as agent_tools
from local_budget.web import mcp_server


@pytest.fixture(autouse=True)
def no_network_egress():
    """Override conftest's socket block: asyncio's self-pipe needs a socket, but
    the server runs NO network I/O (it reads only the local budget.db)."""
    yield


_EXPECTED_TOOLS = {
    # read
    "get_month_summary", "get_category_breakdown", "query_transactions",
    "top_merchants", "compare_periods", "recurring_charges", "find_anomalies",
    "run_sql", "save_user_note", "list_user_notes", "delete_user_note",
    # write (Phase 3)
    "set_merchant_category", "set_txn_category", "add_custom_category", "remove_category",
    "set_budget_limit", "clear_budget_limit", "set_expected_income", "split_subscriptions",
    "save_brief",
    # read (Phase 4)
    "budget_overview", "income_by_source", "income_transactions", "subcategory_breakdown",
    "insights", "monthly_trend", "review_queue", "open_conflicts",
}


def _seed():
    db.init_schema()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, acct_type, acct_last4, acct_hash, created_at) "
            "VALUES (1, 'WF', 'CHECKING', '1234', 'hash-secret-1', ?)", (db.now_iso(),))
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'G1', '2026-06-03', -5000, 'posted', 'DEBIT', 'WALMART', 'memo', "
            "'WALMART', 'Groceries', 'rule', 'RAW-OFX-SECRET', ?)", (db.now_iso(),))
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'I1', '2026-06-01', 200000, 'posted', 'CREDIT', 'ACME PAYROLL', 'memo', "
            "'ACME PAYROLL', 'Income', 'rule', 'RAW-OFX-SECRET', ?)", (db.now_iso(),))


def test_build_server_constructs():
    server = mcp_server.build_server()
    assert server.name == "budget"


def test_registry_tool_names():
    assert {s.name for s in agent_tools.TOOL_SPECS} == _EXPECTED_TOOLS
    # name -> spec map is complete and consistent.
    assert set(agent_tools.SPEC_BY_NAME) == _EXPECTED_TOOLS


def test_every_input_schema_json_serializes():
    # A non-serializable Python-class shorthand schema would fail to go over stdio.
    blob = json.dumps([s.input_schema for s in agent_tools.TOOL_SPECS])
    assert blob
    for s in agent_tools.TOOL_SPECS:
        assert s.input_schema["type"] == "object"
        assert "properties" in s.input_schema and "required" in s.input_schema


def test_get_month_summary_round_trip_rendered_no_pii(data_dir):
    _seed()
    result = asyncio.run(agent_tools.SPEC_BY_NAME["get_month_summary"].handler({"month": "2026-06"}))
    rendered = result["rendered"]
    # the deterministic markdown carries the real figures …
    assert "$50.00" in rendered          # spend
    assert "$2,000.00" in rendered       # income
    # … and never leaks the read-denied PII columns.
    assert "RAW-OFX-SECRET" not in rendered
    assert "hash-secret-1" not in rendered
    blob = json.dumps(result)
    assert "RAW-OFX-SECRET" not in blob and "hash-secret-1" not in blob
