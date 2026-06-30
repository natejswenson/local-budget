"""Masked-DB backup with a TRUE allowlist of destinations (design §7.11/S2/M3).

Backs up budget.db (already masked — full account numbers were never stored).
`--out` is permitted ONLY if it resolves under data/ or an explicitly
configured `backup_root`; every other target is REFUSED (default-deny), so a
backup can never silently land in a cloud-synced folder. Output is chmod 0600.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from . import db, paths


class BackupError(ValueError):
    pass


def _allowed_roots() -> list[Path]:
    roots = [paths.data_dir().resolve()]
    configured = db.get_setting("backup_root")
    if configured:
        roots.append(Path(configured).resolve())
    return roots


def _check_allowed(target: Path) -> Path:
    resolved = target.resolve()
    for root in _allowed_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise BackupError(
        f"refused: {target} is not under data/ or a configured backup_root. "
        f"Set one with `budget config set backup_root <abs-path>`."
    )


def backup(out: str | None = None) -> Path:
    if out is None:
        dest = paths.data_dir() / f"budget-backup-{date.today().isoformat()}.db"
    else:
        dest = _check_allowed(Path(out))
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Online backup -> a clean, checkpointed copy (no -wal/-shm needed).
    src = sqlite3.connect(db.get_db_path())
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    paths._chmod(dest, paths.FILE_MODE)
    return dest
