"""Drop-folder adapter (red-team-approved intake design).

Owns everything PHYSICAL about a file source: where the inbox is, which files are
complete + recognized bank statement exports, the single-intake mutex, and safe
disposal to `processed/` (never Trash, never shell-out, paths confined). The
agnostic intake core (importer / `intake.py`) operates on what this hands it.

Safety properties enforced here:
- Never ingest an in-progress / truncated / unrecognized file (integrity + format validation).
- Never reprocess an already-seen file (tracked by CONTENT HASH).
- Single intake at a time (flock; auto-released on crash).
- Dispose only AFTER a verified import, and only this-run's files; paths are
  realpath-confined to the inbox; disposal is `os.rename`, never a shelled command.
- Filenames/raw rows never cross to the browser — only sanitized enum reasons.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from pathlib import Path

from . import db, paths
from .ingest import parse

SUPPORTED = (".ofx", ".qfx", ".qbo", ".csv")
IN_PROGRESS = (".crdownload", ".part", ".download", ".tmp")
STABILITY_SECS = 3          # a file must be untouched this long before we read it
MAX_FILE_BYTES = 64 * 1024 * 1024   # 64 MB cap (DoS guard, red-team S6)
MAX_INTAKE_ATTEMPTS = 3     # failed-import retries before a file is quarantined (S2)

# Sanitized quarantine reasons — the ONLY thing about a bad file that may reach
# the browser/logs (never the filename or raw rows; red-team S6/M3).
NOT_STATEMENT = "not_recognized_statement"
MALFORMED = "malformed_row"
TRUNCATED = "truncated_file"
TOO_BIG = "file_too_large"


class UploadError(ValueError):
    """Rejected dashboard upload (bad type / size / path) — message is safe to show."""
REPEATED_ERROR = "repeated_import_error"   # transient error hit the retry cap (S2)


def inbox_dir() -> Path:
    raw = db.get_setting("inbox_dir")
    d = Path(raw).expanduser() if raw else paths.default_inbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def processed_dir() -> Path:
    d = inbox_dir() / "processed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _confined(p: Path) -> bool:
    """True iff `p` resolves to inside the inbox dir (no traversal/symlink escape)."""
    try:
        root = inbox_dir().resolve()
        return p.resolve() == root or root in p.resolve().parents
    except OSError:
        return False


def content_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── integrity gating (before any parse) ──────────────────────────────────────
def is_stable(p: Path, now: float | None = None) -> bool:
    """A file is stable (download/copy finished) if it has a supported extension,
    is non-empty, under the size cap, and hasn't been modified very recently."""
    name = p.name.lower()
    if p.suffix.lower() not in SUPPORTED or any(name.endswith(x) for x in IN_PROGRESS):
        return False
    try:
        st = p.stat()
    except OSError:
        return False
    if st.st_size == 0 or st.st_size > MAX_FILE_BYTES:
        return False
    now = time.time() if now is None else now
    return (now - st.st_mtime) >= STABILITY_SECS


def validate_export(p: Path) -> tuple[bool, str | None]:
    """Positive statement-format validation — we never ingest-on-guess (red-team F4/F5).
    Returns (ok, sanitized_reason_or_None). Surfaces only an enum reason, never
    raw content."""
    try:
        if p.stat().st_size > MAX_FILE_BYTES:
            return False, TOO_BIG
        if p.suffix.lower() == ".csv":
            return _validate_csv(p)
        return _validate_ofx(p)
    except parse.ParseError:
        return False, NOT_STATEMENT
    except (OSError, UnicodeDecodeError):
        return False, MALFORMED


def _validate_csv(p: Path) -> tuple[bool, str | None]:
    # A missing trailing newline is NOT truncation (red-team S2): a complete bank
    # export commonly ends without "\n". Truncation is detected by the FINAL row
    # failing the statement shape (the per-row loop below validates EVERY row), not by
    # the absence of a trailing newline — which would false-quarantine a complete
    # file and silently never import it.
    import csv
    text = p.read_text(errors="strict")
    rows = [r for r in csv.reader(text.splitlines()) if any(c.strip() for c in r)]
    if not rows:
        return False, NOT_STATEMENT
    from .money import AmountParseError, cents_from_amount_str
    from .ingest.parse import _iso_date
    for r in rows:                        # every row must match the statement shape
        if len(r) < 5:
            return False, NOT_STATEMENT
        try:
            _iso_date(r[0].strip())
            cents_from_amount_str(r[1].strip().replace("$", ""))
        except (parse.ParseError, AmountParseError):
            return False, NOT_STATEMENT
    return True, None


def _validate_ofx(p: Path) -> tuple[bool, str | None]:
    text = p.read_text(errors="ignore").upper()
    if "<OFX>" not in text or "</OFX>" not in text:   # close tag absent → truncated
        return False, (TRUNCATED if "<OFX>" in text else NOT_STATEMENT)
    # An OFX statement is recognized by its structural tags + an intact close tag
    # (not truncated). <FITID> presence MUST NOT gate acceptance (red-team S1): the
    # parser now recovers FITID-less <STMTTRN> rows with a synthetic content FITID,
    # so an all-FITID-less OFX is fully importable. Requiring <FITID> here would
    # quarantine it on the intake/drop-folder path while direct `budget import`
    # imports it fine — an inconsistency that silently loses that spend.
    if "<STMTRS>" not in text or "<STMTTRN>" not in text:
        return False, NOT_STATEMENT
    return True, None


# ── single-intake mutex (flock; auto-released on crash) ──────────────────────
@contextlib.contextmanager
def intake_lock(blocking: bool = False):
    """Serialize the whole scan→ingest→finish sequence. Yields True if acquired,
    False if another intake holds it (when non-blocking). The OS releases a flock
    on process death, so a crash never wedges intake (red-team S4)."""
    fd = os.open(paths.intake_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError:
        os.close(fd)
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── seen-file tracking + disposal ────────────────────────────────────────────
def already_seen(h: str) -> bool:
    """True iff this content hash is recorded AND should not be reprocessed.

    A file recorded as 'errored' with attempts BELOW the cap is treated as
    NOT-seen so a TRANSIENT failure self-heals on the next run (red-team S2). Once
    attempts reach the cap the row is rewritten as 'quarantined' (see
    record_error), which IS seen — so it stops reprocessing."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT state, attempts FROM inbox_files WHERE content_hash = ?", (h,)
        ).fetchone()
    if row is None:
        return False
    if row["state"] == "errored" and row["attempts"] < MAX_INTAKE_ATTEMPTS:
        return False   # transient error → retry on the next scan
    return True


def record_seen(h: str, filename: str, state: str, run_id: int | None, reason: str | None,
                conn=None) -> None:  # noqa: ANN001
    # A successful/terminal record resets attempts to 0 (a retry that imports
    # clears the transient-error state, red-team S2).
    #
    # `conn` (default None): when provided, the write reuses the CALLER's open
    # connection/transaction instead of opening its own — so the seen-record
    # commits ATOMICALLY with the rows it describes (red-team F7-1). This is how
    # import_file persists the 'imported' record inside the same budget.db
    # transaction that holds the file's transactions: on rollback NEITHER
    # persists, so the file stays unseen and is correctly retried, and the
    # seen-record's run_id always equals the batch that owns the rows (no crash
    # window, no reassignment to a competing 0-row batch). With conn=None it
    # keeps its standalone behavior (own connection) for the quarantined/errored
    # callers that don't import rows.
    sql = ("INSERT OR REPLACE INTO inbox_files "
           "(content_hash, filename, state, reason, run_id, disposed, attempts, recorded_at) "
           "VALUES (?,?,?,?,?,0,0,?)")
    params = (h, filename, state, reason, run_id, db.now_iso())
    if conn is not None:
        conn.execute(sql, params)
        return
    with db.connect() as c:
        c.execute(sql, params)


def record_error(h: str, filename: str, run_id: int | None) -> int:
    """Record a failed import attempt and return the new attempt count. While
    below MAX_INTAKE_ATTEMPTS the row stays state='errored' (retried next run); at
    the cap it becomes a terminal state='quarantined'/REPEATED_ERROR so it stops
    reprocessing (red-team S2). Returns the post-increment attempt count."""
    with db.connect() as conn:
        prior = conn.execute(
            "SELECT attempts FROM inbox_files WHERE content_hash = ?", (h,)).fetchone()
        attempts = (prior["attempts"] if prior else 0) + 1
        terminal = attempts >= MAX_INTAKE_ATTEMPTS
        conn.execute(
            "INSERT OR REPLACE INTO inbox_files "
            "(content_hash, filename, state, reason, run_id, disposed, attempts, recorded_at) "
            "VALUES (?,?,?,?,?,0,?,?)",
            (h, filename,
             "quarantined" if terminal else "errored",
             REPEATED_ERROR if terminal else None,
             run_id, attempts, db.now_iso()))
    return attempts


def forget(h: str) -> None:
    """Drop the seen-record (used by undo so a restored file is reprocessed)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM inbox_files WHERE content_hash = ?", (h,))


def dispose_imported() -> int:
    """Move not-yet-disposed, successfully-IMPORTED inbox files to processed/.
    Called at the start of the NEXT intake run (undo window = until next import).
    Only moves files that still exist in the inbox and resolve inside it. Returns
    the count moved."""
    moved = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT content_hash, filename FROM inbox_files "
            "WHERE state = 'imported' AND disposed = 0").fetchall()
    dst_dir = processed_dir()
    for r in rows:
        src = inbox_dir() / (r["filename"] or "")
        if not src.exists() or not _confined(src):
            with db.connect() as conn:   # file gone already → mark disposed
                conn.execute("UPDATE inbox_files SET disposed = 1 WHERE content_hash = ?",
                             (r["content_hash"],))
            continue
        # F8-1: dispose ONLY a file whose CURRENT on-disk content still matches the
        # recorded hash. If the user re-downloaded an export under the SAME canonical
        # name (e.g. a bank's fixed "Checking.csv") with NEW content while the undo
        # window was open, moving it by stale filename would carry the NEW export to
        # processed/ and the subsequent scan would find an empty inbox → the new
        # charges silently vanish. On a content MISMATCH we DON'T move it: we mark the
        # original (already-imported) row disposed — its original content was
        # overwritten by the user and is unrecoverable, which is acceptable since the
        # user themselves replaced it — and LEAVE the new file in the inbox so the
        # subsequent scan picks it up by its NEW (not-already-seen) hash and imports it.
        try:
            current = content_hash(src)
        except OSError:                  # unreadable now → treat as gone; retry next run
            continue
        if current != r["content_hash"]:
            with db.connect() as conn:
                conn.execute("UPDATE inbox_files SET disposed = 1 WHERE content_hash = ?",
                             (r["content_hash"],))
            continue
        dst = _unique_dest(dst_dir, src.name)
        try:
            os.rename(src, dst)          # never shell out; same-fs atomic move
        except OSError:
            continue                     # rename failed → leave undisposed, retry next run
        # os.rename returned without raising → the file MOVED. Mark disposed
        # unconditionally (red-team M1): a re-created same-name file in the inbox
        # must not make dst.exists()/src.exists() lie and under-report disposal.
        # Persist the ACTUAL destination basename (which _unique_dest may have
        # suffixed on a processed/ name collision) so undo restores the CORRECT
        # file, not a same-named pre-existing one (red-team S1).
        with db.connect() as conn:
            conn.execute(
                "UPDATE inbox_files SET disposed = 1, disposed_name = ? WHERE content_hash = ?",
                (dst.name, r["content_hash"]))
        moved += 1
    return moved


def _unique_dest(d: Path, name: str) -> Path:
    dst = d / name
    i = 1
    while dst.exists():                  # never overwrite an existing processed file
        dst = d / f"{Path(name).stem}.{i}{Path(name).suffix}"
        i += 1
    return dst


# ── dashboard upload (write a received export into the inbox) ─────────────────
def stage_upload(name: str, data: bytes) -> Path:
    """Write a dashboard-uploaded export into the inbox as a complete, ready-to-
    import file, then hand it to the normal intake pipeline (validate_export + import).

    Safety: only a BASENAME of `name` is honored (never a path/traversal — the
    client cannot choose a destination); the type must be a supported export and
    the size under the cap; the destination is realpath-confined to the inbox and
    `_unique_dest` never overwrites. The file is whole (the entire request body
    already arrived), so its mtime is backdated past the stability window — it
    cannot be a partial write, so the drop-folder stability gate need not delay it.
    Returns the destination path (caller does not expose it to the browser)."""
    safe = Path(name or "").name.strip()
    if not safe or Path(safe).suffix.lower() not in SUPPORTED:
        raise UploadError("unsupported file type — upload a .csv / .ofx / .qfx export")
    if not data:
        raise UploadError("empty file")
    if len(data) > MAX_FILE_BYTES:
        raise UploadError("file too large")
    dest = _unique_dest(inbox_dir(), safe)
    if not _confined(dest):
        raise UploadError("invalid path")
    dest.write_bytes(data)
    backdated = time.time() - STABILITY_SECS - 1
    os.utime(dest, (backdated, backdated))
    return dest
