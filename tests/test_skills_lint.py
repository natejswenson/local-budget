"""Deterministic skill-lint over the no-code budget SKILL.md files (Phase 4,
Task 5). No model calls. Asserts each skill has YAML frontmatter (name +
description), embeds no executable code fence, lists only tools that exist in
``SPEC_BY_NAME`` (and never ``run_sql``), and — when it can write — carries a
confirm-gate phrase. ``yaml`` is NOT a project dependency, so the frontmatter is
sliced and string-parsed, never ``import yaml``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from local_budget.agent.tools import SPEC_BY_NAME

# Repo root resolved relative to THIS file (tests/ -> repo root), not cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

# Write tools: a manifest containing any of these means the skill can write.
WRITE_PREFIXES = ("set_", "add_", "remove_", "clear_")
WRITE_EXACT = {"split_subscriptions", "save_brief"}

EXPECTED_SKILL_DIRS = [
    "budget-setup",
    "budget-coach",
    "budget-monthly-brief",
    "budget-categorize",
    "budget-budgets",
    "budget-income",
    "budget-subscriptions",
    "budget-reconcile",
]

# Executable fenced-block languages that are forbidden (no-code skills).
EXEC_FENCE = re.compile(r"^```(python|bash|sh)\b", re.MULTILINE | re.IGNORECASE)

CONFIRM_PHRASES = ("confirm", "before writing", "get a yes", "your yes")


def _budget_skill_files():
    return sorted(SKILLS_DIR.glob("budget-*/SKILL.md"))


def _split_frontmatter(text: str) -> str:
    """Return the YAML frontmatter block between the leading ``---`` fences."""
    assert text.startswith("---"), "SKILL.md must start with a --- frontmatter fence"
    parts = text.split("---", 2)
    # parts[0] is '' (before first ---), parts[1] is the frontmatter, parts[2] body.
    assert len(parts) >= 3, "SKILL.md frontmatter must be closed by a second ---"
    return parts[1]


def _manifest_tools(frontmatter: str) -> list[str]:
    """Parse the inline ``tools: [a, b, c]`` list; drop blank tokens (so the
    persona's ``tools: []`` yields [] and not ['')."""
    tools: list[str] = []
    for line in frontmatter.splitlines():
        if line.strip().startswith("tools:"):
            inside = line.split("tools:", 1)[1].strip()
            inside = inside.strip("[]")
            tools = [t.strip() for t in inside.split(",") if t.strip()]
            break
    return tools


def test_budget_skill_files_found():
    assert _budget_skill_files(), "no budget-*/SKILL.md files found"


def test_expected_skill_dirs_exist():
    for name in EXPECTED_SKILL_DIRS + ["budget-analyst"]:
        assert (SKILLS_DIR / name / "SKILL.md").is_file(), f"missing skill: {name}"


@pytest.mark.parametrize(
    "path", _budget_skill_files(), ids=lambda p: p.parent.name
)
def test_skill_lint(path: Path):
    text = path.read_text()
    frontmatter = _split_frontmatter(text)

    # 1. frontmatter has name + description
    assert re.search(r"^name:\s*\S+", frontmatter, re.MULTILINE), f"{path}: no name:"
    assert re.search(
        r"^description:\s*\S+", frontmatter, re.MULTILINE
    ), f"{path}: no description:"

    # 2. no executable code fence
    assert not EXEC_FENCE.search(text), f"{path}: executable code fence forbidden"

    # 3. every manifest tool exists in SPEC_BY_NAME; never run_sql
    tools = _manifest_tools(frontmatter)
    for tool in tools:
        assert tool in SPEC_BY_NAME, f"{path}: unknown tool {tool!r}"
    assert "run_sql" not in tools, f"{path}: run_sql must not appear in a manifest"

    # 4. write-capable skills carry a confirm-gate phrase
    has_write = any(
        t in WRITE_EXACT or t.startswith(WRITE_PREFIXES) for t in tools
    )
    if has_write:
        body = text.lower()
        assert any(
            phrase in body for phrase in CONFIRM_PHRASES
        ), f"{path}: write skill lacks a confirm-gate phrase"
