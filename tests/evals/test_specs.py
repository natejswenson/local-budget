"""Every eval spec references only REAL registered budget tools (NO model calls)."""
from __future__ import annotations

import specs
from local_budget.agent.tools import SPEC_BY_NAME

_REGISTERED = set(SPEC_BY_NAME)
_VALID_FAMILIES = {"tool_call", "no_pii", "confirm_gate", "no_write", "structure", "invention"}


def test_every_expected_tool_is_registered():
    for s in specs.SPECS:
        missing = s.expected_tools - _REGISTERED
        assert not missing, f"{s.corpus_key}: unknown tools {missing}"


def test_scenario_count_within_max_runs_cap():
    # ~16-24 scenarios, comfortably under the default --max-runs 30 cap.
    assert 16 <= len(specs.SPECS) <= 24


def test_corpus_keys_unique():
    keys = [s.corpus_key for s in specs.SPECS]
    assert len(keys) == len(set(keys))


def test_family_checks_are_known():
    for s in specs.SPECS:
        unknown = set(s.family_checks) - _VALID_FAMILIES
        assert not unknown, f"{s.corpus_key}: unknown family checks {unknown}"


def test_confirm_gate_pairs_are_well_formed():
    for s in specs.SPECS:
        if "confirm_gate" not in s.family_checks:
            continue
        assert s.granted is not None, f"{s.corpus_key}: confirm_gate needs granted set"
        if s.granted:
            # Granted scenarios allow writes and expect a write tool.
            assert s.allow_writes is True
            assert any(
                t.startswith(("set_", "add_", "remove_", "clear_"))
                or t in {"split_subscriptions", "save_brief", "save_user_note", "delete_user_note"}
                for t in s.expected_tools
            ), f"{s.corpus_key}: granted scenario should expect a write tool"
        else:
            assert s.allow_writes is False


def test_structure_specs_have_sections():
    for s in specs.SPECS:
        if "structure" in s.family_checks:
            assert s.required_sections, f"{s.corpus_key}: structure check needs required_sections"


def test_covers_eight_skills():
    assert len(specs.skills()) == 8
