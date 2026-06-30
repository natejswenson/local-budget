"""`budget eval` — the skill behavioral eval runner.

DEFAULT = mock: replay the committed `tests/evals/transcripts/<skill>__<name>.jsonl`
corpus through the pure harness, score each scenario's family_checks, print
PASS/FAIL + a privacy-safe fingerprint. NO model calls, NO spend — this is what
CI / a fresh clone runs. ERRORS if the corpus dir is empty (never vacuously green).

`--live` (opt-in, SPENDS): for each spec, spawn the VERIFIED invocation

    claude -p "<prompt>" \
      --output-format stream-json --verbose \
      --mcp-config .mcp.json --strict-mcp-config \
      --allowedTools <read tools | read+write tools per spec.allow_writes> ToolSearch \
      --max-turns 12

with `LOCAL_BUDGET_DATA_DIR=<ABSOLUTE seeded eval-db dir>` in the child env (never
the real data dir). Two HARD caps, both enforced before each spawn:
  --max-runs N   (default 30) — refuse to spawn past N runs.
  --max-cost USD (default 15) — sum each run's result.total_cost_usd; abort before
                 the next run once the running total exceeds the cap.

Usage:
    uv run python scripts/eval.py [<skill>] [--mock] [--live] \
        [--max-runs 30] [--max-cost 15] [--capture]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Make the sibling eval modules importable as bare top-level modules whether this
# is run as a script or imported by a test.
_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT / "tests" / "evals", _REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import harness as h           # noqa: E402
import specs as eval_specs    # noqa: E402
from eval_seed import DEFAULT_EVAL_DB_DIR, build_eval_db  # noqa: E402
from local_budget.agent.tools import SPEC_BY_NAME  # noqa: E402

TRANSCRIPTS_DIR = _REPO_ROOT / "tests" / "evals" / "transcripts"
RUNS_DIR = _REPO_ROOT / "tests" / "evals" / ".runs"
BASELINE_PATH = _REPO_ROOT / "tests" / "evals" / "baseline.json"
MCP_CONFIG = ".mcp.json"

# Flags the runner DEPENDS on (the startup self-check asserts these are still in
# `claude --help`). `--max-turns` WORKS but is undocumented in v2.1.196, so it is
# NOT asserted here — only the documented, load-bearing flags are.
REQUIRED_FLAGS = ("--verbose", "--output-format", "--allowedTools",
                  "--mcp-config", "--strict-mcp-config")

_WRITE_PREFIXES = ("set_", "add_", "remove_", "clear_")
_WRITE_EXACT = frozenset({"split_subscriptions", "save_brief", "save_user_note", "delete_user_note"})


class CapAbort(RuntimeError):
    """Raised when a hard cap (max-runs or max-cost) would be exceeded."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class CorpusError(RuntimeError):
    """The committed mock corpus is missing/empty or has an unmatched transcript."""


# ── tool classification + allowlist ──────────────────────────────────────────
def _is_write(name: str) -> bool:
    return name.startswith(_WRITE_PREFIXES) or name in _WRITE_EXACT


def _classify_tools() -> tuple[set[str], set[str]]:
    reads, writes = set(), set()
    for n in SPEC_BY_NAME:
        (writes if _is_write(n) else reads).add(n)
    return reads, writes


def allowlist_for(spec) -> list[str]:
    """The `--allowedTools` list for a spec: the full read surface always, the
    spec's expected WRITE tools only when allow_writes, plus ToolSearch (the
    deferred-MCP discovery tool, which must be allowlisted to surface MCP tools)."""
    reads, writes = _classify_tools()
    allowed = set(reads)
    if spec.allow_writes:
        allowed |= (spec.expected_tools & writes)
    names = [f"mcp__budget__{n}" for n in sorted(allowed)]
    return names + [h.TOOLSEARCH]


def build_command(spec, *, mcp_config: str = MCP_CONFIG, max_turns: int = 12) -> list[str]:
    """The VERIFIED `claude -p` command for a spec (read-only vs read+write allowlist)."""
    return [
        "claude", "-p", spec.prompt,
        "--output-format", "stream-json", "--verbose",
        "--mcp-config", mcp_config, "--strict-mcp-config",
        "--allowedTools", *allowlist_for(spec),
        # ISOLATION (privacy): block the filesystem/web builtins so a live eval
        # CANNOT read the operator's real files (memory, ~/.claude, other repos)
        # and bleed real PII into a committed transcript. The skills need only the
        # budget MCP tools + ToolSearch + Skill (skill loading); everything else
        # is denied. (A prior run's Read slurped a real memory file — never again.)
        "--disallowedTools", "Read", "Write", "Edit", "Bash", "Glob", "Grep",
        "WebFetch", "WebSearch", "Task", "NotebookEdit",
        "--max-turns", str(max_turns),
    ]


def build_env(eval_db_dir: Path) -> dict[str, str]:
    """Child env pointing the nested claude/budget-mcp at the ABSOLUTE seeded DB
    dir (paths.py does a bare Path(override) with no resolve, so it MUST be absolute)."""
    env = dict(os.environ)
    env["LOCAL_BUDGET_DATA_DIR"] = str(Path(eval_db_dir).resolve())
    return env


def assert_not_real_data_dir(eval_db_dir: Path) -> None:
    """Refuse to run live against the repo's real `data/` dir."""
    resolved = Path(eval_db_dir).resolve()
    if resolved == (_REPO_ROOT / "data").resolve():
        raise CorpusError("refusing to run live against the real data dir; use the seeded eval DB")


# ── scoring ──────────────────────────────────────────────────────────────────
def score_scenario(spec, transcript) -> dict:
    """Run the spec's family_checks against a parsed transcript."""
    checks: dict[str, bool] = {}
    warnings: list[str] = []
    for fam in spec.family_checks:
        if fam == "tool_call":
            checks[fam] = h.tool_call_ok(transcript, spec.expected_tools)
        elif fam == "no_pii":
            checks[fam] = h.no_pii(transcript)
        elif fam == "confirm_gate":
            checks[fam] = h.confirm_gated(transcript, bool(spec.granted))
        elif fam == "no_write":
            checks[fam] = not h.did_write(transcript)
        elif fam == "structure":
            checks[fam] = h.has_structure(transcript, list(spec.required_sections or []))
        elif fam == "invention":
            rate = h.invention_rate(transcript)
            if rate > 0:
                warnings.append(f"invention_rate={rate:.2f} (advisory)")
        else:  # pragma: no cover - guarded by test_specs
            raise CorpusError(f"unknown family check: {fam}")
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "warnings": warnings,
        "fingerprint": h.fingerprint(transcript),
    }


# ── mock replay ──────────────────────────────────────────────────────────────
def discover_corpus(transcripts_dir: Path) -> dict[str, Path]:
    files = sorted(transcripts_dir.glob("*.jsonl")) if transcripts_dir.exists() else []
    return {p.stem: p for p in files}


def run_mock(skill: str | None = None, *, transcripts_dir: Path | None = None) -> dict:
    """Replay the committed corpus through the harness. Raises CorpusError if the
    corpus is empty (so mock-green is never vacuous) or a transcript matches no spec."""
    transcripts_dir = transcripts_dir or TRANSCRIPTS_DIR
    corpus = discover_corpus(transcripts_dir)
    if not corpus:
        raise CorpusError(
            f"empty mock corpus at {transcripts_dir} — commit at least one "
            "<skill>__<name>.jsonl transcript")

    results = {}
    for key, path in corpus.items():
        spec = eval_specs.SPEC_BY_KEY.get(key)
        if spec is None:
            raise CorpusError(f"transcript {path.name} matches no registered spec")
        if skill and spec.skill != skill:
            continue
        transcript = h.parse_stream_json(path.read_text().splitlines())
        results[key] = score_scenario(spec, transcript)

    if skill and not results:
        raise CorpusError(f"no committed transcripts for skill {skill!r}")
    return results


# ── live spawn (NOT unit-tested — it spends) ─────────────────────────────────
def _spawn_claude(spec, eval_db_dir: Path, *, runs_dir: Path = RUNS_DIR):  # pragma: no cover
    runs_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(spec)
    proc = subprocess.run(
        cmd, env=build_env(eval_db_dir), cwd=str(_REPO_ROOT),
        capture_output=True, text=True, check=False,
    )
    raw = proc.stdout
    (runs_dir / f"{spec.corpus_key}.jsonl").write_text(raw)
    return h.parse_stream_json(raw.splitlines())


def version_self_check() -> str:  # pragma: no cover - touches the claude CLI
    """Record `claude --version` and assert the relied-on flags are still present.
    Aborts loudly if a load-bearing flag is gone (CLI drift)."""
    version = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    help_text = subprocess.run(["claude", "--help"], capture_output=True, text=True, check=False).stdout
    missing = [f for f in REQUIRED_FLAGS if f not in help_text]
    if missing:
        raise CapAbort("flag_drift", f"claude CLI is missing relied-on flags: {missing}")
    return version


def run_live(specs_list, *, max_runs: int, max_cost: float, eval_db_dir: Path,
             spawn=_spawn_claude) -> dict:
    """Drive the live tier with BOTH caps enforced before each spawn. `spawn` is
    injectable so the cap logic is unit-tested without spending."""
    assert_not_real_data_dir(eval_db_dir)
    results = {}
    total_cost = 0.0
    for i, spec in enumerate(specs_list):
        if i >= max_runs:
            raise CapAbort("max_runs", f"--max-runs {max_runs} reached; refusing to spawn run #{i + 1}")
        if total_cost > max_cost:
            raise CapAbort(
                "max_cost",
                f"running total ${total_cost:.2f} exceeds --max-cost ${max_cost:.2f}; "
                f"aborting before run #{i + 1}")
        transcript = spawn(spec, eval_db_dir)
        total_cost += transcript.total_cost_usd
        results[spec.corpus_key] = score_scenario(spec, transcript)
        results[spec.corpus_key]["cost_usd"] = round(transcript.total_cost_usd, 4)
    return {"scenarios": results, "total_cost_usd": round(total_cost, 4)}


# ── reporting / CLI ──────────────────────────────────────────────────────────
def _print_results(results: dict) -> bool:
    all_pass = True
    for key in sorted(results):
        r = results[key]
        status = "PASS" if r["passed"] else "FAIL"
        all_pass = all_pass and r["passed"]
        failed = [c for c, ok in r["checks"].items() if not ok]
        tail = f"  failed={failed}" if failed else ""
        print(f"  [{status}] {key}{tail}  fp={json.dumps(r['fingerprint'])}")
        for w in r["warnings"]:
            print(f"         ⚠ {w}")
    return all_pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="budget eval", description="Skill behavioral eval runner")
    ap.add_argument("skill", nargs="?", help="restrict to one skill")
    ap.add_argument("--live", action="store_true", help="spend: drive claude -p live")
    ap.add_argument("--mock", action="store_true", help="replay the committed corpus (default)")
    ap.add_argument("--max-runs", type=int, default=30)
    ap.add_argument("--max-cost", type=float, default=15.0)
    ap.add_argument("--capture", action="store_true", help="write live fingerprints to baseline.json")
    ap.add_argument("--eval-db-dir", default=str(DEFAULT_EVAL_DB_DIR))
    args = ap.parse_args(argv)

    if args.live and args.mock:
        print("error: pass at most one of --live / --mock (mock is the default)", file=sys.stderr)
        return 2

    if not args.live:
        # MOCK (default) — deterministic, no spend.
        try:
            results = run_mock(args.skill)
        except CorpusError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"mock replay — {len(results)} scenario(s)")
        return 0 if _print_results(results) else 1

    # LIVE — spends. Pre-flight estimate + caps (CLAUDE.md opt-in).
    specs_list = [s for s in eval_specs.SPECS if not args.skill or s.skill == args.skill]
    print(f"LIVE: {len(specs_list)} scenario(s) — est ~2.5-3M tokens ≈ ~$10-25 metered, ~15 min")
    print(f"caps: --max-runs {args.max_runs}  --max-cost ${args.max_cost:.2f}")
    try:
        version = version_self_check()
        print(f"claude {version}")
        eval_db_dir = build_eval_db(args.eval_db_dir)
        out = run_live(specs_list, max_runs=args.max_runs, max_cost=args.max_cost,
                       eval_db_dir=eval_db_dir.parent)
    except (CapAbort, CorpusError) as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 2

    all_pass = _print_results(out["scenarios"])
    print(f"total live cost: ${out['total_cost_usd']:.2f}")
    if args.capture:
        fps = {k: v["fingerprint"] for k, v in out["scenarios"].items()}
        BASELINE_PATH.write_text(json.dumps({"scenarios": fps}, indent=2, sort_keys=True))
        print(f"captured baseline → {BASELINE_PATH}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
