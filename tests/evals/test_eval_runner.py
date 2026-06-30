"""Unit tests for the budget eval runner — mock replay + both cap aborts +
command construction (NO model calls, NO spend)."""
from __future__ import annotations

import json

import eval as runner
import harness as h
import pytest
import specs as eval_specs


# ── fake transcripts (no claude, no DB) ──────────────────────────────────────
def _fake_transcript(tools=(), final="", cost=0.0, results=None):
    return h.Transcript(
        tool_calls=[{"id": f"t{i}", "name": f"mcp__budget__{n}", "input": {}} for i, n in enumerate(tools)],
        tool_results=results or [],
        final_text=final,
        total_cost_usd=cost,
    )


# ── mock replay over the committed stub corpus ───────────────────────────────
def test_mock_green_over_committed_corpus():
    results = runner.run_mock()
    assert results, "stub corpus must produce at least one scenario"
    assert "budget-coach__spend" in results
    assert all(r["passed"] for r in results.values())


def test_mock_fingerprint_strips_toolsearch():
    fp = runner.run_mock()["budget-coach__spend"]["fingerprint"]
    assert "ToolSearch" not in fp["tools"]
    assert fp["tools"] == ["get_category_breakdown"]


def test_mock_empty_corpus_errors(tmp_path):
    with pytest.raises(runner.CorpusError):
        runner.run_mock(transcripts_dir=tmp_path)


def test_mock_unmatched_transcript_errors(tmp_path):
    (tmp_path / "bogus-skill__nope.jsonl").write_text(
        json.dumps({"type": "result", "result": "x"}) + "\n")
    with pytest.raises(runner.CorpusError):
        runner.run_mock(transcripts_dir=tmp_path)


# ── scoring ──────────────────────────────────────────────────────────────────
def test_score_scenario_confirm_gate_granted():
    spec = eval_specs.SPEC_BY_KEY["budget-categorize__granted"]
    t = _fake_transcript(tools=["set_merchant_category"], final="Done — pinned WALMART to Groceries.")
    r = runner.score_scenario(spec, t)
    assert r["passed"] is True
    assert r["checks"]["confirm_gate"] is True
    assert r["checks"]["tool_call"] is True


def test_score_scenario_confirm_gate_ungranted_fails_on_write():
    spec = eval_specs.SPEC_BY_KEY["budget-categorize__ungranted"]
    # A write fired without being granted → confirm_gate fails.
    t = _fake_transcript(tools=["set_merchant_category"], final="pinned it.")
    r = runner.score_scenario(spec, t)
    assert r["checks"]["confirm_gate"] is False
    assert r["passed"] is False


# ── cap aborts (simulated with fake spawn — no spend) ────────────────────────
def _three_specs():
    return eval_specs.SPECS[:3]


def test_max_runs_abort(tmp_path):
    def spawn(spec, eval_db_dir):
        return _fake_transcript(cost=0.0)

    with pytest.raises(runner.CapAbort) as ei:
        runner.run_live(_three_specs(), max_runs=2, max_cost=100.0,
                        eval_db_dir=tmp_path, spawn=spawn)
    assert ei.value.kind == "max_runs"


def test_max_cost_abort(tmp_path):
    # Each run costs $10; after 2 runs total=$20 > $15 cap → abort before run #3.
    def spawn(spec, eval_db_dir):
        return _fake_transcript(cost=10.0)

    with pytest.raises(runner.CapAbort) as ei:
        runner.run_live(_three_specs(), max_runs=30, max_cost=15.0,
                        eval_db_dir=tmp_path, spawn=spawn)
    assert ei.value.kind == "max_cost"


def test_run_live_under_caps_sums_cost(tmp_path):
    def spawn(spec, eval_db_dir):
        return _fake_transcript(tools=["get_category_breakdown"], cost=0.25)

    out = runner.run_live(eval_specs.SPECS[:2], max_runs=30, max_cost=100.0,
                          eval_db_dir=tmp_path, spawn=spawn)
    assert out["total_cost_usd"] == 0.5
    assert len(out["scenarios"]) == 2


def test_run_live_refuses_real_data_dir():
    with pytest.raises(runner.CorpusError):
        runner.run_live(eval_specs.SPECS[:1], max_runs=1, max_cost=1.0,
                        eval_db_dir=runner._REPO_ROOT / "data",
                        spawn=lambda s, d: _fake_transcript())


# ── command + allowlist construction ─────────────────────────────────────────
def test_build_command_carries_verified_flags():
    spec = eval_specs.SPEC_BY_KEY["budget-coach__spend"]
    cmd = runner.build_command(spec)
    assert cmd[0] == "claude" and "-p" in cmd
    for flag in ("--output-format", "stream-json", "--verbose",
                 "--mcp-config", "--strict-mcp-config", "--allowedTools", "--max-turns"):
        assert flag in cmd
    assert "ToolSearch" in cmd


def test_allowlist_excludes_writes_when_ungranted():
    spec = eval_specs.SPEC_BY_KEY["budget-categorize__ungranted"]
    allowed = runner.allowlist_for(spec)
    assert "mcp__budget__set_merchant_category" not in allowed
    assert any(a.startswith("mcp__budget__get_") for a in allowed)
    assert "ToolSearch" in allowed


def test_allowlist_includes_expected_write_when_granted():
    spec = eval_specs.SPEC_BY_KEY["budget-categorize__granted"]
    allowed = runner.allowlist_for(spec)
    assert "mcp__budget__set_merchant_category" in allowed


def test_build_env_sets_absolute_data_dir(tmp_path):
    env = runner.build_env(tmp_path)
    assert env["LOCAL_BUDGET_DATA_DIR"] == str(tmp_path.resolve())


# ── CLI surface ──────────────────────────────────────────────────────────────
def test_cli_mock_default_returns_zero(capsys):
    rc = runner.main([])
    assert rc == 0
    assert "mock replay" in capsys.readouterr().out


def test_cli_rejects_both_live_and_mock(capsys):
    rc = runner.main(["--live", "--mock"])
    assert rc == 2
    assert "at most one" in capsys.readouterr().err


def test_cli_mock_empty_corpus_returns_two(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "TRANSCRIPTS_DIR", tmp_path)
    rc = runner.main([])
    assert rc == 2
    assert "empty mock corpus" in capsys.readouterr().err
