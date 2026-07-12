"""Filesystem paths and at-rest permissions (design §7.4 — NET-NEW hardening).

The durable control is the DIRECTORY: `data/` is created 0700 (owner-only) so a
group/other-readable parent can never expose the DBs. Both `.db` files AND their
`-wal`/`-shm` sidecars (WAL mode creates them with the process umask, not 600)
plus the `local_key` are chmod'd 0600. A restrictive umask is set before any
connect() to shrink the connect-then-chmod TOCTOU window.

`LOCAL_BUDGET_DATA_DIR` overrides the data directory (used by tests for a
hermetic temp dir, mirroring local-fitness).
"""
from __future__ import annotations

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

DIR_MODE = 0o700
FILE_MODE = 0o600


def data_dir() -> Path:
    """Resolve and create (0700) the data directory."""
    override = os.environ.get("LOCAL_BUDGET_DATA_DIR")
    base = Path(override) if override else (_PROJECT_ROOT / "data")
    os.umask(0o077)  # shrink the connect-then-chmod TOCTOU window
    base.mkdir(parents=True, exist_ok=True)
    _chmod(base, DIR_MODE)
    return base


def budget_db_path() -> Path:
    """Full-PII database — the agent NEVER opens this."""
    return data_dir() / "budget.db"


def local_key_path() -> Path:
    """0600 file holding the HMAC key for acct_hash (design §3/M1)."""
    return data_dir() / "local_key"


def briefings_dir() -> Path:
    """Monthly briefings — cleartext spend summaries, under the 0700 regime.

    `LOCAL_BUDGET_BRIEFINGS_DIR` overrides the location (mirrors `LOCAL_BUDGET_DATA_DIR`).
    Without it the default `data_dir().parent/"briefings"` resolves to an UNMOUNTED,
    read-only path inside the container (data_dir is /data → /briefings), so the
    container sets this to /data/briefings, under the writable bind mount. [design CORR-1]
    """
    override = os.environ.get("LOCAL_BUDGET_BRIEFINGS_DIR")
    d = Path(override) if override else (data_dir().parent / "briefings")
    d.mkdir(parents=True, exist_ok=True)
    _chmod(d, DIR_MODE)
    return d


def reports_dir() -> Path:
    """Rendered visual-report PDFs — full monthly financials, so the same 0700
    regime as data/ and briefings/ (siege S3: the old skill-prose path wrote
    0644 files into a 0755 dir). `LOCAL_BUDGET_REPORTS_DIR` overrides the
    location; the default is the same `reports/` the prose path used, so
    nothing moves for the user."""
    override = os.environ.get("LOCAL_BUDGET_REPORTS_DIR")
    d = Path(override) if override else (data_dir().parent / "reports")
    d.mkdir(parents=True, exist_ok=True)
    _chmod(d, DIR_MODE)
    return d


def user_notes_path() -> Path:
    """Non-financial user-preference notes (the only agent write path — M2)."""
    return data_dir() / "user_notes.md"


def default_inbox_dir() -> Path:
    """Dedicated drop-folder for bank statement exports (NOT ~/Downloads — red-team
    F5). The user can override via the `inbox_dir` setting."""
    return Path.home() / "budget-inbox"


def intake_lock_path() -> Path:
    """Lockfile for the single-intake mutex (flock; auto-released on crash)."""
    return data_dir() / ".intake.lock"


def harden_db_files(db_path: Path) -> None:
    """chmod 0600 a .db file and its -wal/-shm sidecars (design §7.4)."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            _chmod(p, FILE_MODE)


def _chmod(p: Path, mode: int) -> None:
    try:
        p.chmod(mode)
    except OSError:
        # Best-effort on filesystems that don't support POSIX perms.
        pass
