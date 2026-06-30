"""User-preference notes — the agent's ONLY filesystem write path (design §5/M2).

Writes plain TEXT to a fixed, non-financial file `data/user_notes.md`. Never a
database, never agent-readable as SQL, fixed path (not redirectable). No
financial data, no ledger access.
"""
from __future__ import annotations

from . import paths

_MAX_LEN = 280


def _lines() -> list[str]:
    p = paths.user_notes_path()
    if not p.exists():
        return []
    return [ln.rstrip("\n") for ln in p.read_text().splitlines() if ln.strip()]


def _write(lines: list[str]) -> None:
    p = paths.user_notes_path()
    p.write_text("\n".join(lines) + ("\n" if lines else ""))
    paths._chmod(p, paths.FILE_MODE)


def append_note(text: str) -> dict:
    text = " ".join(text.split())[:_MAX_LEN]
    lines = _lines()
    lines.append(text)
    _write(lines)
    return {"line": len(lines) - 1, "text": text}


def read_notes() -> list[dict]:
    return [{"line": i, "text": t} for i, t in enumerate(_lines())]


def delete_note(line: int) -> bool:
    lines = _lines()
    if not (0 <= line < len(lines)):
        return False
    del lines[line]
    _write(lines)
    return True
