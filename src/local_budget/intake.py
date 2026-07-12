"""Intake core — source-agnostic operations over imported batches.

This is the agnostic core from the (red-team-approved) intake design: it operates
on already-parsed/validated data via the importer, and owns the batch-level
lifecycle — undo and account reassignment. The drop-folder adapter (scan,
integrity-gate, validate, dispose) lives separately in `inbox_adapter.py`.

A "batch" is an `import_runs` row; every inserted transaction and every rule
promoted by that run carries its `import_run_id`, so undo is exact and reversible.
"""
from __future__ import annotations

import os

from . import categories, db, inbox_adapter
from .ingest import importer


def scan() -> list:
    """Stable, recognized, not-already-seen inbox files (Path list)."""
    out = []
    for p in sorted(inbox_adapter.inbox_dir().iterdir()):
        # Skip symlinks (security siege, Low): a symlink dropped in the inbox would
        # otherwise let import read/parse content from OUTSIDE the drop folder.
        # Treat it like an unsupported/non-stable file — never import it.
        if p.is_symlink():
            continue
        if not p.is_file() or not inbox_adapter.is_stable(p):
            continue
        if inbox_adapter.already_seen(inbox_adapter.content_hash(p)):
            continue
        out.append(p)
    return out


def pending() -> dict:
    """What's waiting, for the dashboard's thin on-load check (zero network).
    Returns counts only — never filenames or rows (red-team S6). `last_import_at`
    is the most recent SUCCESSFUL import timestamp (a date, not PII) so the UI can
    show a freshness banner and warn when the data is stale."""
    new_files = len(scan())
    with db.connect() as conn:
        unsure = conn.execute(
            "SELECT COUNT(DISTINCT merchant_norm) FROM transactions "
            "WHERE status='posted' AND category = ?", (categories.UNCATEGORIZED,)).fetchone()[0]
        row = conn.execute(
            "SELECT MAX(completed_at) AS last FROM import_runs WHERE status = 'success'"
        ).fetchone()
    return {"new_files": new_files, "needs_review": unsure,
            "last_import_at": row["last"] if row else None}


def run_intake() -> dict:
    """The on-launch intake (ZERO network): dispose the prior run's files (undo
    window closed), then scan → integrity-gate → format-validate → import (offline
    rule categorization) → record. Serialized by the intake mutex; a second
    caller no-ops. The LLM step is separate and explicit (never here)."""
    with inbox_adapter.intake_lock() as got:
        if not got:
            return {"ran": False, "reason": "another intake is in progress"}

        # Reap orphaned in_progress runs (red-team S2): a crash between
        # begin_batch_run and finish_batch_run leaves a run 'in_progress' whose
        # committed rows count in reports but which `undo` can never reach (it would
        # reverse an older batch instead). Because the intake lock serializes all
        # intake, any in_progress run here is necessarily orphaned — finalize it
        # (success if it has rows → undoable, else undone) BEFORE opening the new batch.
        importer.reap_orphaned_runs()

        disposed = inbox_adapter.dispose_imported()   # undo window = until next import
        files = scan()
        imported = quarantined = errored = new_rows = deduped = conflicts = 0
        possible_dups = 0
        dropped_rows = 0   # malformed/unrecoverable export rows, surfaced (F1)
        quarantine_reasons: list[str] = []
        # ONE intake action = ONE undoable batch (red-team S1): every imported file
        # shares this run_id, so a single undo reverses the whole drop atomically.
        # Per-file import stays all-or-nothing (its own DB transaction), so one bad
        # file rolls back only itself, not the batch.
        run_id = importer.begin_batch_run("intake") if files else None
        for p in files:
            h = inbox_adapter.content_hash(p)
            ok, reason = inbox_adapter.validate_export(p)
            if not ok:
                inbox_adapter.record_seen(h, p.name, "quarantined", None, reason)
                quarantined += 1
                quarantine_reasons.append(reason)
                continue
            try:
                # Pass the content hash so import_file writes the 'imported'
                # seen-record ATOMICALLY inside its own import transaction, bound
                # to this batch run (red-team F7-1): commit ⟺ seen-record, so a
                # crash between commit and a separate record_seen can no longer
                # strand the file unseen and let a later re-import bind its
                # seen-record to a competing 0-row batch (silent spend loss).
                r = importer.import_file(p, run_id=run_id, content_hash=h,
                                         source_filename=p.name)
            except Exception:  # noqa: BLE001 — a TRANSIENT failure (WAL lock,
                # brief I/O blip) must NOT permanently strand a valid file (silent
                # under-count, red-team S2). Bump this hash's attempt count: while
                # under MAX_INTAKE_ATTEMPTS it stays NOT-seen so the next run
                # retries it; at the cap it's marked terminal (quarantined) so it
                # stops reprocessing. This file's rows already rolled back
                # (all-or-nothing); the shared batch run survives.
                attempts = inbox_adapter.record_error(h, p.name, run_id)
                if attempts >= inbox_adapter.MAX_INTAKE_ATTEMPTS:
                    quarantined += 1
                    quarantine_reasons.append(inbox_adapter.REPEATED_ERROR)
                else:
                    errored += 1
                continue
            # NOTE: the 'imported' seen-record is now written ATOMICALLY inside
            # import_file's transaction (above), bound to this batch run_id — NOT
            # here as a separate transaction (red-team F7-1). The quarantined and
            # errored cases still record_seen/record_error here (they import no
            # rows, so there is no transaction to ride along with).
            imported += 1
            new_rows += r["inserted"]
            deduped += r["skipped"]
            conflicts += r["conflicts"]
            possible_dups += r.get("possible_duplicates", 0)
            dropped_rows += r.get("dropped_rows", 0)
        if run_id is not None:
            # Finalize 'success' only if the batch actually inserted rows. A batch
            # that imported a file but added 0 NEW transactions (every row exact-
            # deduped — e.g. a re-import after a crash between commit and
            # record_seen) has NOTHING to undo, so 'undone' is correct: it keeps
            # last_import() pointing at the most recent batch that actually changed
            # the ledger and avoids a stray no-op undo target (red-team M3).
            status = "success" if new_rows > 0 else "undone"
            importer.finish_batch_run(run_id, status, imported, new_rows,
                                      deduped, conflicts)

    p = pending()
    return {"ran": True, "files_imported": imported, "files_quarantined": quarantined,
            "files_errored": errored,
            "quarantine_reasons": sorted(set(quarantine_reasons)), "disposed": disposed,
            "new_transactions": new_rows, "deduped": deduped,
            "possible_duplicates": possible_dups,
            "dropped_rows": dropped_rows,
            "needs_review": p["needs_review"]}


def last_import() -> dict | None:
    """The most recent successful, not-yet-undone import batch (or None)."""
    with db.connect() as conn:
        r = conn.execute(
            "SELECT run_id, source_name, completed_at, rows_inserted FROM import_runs "
            "WHERE status = 'success' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    return dict(r) if r else None


def undo_last_import() -> dict:
    """Reverse the most recent import batch completely: delete exactly its inserted
    transactions, the rules it promoted, and its conflict rows; mark the run
    undone; rebuild agent.db. Returns a summary. Most-recent-only by design
    (red-team S5) — undo again to peel back further, newest first.

    Serialized by the SAME single intake mutex as run_intake/dispose (red-team
    S3): undo mutates the same inbox/processed files and rows, so it must never
    race a concurrent intake. If the lock is held, this no-ops cleanly."""
    with inbox_adapter.intake_lock() as got:
        if not got:
            return {"undone": False, "reason": "another intake is in progress"}

        batch = last_import()
        if not batch:
            return {"undone": False, "reason": "no import to undo"}
        run_id = batch["run_id"]
        with db.connect() as conn:
            conflicts = conn.execute(
                "DELETE FROM import_conflicts WHERE run_id = ?", (run_id,)).rowcount
            rules = conn.execute(
                "DELETE FROM category_rules WHERE import_run_id = ?", (run_id,)).rowcount
            txns = conn.execute(
                "DELETE FROM transactions WHERE import_run_id = ?", (run_id,)).rowcount
            files = conn.execute(
                "SELECT content_hash, filename, disposed, disposed_name "
                "FROM inbox_files WHERE run_id = ?",
                (run_id,)).fetchall()
            conn.execute("UPDATE import_runs SET status = 'undone' WHERE run_id = ?", (run_id,))

        # Restore each disposed file to the inbox and clear its seen-record so it
        # can be re-imported (e.g. into the correct account). (red-team M3)
        # CRITICAL (red-team F2): restore through the SAME confined, non-clobbering
        # path as dispose_imported — a live inbox file of the same name must NEVER
        # be overwritten/destroyed. _unique_dest picks a fresh name on collision;
        # _confined blocks any traversal/symlink escape.
        restored = 0
        for f in files:
            # Restore from the ACTUAL disposed basename (which dispose_imported may
            # have suffixed on a processed/ name collision); fall back to `filename`
            # only for legacy rows predating disposed_name (red-team S1). Restoring
            # via the original `filename` would rename the WRONG (pre-existing)
            # processed/ file and strand the real export.
            name = f["disposed_name"] or f["filename"]
            if f["disposed"] and name:
                src = inbox_adapter.processed_dir() / name
                if src.exists() and inbox_adapter._confined(src):
                    dst = inbox_adapter._unique_dest(inbox_adapter.inbox_dir(), name)
                    if inbox_adapter._confined(dst):
                        os.rename(src, dst)       # never overwrite a live inbox file
                        restored += 1
            inbox_adapter.forget(f["content_hash"])

        return {"undone": True, "run_id": run_id, "transactions_removed": txns,
                "rules_removed": rules, "conflicts_removed": conflicts,
                "files_restored": restored, "source": batch["source_name"]}
