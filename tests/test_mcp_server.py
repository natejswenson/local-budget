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
    "mark_floor_category", "unmark_floor_category",
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


def _seed_two_months():
    """June: one $50 Groceries charge. July: one $30 Groceries charge — enough
    to tell whether a query is scoped to the right month."""
    db.init_schema()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, acct_type, acct_last4, acct_hash, created_at) "
            "VALUES (1, 'WF', 'CHECKING', '1234', 'hash-secret-1', ?)", (db.now_iso(),))
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'G-JUN', '2026-06-15', -5000, 'posted', 'DEBIT', 'WALMART', 'memo', "
            "'WALMART', 'Groceries', 'rule', 'RAW-OFX-SECRET', ?)", (db.now_iso(),))
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
            "txn_type, payee, memo, merchant_norm, category, category_source, raw_ofx, imported_at) "
            "VALUES (1, 'G-JUL', '2026-07-15', -3000, 'posted', 'DEBIT', 'WALMART', 'memo', "
            "'WALMART', 'Groceries', 'rule', 'RAW-OFX-SECRET', ?)", (db.now_iso(),))


def test_query_transactions_month_scopes_to_that_month_only(data_dir):
    _seed_two_months()
    result = asyncio.run(agent_tools.SPEC_BY_NAME["query_transactions"].handler(
        {"category": "Groceries", "month": "2026-06"}))
    rows = result["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["posted_date"] == "2026-06-15"
    assert "$30.00" not in result["rendered"]


def test_query_transactions_month_wins_when_days_also_given(data_dir):
    _seed_two_months()
    # A `days` value that alone would reach back into June must be ignored
    # entirely once `month` is given — the July charge must not contaminate.
    result = asyncio.run(agent_tools.SPEC_BY_NAME["query_transactions"].handler(
        {"category": "Groceries", "month": "2026-07", "days": 365}))
    rows = result["data"]["rows"]
    assert len(rows) == 1
    assert rows[0]["posted_date"] == "2026-07-15"


def test_insights_under_target_rendered_separately_from_ways_to_save(data_dir, tmp_path):
    """S1 regression: a floor category (e.g. Investments) short of its target must
    NOT render under '## Ways to save' with plain '- label: $amount' — that reads
    as 'cut this' when reports.insights() actually means 'add more'. It must render
    under its own heading with 'short of target' wording instead."""
    from local_budget import budgets, categories
    from local_budget.categorize.manual import set_merchant_category as setc
    from local_budget.ingest import importer
    from ofx_fixtures import write_ofx

    db.init_schema()
    categories.mark_floor_category("Investments")
    importer.import_file(write_ofx(tmp_path / "wf.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-200.00", "fitid": "N1", "name": "529 PLAN"}]))
    setc("529 PLAN", "Investments")
    budgets.set_limit("Investments", 30000)  # $300 target, $200 spent -> $100 short

    result = asyncio.run(agent_tools.SPEC_BY_NAME["insights"].handler({"month": "all"}))
    rendered = result["rendered"]
    assert any(i["kind"] == "under_target" for i in result["data"]["insights"])
    ways_to_save = rendered.split("## Under target")[0]
    assert "Investments" not in ways_to_save          # not flattened into "Ways to save"
    assert "## Under target" in rendered
    assert "Investments: $100.00 short of target" in rendered


def test_top_merchants_data_carries_resolved_month(data_dir):
    _seed_two_months()
    result = asyncio.run(agent_tools.SPEC_BY_NAME["top_merchants"].handler({"month": "2026-06"}))
    assert result["data"]["month"] == "2026-06"
    # table() (unlike bars()) always adds a Row header for a numbered list.
    assert "Row" in result["rendered"]


def test_get_month_summary_dict_order_matches_numbered_table_order(data_dir):
    """Architecture §2's sub-invariant: row N of the numbered table() output must
    equal the Nth entry of data["spend_by_category"] in dict-insertion order —
    checkable, not assumed, since both are built from the same sorted() call."""
    _seed()
    result = asyncio.run(agent_tools.SPEC_BY_NAME["get_month_summary"].handler({"month": "2026-06"}))
    dict_order = list(result["data"]["spend_by_category"].keys())
    table_lines = [ln for ln in result["rendered"].splitlines() if ln.startswith("| ")]
    data_rows = table_lines[2:]  # drop the header line and the --- separator line
    table_order = [ln.removeprefix("| ").removesuffix(" |").split(" | ")[1] for ln in data_rows]
    assert dict_order == table_order


def test_get_category_breakdown_row_column_does_not_collide_with_count_column(data_dir):
    """Row (the new drill-down index) and # (the pre-existing transaction count)
    must both appear as distinct headers — the whole reason Row was chosen
    instead of reusing #."""
    _seed()
    result = asyncio.run(agent_tools.SPEC_BY_NAME["get_category_breakdown"].handler({"month": "2026-06"}))
    header_line = result["rendered"].splitlines()[1]
    assert "| Row | Category | Spent | # |" == header_line


# ── structuredContent transport (design: budget-analyst rule 6 needs `data`) ──
# The server returns (rendered-text, {"data": ...}): the text block stays
# byte-identical to the old transport (skills print it verbatim), and the
# structured payload finally reaches the client model so row references
# (txn_id / merchant / cents) resolve without parsing the printed table.
def _rpc(name: str, args: dict):
    from mcp import types

    server = mcp_server.build_server()
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=args))
    return asyncio.run(handler(req)).root


def test_call_tool_carries_rendered_text_and_structured_data(data_dir):
    _seed()
    res = _rpc("get_month_summary", {"month": "2026-06"})
    assert res.isError is not True
    # text block == the handler's rendered markdown, exactly
    direct = asyncio.run(agent_tools.SPEC_BY_NAME["get_month_summary"].handler(
        {"month": "2026-06"}))
    assert res.content[0].text == direct["rendered"]
    # structured payload carries the data dict
    assert res.structuredContent["data"]["spend_total_cents"] == 5000
    # no PII anywhere in the serialized result
    blob = res.model_dump_json()
    assert "RAW-OFX-SECRET" not in blob and "hash-secret-1" not in blob


def test_call_tool_error_payload_still_structured(data_dir):
    _seed()
    res = _rpc("run_sql", {"query": "DELETE FROM transactions"})
    assert res.structuredContent["error"].startswith("read-only")
    assert "read-only" in res.content[0].text


def test_call_tool_unknown_tool_is_clean_error(data_dir):
    _seed()
    res = _rpc("no_such_tool", {})
    blob = res.model_dump_json()
    assert "unknown tool" in blob
