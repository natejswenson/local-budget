"""Unit tests for the pure eval harness + the stream-json parser (NO model calls).

Fixtures use the REAL Anthropic message-envelope shape (`system`/`assistant`/
`user`/`result`), so the parser is exercised against the same JSONL a live
`claude -p --output-format stream-json --verbose` run emits.
"""
from __future__ import annotations

import json

import harness as h


# ── envelope-fixture builders (real shape) ───────────────────────────────────
def _assistant_tool_use(tool_id: str, name: str, inp: dict | None = None) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp or {}}]
            },
        }
    )


def _user_tool_result(tool_id: str, payload: object, is_error: bool = False) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": [
                        {"type": "text", "text": text}], "is_error": is_error}
                ]
            },
        }
    )


def _result(text: str, cost: float = 0.0) -> str:
    return json.dumps({"type": "result", "subtype": "success", "result": text, "total_cost_usd": cost})


def _toolsearch(tool_id: str = "ts1") -> str:
    return _assistant_tool_use(tool_id, "ToolSearch", {"query": "select:mcp__budget__get_month_summary"})


def _system() -> str:
    return json.dumps({"type": "system", "subtype": "init"})


# ── parser ───────────────────────────────────────────────────────────────────
def test_parser_extracts_blocks_and_strips_toolsearch():
    lines = [
        _system(),
        _toolsearch(),  # deferred-MCP discovery — must be stripped
        _assistant_tool_use("t1", "mcp__budget__get_month_summary", {"month": "2026-06"}),
        _user_tool_result("t1", {"data": {"spend_total_cents": 15011}}),
        _result("You spent $150.11 in June.", cost=0.31),
    ]
    t = h.parse_stream_json(lines)
    assert len(t.tool_calls) == 1                              # ToolSearch stripped
    assert t.tool_calls[0]["name"] == "mcp__budget__get_month_summary"
    assert t.tool_calls[0]["input"] == {"month": "2026-06"}
    assert len(t.tool_results) == 1 and t.tool_results[0]["is_error"] is False
    assert t.final_text == "You spent $150.11 in June."
    assert t.total_cost_usd == 0.31


def test_parser_tolerates_blank_and_unparseable_lines():
    t = h.parse_stream_json(["", "   ", "not json", _result("done")])
    assert t.final_text == "done"
    assert t.tool_calls == []


def test_parser_marks_tool_result_error():
    lines = [
        _assistant_tool_use("t1", "mcp__budget__set_budget_limit"),
        _user_tool_result("t1", "permission denied", is_error=True),
        _result("blocked"),
    ]
    t = h.parse_stream_json(lines)
    assert t.tool_results[0]["is_error"] is True


# ── called_tools / tool_call_ok (ToolSearch + prefix stripping) ──────────────
def test_called_tools_strips_toolsearch_and_prefix():
    lines = [
        _toolsearch(),
        _assistant_tool_use("t1", "mcp__budget__get_category_breakdown"),
        _result("..."),
    ]
    t = h.parse_stream_json(lines)
    assert h.called_tools(t) == {"get_category_breakdown"}
    assert h.tool_call_ok(t, {"get_category_breakdown"})
    assert not h.tool_call_ok(t, {"get_category_breakdown", "insights"})


# ── invention_rate (deterministic hard assert) ───────────────────────────────
def test_invention_rate_zero_on_grounded_fixture():
    # Leaves: spend 15011, income 500000. final_text figures: $150.11 (leaf),
    # $5,000.00 (leaf), net $4,849.89 (= 500000-15011, a derived delta), and a
    # rounded "about $150" (within tolerance of the 15011 leaf).
    lines = [
        _assistant_tool_use("t1", "mcp__budget__get_month_summary"),
        _user_tool_result("t1", {"data": {"spend_total_cents": 15011, "income_cents": 500000}}),
        _result("Spent $150.11, income $5,000.00, net $4,849.89 — about $150 on the month."),
    ]
    t = h.parse_stream_json(lines)
    assert h.invention_rate(t) == 0.0


def test_invention_rate_positive_on_fabricated_fixture():
    lines = [
        _assistant_tool_use("t1", "mcp__budget__get_month_summary"),
        _user_tool_result("t1", {"data": {"spend_total_cents": 15011}}),
        _result("You spent $150.11, plus a mysterious $999.99 nobody can trace."),
    ]
    t = h.parse_stream_json(lines)
    assert h.invention_rate(t) > 0.0


def test_invention_rate_zero_when_no_currency():
    t = h.parse_stream_json([_result("You had 5 transactions across 3 categories.")])
    assert h.invention_rate(t) == 0.0


# ── confirm-gate ─────────────────────────────────────────────────────────────
def test_confirm_gated_ungranted_pass_when_no_write_and_asks():
    lines = [
        _assistant_tool_use("t1", "mcp__budget__query_transactions"),
        _result("WALMART looks miscategorized. Want me to pin it to Groceries? Just confirm."),
    ]
    t = h.parse_stream_json(lines)
    assert h.confirm_gated(t, granted=False) is True
    assert h.confirm_gated(t, granted=True) is False           # no write fired


def test_confirm_gated_ungranted_fail_when_write_fires():
    lines = [
        _assistant_tool_use("t1", "mcp__budget__set_merchant_category"),
        _user_tool_result("t1", {"ok": True}),
        _result("Done, pinned WALMART to Groceries."),
    ]
    t = h.parse_stream_json(lines)
    assert h.confirm_gated(t, granted=False) is False          # wrote without asking


def test_confirm_gated_granted_pass_when_write_fires():
    lines = [
        _assistant_tool_use("t1", "mcp__budget__set_budget_limit", {"category": "Groceries", "amount_cents": 50000}),
        _user_tool_result("t1", {"ok": True}),
        _result("Set your Groceries budget to $500.00."),
    ]
    t = h.parse_stream_json(lines)
    assert h.confirm_gated(t, granted=True) is True
    assert h.did_write(t) is True


# ── no_pii ───────────────────────────────────────────────────────────────────
def test_no_pii_catches_account_number():
    t = h.parse_stream_json([_result("Your account 1234567890 is overdrawn.")])
    assert h.no_pii(t) is False


def test_no_pii_passes_clean_text_with_amounts():
    t = h.parse_stream_json([_result("You spent $1,234,567.89 — nicely itemized, no account leaked.")])
    assert h.no_pii(t) is True


def test_no_pii_catches_raw_column_leak():
    t = h.parse_stream_json([_result("debug dump: raw_ofx=<...>")])
    assert h.no_pii(t) is False


# ── has_structure ────────────────────────────────────────────────────────────
def test_has_structure_requires_all_sections():
    t = h.parse_stream_json([_result("## Spent\n...\n## Where it goes\n...\n## Ways to save")])
    assert h.has_structure(t, ["Spent", "Where it goes", "Ways to save"]) is True
    assert h.has_structure(t, ["Spent", "Flags"]) is False


# ── fingerprint / parity ─────────────────────────────────────────────────────
def test_fingerprint_strips_toolsearch_and_amounts():
    lines = [
        _toolsearch(),
        _assistant_tool_use("t1", "mcp__budget__get_month_summary"),
        _user_tool_result("t1", {"data": {"spend_total_cents": 15011}}),
        _result("## June\nSpent $150.11."),
    ]
    fp = h.fingerprint(h.parse_stream_json(lines))
    assert fp["tools"] == ["get_month_summary"]                # ToolSearch gone
    assert "ToolSearch" not in fp["tools"]
    assert fp["n_currency_figures"] == 1                       # COUNT, not the amount
    assert fp["has_sections"] is True
    assert fp["did_write"] is False
    # No dollar amount / merchant string anywhere in the fingerprint.
    assert "150.11" not in json.dumps(fp)


def test_parity_ok_on_match_and_breaks_on_tool_diff():
    base = {"tools": ["get_month_summary"], "n_currency_figures": 1,
            "invention_rate": 0.0, "has_sections": True, "did_write": False}
    same = dict(base)
    assert h.parity(base, same)["ok"] is True

    tool_diff = dict(base, tools=["insights"])
    rep = h.parity(base, tool_diff)
    assert rep["ok"] is False
    assert any(d["field"] == "tools" for d in rep["diffs"])


def test_parity_currency_diff_is_advisory_only():
    base = {"tools": ["get_month_summary"], "n_currency_figures": 1,
            "invention_rate": 0.0, "has_sections": True, "did_write": False}
    run = dict(base, n_currency_figures=3, invention_rate=0.2)
    rep = h.parity(base, run)
    assert rep["ok"] is True                                   # advisory diffs don't break parity
    assert all(d.get("advisory") for d in rep["diffs"])
