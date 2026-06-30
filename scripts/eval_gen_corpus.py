"""Generate the committed mock-eval corpus as FABRICATED, PII-free transcripts.

The committed `tests/evals/transcripts/<skill>__<name>.jsonl` corpus is the
DETERMINISTIC (CI) tier — it must contain zero personal data. Rather than commit
raw `claude -p` session dumps (which embed the operator's environment AND can
bleed real context — a prior live run's transcript slurped a real memory file),
this synthesizes one minimal, spec-faithful transcript per scenario from purely
fabricated data. The transcript exercises the harness's family-check scoring
end-to-end; the LIVE tier (`scripts/eval.py --live`, now isolated via
`--disallowedTools`) is the real behavioral check. Re-run after editing specs:
    uv run python scripts/eval_gen_corpus.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))
from evals.specs import SPECS  # noqa: E402

_OUT = _REPO / "tests" / "evals" / "transcripts"


def _rendered(tool: str) -> str:
    # Generic, fabricated rendered block — no real merchants/amounts/PII.
    return (f"## {tool}\n| Category | Spent |\n| --- | ---: |\n"
            "| Groceries | $42.00 |\n| Dining Out | $18.50 |")


def _final_text(spec) -> str:
    parts: list[str] = []
    if spec.required_sections:
        parts += [f"## {s}" for s in spec.required_sections]
        parts.append("Groceries and dining were the biggest categories — all figures from the tools above.")
    if "confirm_gate" in spec.family_checks and spec.granted is False:
        parts.append("Want me to make that change? Say the word and I'll apply it.")
    elif spec.granted is True:
        parts.append("Done — I've applied that change.")
    else:
        parts.append("Here's what your budget data shows, pulled straight from the tools.")
    return "\n".join(parts)


def _transcript(spec) -> str:
    lines: list[dict] = []
    for i, tool in enumerate(sorted(spec.expected_tools)):
        tid = f"t{i}"
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__budget__{tool}", "input": {"month": "2026-06"}}]}})
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": _rendered(tool), "is_error": False}]}})
    lines.append({"type": "result", "subtype": "success", "result": _final_text(spec), "is_error": False})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def main() -> int:
    _OUT.mkdir(parents=True, exist_ok=True)
    for spec in SPECS:
        (_OUT / f"{spec.skill}__{spec.name}.jsonl").write_text(_transcript(spec))
    print(f"generated {len(SPECS)} fabricated transcripts under {_OUT.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
