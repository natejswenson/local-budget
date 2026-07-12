"""Build a known, FABRICATED eval DB for the live behavioral evals.

The live tier points the nested `claude -p` / `budget-mcp` child at THIS db via
`LOCAL_BUDGET_DATA_DIR=<absolute dir>` so scenarios are deterministic and
PII-free — NEVER the user's real `data/budget.db` (full PII, non-deterministic)
and NEVER the empty worktree DB (no tables → "no such table: transactions").

All values here are invented (the seeded-DB universe). Money is integer cents;
spend rows are stored as NEGATIVE `amount_cents` (income positive), matching the
deterministic core's convention. The dir MUST be absolute — `paths.py` does a
bare `Path(override)` with no `.resolve()`, so a relative value would bind to the
child process cwd, not the runner's.

Usage:
    uv run python scripts/eval_seed.py [<dir>]     # default: tests/evals/.evaldb
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVAL_DB_DIR = _REPO_ROOT / "tests" / "evals" / ".evaldb"

# Fixed fabricated month for every scenario (CLAUDE.md currentDate is 2026-06).
EVAL_MONTH = "2026-06"

# (fitid, posted_date, amount_cents, payee, merchant_norm, category, subcategory)
# Spend is negative; income positive. Two+ spend categories + income + a sub.
_TXNS = [
    ("E001", "2026-06-01", -1599, "NETFLIX.COM", "NETFLIX", "Subscriptions", "Netflix"),
    # An UNSPLIT subscription (blank subcategory) so split_subscriptions has work.
    ("E007", "2026-06-05", -999, "SPOTIFY USA", "SPOTIFY", "Subscriptions", None),
    ("E002", "2026-06-03", -8500, "WALMART GROCERY", "WALMART", "Groceries", None),
    ("E003", "2026-06-10", -4212, "TRADER JOES", "TRADER JOES", "Groceries", None),
    ("E004", "2026-06-12", -1899, "CHIPOTLE", "CHIPOTLE", "Dining", None),
    ("E005", "2026-06-20", -4500, "SHELL OIL", "SHELL", "Transportation", None),
    ("E006", "2026-06-15", 500000, "PAYROLL DEPOSIT", "PAYROLL", "Income", None),
]

# A single Groceries budget so over/under-budget scenarios have data ($500.00).
_GROCERIES_LIMIT_CENTS = 50000


def _seed_rows(conn) -> None:
    from local_budget import db

    now = db.now_iso()
    conn.execute(
        "INSERT INTO accounts (account_id, institution, acct_type, acct_last4, "
        "acct_hash, own_account, nickname, created_at) "
        "VALUES (1, 'Example Bank', 'checking', '0000', 'evalhash', 1, 'Eval', ?)",
        (now,),
    )
    for fitid, date, cents, payee, merch, cat, sub in _TXNS:
        conn.execute(
            "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, "
            "status, txn_type, payee, merchant_norm, category, subcategory, "
            "category_source, imported_at) "
            "VALUES (1, ?, ?, ?, 'posted', ?, ?, ?, ?, ?, 'seed', ?)",
            (fitid, date, cents, "credit" if cents > 0 else "debit",
             payee, merch, cat, sub, now),
        )
    conn.execute(
        "INSERT INTO budgets (category, subcategory, limit_cents, effective_from, created_at) "
        "VALUES ('Groceries', NULL, ?, '2026-01-01', ?)",
        (_GROCERIES_LIMIT_CENTS, now),
    )
    # One OPEN import conflict so budget-reconcile has something to find.
    conn.execute(
        "INSERT INTO import_conflicts (account_id, kind, fitid, existing_amount_cents, "
        "existing_posted_date, incoming_amount_cents, incoming_posted_date, "
        "incoming_payee, detected_at, resolved) "
        "VALUES (1, 'amount_mismatch', 'E002', -8500, '2026-06-03', -8900, "
        "'2026-06-03', 'WALMART GROCERY', ?, 0)",
        (now,),
    )
    # Expected monthly income setting so income/setup scenarios have a baseline.
    db.set_setting("expected_income_cents", "500000", conn=conn)


def build_eval_db(target_dir: str | os.PathLike[str] | None = None) -> Path:
    """Build the seeded eval DB at an ABSOLUTE dir; return the db path.

    Idempotent: an existing eval DB is removed and rebuilt so the fixture is fixed.
    Sets `LOCAL_BUDGET_DATA_DIR` to the resolved absolute dir for the duration of
    the build (the runner re-sets it per child spawn).
    """
    import shutil

    d = Path(target_dir or DEFAULT_EVAL_DB_DIR).resolve()
    shutil.rmtree(d, ignore_errors=True)   # wipe wholesale so a re-seed is always fresh
    d.mkdir(parents=True, exist_ok=True)
    os.environ["LOCAL_BUDGET_DATA_DIR"] = str(d)

    from local_budget import db

    db_path = db.get_db_path()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    db.init_schema()
    with db.connect() as conn:
        _seed_rows(conn)
    return db_path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target = argv[0] if argv else None
    path = build_eval_db(target)
    print(f"seeded eval DB → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
