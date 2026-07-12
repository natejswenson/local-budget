"""Drop-folder adapter + intake orchestration: integrity gating, format validation,
disposal-to-processed, content-hash tracking, mutex, end-to-end run_intake."""
from __future__ import annotations


import pytest

from local_budget import db, inbox_adapter, intake


@pytest.fixture
def inbox(data_dir, tmp_path, monkeypatch):
    db.init_schema()
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    # Make freshly-written files count as "stable" immediately (no real sleep).
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    return box


def _stmt_csv(box, name, rows):
    p = box / name
    p.write_text("".join(f'"{d}","{a}","*","","{desc}"\n' for d, a, desc in rows))
    return p


# ── integrity gating ─────────────────────────────────────────────────────────
def test_in_progress_download_skipped(inbox):
    _stmt_csv(inbox, "stmt.csv.crdownload", [("06/03/2026", "-5.00", "X")])
    assert inbox_adapter.is_stable(inbox / "stmt.csv.crdownload") is False


def test_zero_byte_skipped(inbox):
    (inbox / "empty.csv").write_text("")
    assert inbox_adapter.is_stable(inbox / "empty.csv") is False


def test_recently_modified_skipped(inbox, monkeypatch):
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 5)
    _stmt_csv(inbox, "fresh.csv", [("06/03/2026", "-5.00", "X")])
    assert inbox_adapter.is_stable(inbox / "fresh.csv") is False   # just written


# ── statement-format validation (never ingest-on-guess) ─────────────────────────────
def test_valid_stmt_csv_passes(inbox):
    ok, reason = inbox_adapter.validate_export(_stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL")]))
    assert ok and reason is None


def test_non_stmt_csv_quarantined(inbox):
    (inbox / "other.csv").write_text("name,email\nAlice,a@x.com\n")
    ok, reason = inbox_adapter.validate_export(inbox / "other.csv")
    assert not ok and reason == inbox_adapter.NOT_STATEMENT


def test_truncated_csv_rejected(inbox):
    # A genuinely PARTIAL final row (cut off mid-line: only 2 columns, not the
    # the required 5) must still be rejected. (red-team S2: detect truncation by the final
    # row failing shape, NOT by a missing trailing newline.)
    (inbox / "trunc.csv").write_text(
        '"06/03/2026","-5.00","*","","SHELL"\n"06/04/2026","-9.0')
    ok, reason = inbox_adapter.validate_export(inbox / "trunc.csv")
    assert not ok and reason == inbox_adapter.NOT_STATEMENT


def test_complete_csv_no_trailing_newline_validates(inbox):
    # A COMPLETE export whose well-formed final row lacks a trailing newline
    # (common) must PASS — never false-quarantined as truncated (red-team S2).
    p = inbox / "nonl.csv"
    p.write_text('"06/03/2026","-5.00","*","","SHELL"\n'
                 '"06/04/2026","-9.00","*","","WALMART"')   # no trailing \n
    assert not p.read_text().endswith("\n")
    ok, reason = inbox_adapter.validate_export(p)
    assert ok and reason is None


def test_truncated_ofx_rejected(inbox):
    (inbox / "t.ofx").write_text("<OFX><STMTRS><STMTTRN><FITID>1")   # no </OFX>
    ok, reason = inbox_adapter.validate_export(inbox / "t.ofx")
    assert not ok and reason == inbox_adapter.TRUNCATED


# ── end-to-end orchestration ─────────────────────────────────────────────────
def test_run_intake_imports_and_quarantines(inbox):
    _stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL"), ("06/04/2026", "-9.00", "WALMART")])
    (inbox / "junk.csv").write_text("foo,bar\n1,2\n")
    r = intake.run_intake()
    assert r["ran"] and r["files_imported"] == 1 and r["files_quarantined"] == 1
    assert r["new_transactions"] == 2
    assert r["quarantine_reasons"] == [inbox_adapter.NOT_STATEMENT]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_symlink_in_inbox_not_imported(inbox, tmp_path):
    # Security siege (Low): a symlink dropped in the inbox must NOT be read/parsed —
    # otherwise it would import content from OUTSIDE the drop folder. scan() skips it.
    outside = tmp_path / "outside.csv"
    outside.write_text('"06/03/2026","-5.00","*","","SHELL"\n')
    (inbox / "link.csv").symlink_to(outside)
    assert intake.scan() == []
    r = intake.run_intake()
    assert r["files_imported"] == 0 and r["new_transactions"] == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_already_seen_not_reprocessed(inbox):
    _stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL")])
    intake.run_intake()
    # second run: same file still present, but content-hash says already-seen
    r2 = intake.run_intake()
    assert r2["files_imported"] == 0


def test_disposal_to_processed_on_next_run(inbox):
    _stmt_csv(inbox, "a.csv", [("06/03/2026", "-5.00", "SHELL")])
    intake.run_intake()                       # imports a.csv, leaves it in inbox
    assert (inbox / "a.csv").exists()
    _stmt_csv(inbox, "b.csv", [("06/10/2026", "-9.00", "WALMART")])
    r = intake.run_intake()                   # disposes a.csv, imports b.csv
    assert r["disposed"] == 1
    assert not (inbox / "a.csv").exists()
    assert (inbox / "processed" / "a.csv").exists()


def test_same_name_redownload_different_content_not_lost(inbox):
    # F8-1: dispose_imported moves by FILENAME. If the user re-downloads an export
    # under the SAME canonical name (a bank's fixed "Checking.csv") with NEW content
    # while the prior file's undo window is open, the stale-filename move would carry
    # the NEW export to processed/ and the next scan would find an empty inbox → the
    # new charges silently vanish. Fix: dispose only when on-disk content STILL
    # matches the recorded hash; on a mismatch leave the new file for the scan.
    _stmt_csv(inbox, "Checking.csv", [("06/03/2026", "-5.00", "SHELL")])     # content A
    r1 = intake.run_intake()                  # imports content A, leaves it in inbox
    assert r1["files_imported"] == 1 and r1["new_transactions"] == 1
    # Re-download SAME name, DIFFERENT content (a -$99 BESTBUY charge) before the
    # next run disposes content A.
    _stmt_csv(inbox, "Checking.csv", [("06/10/2026", "-99.00", "BESTBUY")])  # content B
    r2 = intake.run_intake()                  # must NOT silently dispose content B away
    # content B's BESTBUY charge IS imported (not lost), the inbox did NOT silently empty
    assert r2["files_imported"] == 1
    assert r2["new_transactions"] == 1
    assert (inbox / "Checking.csv").exists()  # the new file landed/stayed for the scan
    with db.connect() as conn:
        amounts = [row[0] for row in conn.execute(
            "SELECT amount_cents FROM transactions ORDER BY amount_cents").fetchall()]
        assert amounts == [-9900, -500]       # both charges present, integer cents
        # content A's original row is marked disposed (its content was overwritten).
        assert conn.execute(
            "SELECT disposed FROM inbox_files WHERE state='imported' AND reason IS NULL "
            "ORDER BY recorded_at LIMIT 1").fetchone()[0] == 1


def test_undo_restores_file_and_forgets(inbox):
    _stmt_csv(inbox, "a.csv", [("06/03/2026", "-5.00", "SHELL")])
    intake.run_intake()
    _stmt_csv(inbox, "b.csv", [("06/10/2026", "-9.00", "WALMART")])
    intake.run_intake()                       # a.csv -> processed/, b.csv imported
    r = intake.undo_last_import()             # undo b.csv
    assert r["undone"] and r["transactions_removed"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1  # only SHELL


def test_undo_restores_processed_to_unique_name_not_overwrite(inbox):
    # F2 (focused): processed/a.csv exists from an import; a live a.csv (new
    # content) is in the inbox; undo of that batch must keep BOTH — live intact,
    # restored under a fresh name.
    _stmt_csv(inbox, "a.csv", [("06/03/2026", "-5.00", "SHELL")])
    intake.run_intake()                       # import a.csv
    _stmt_csv(inbox, "b.csv", [("06/10/2026", "-9.00", "WALMART")])
    intake.run_intake()                       # disposes a.csv -> processed/, imports b.csv
    assert (inbox / "processed" / "a.csv").exists()
    # Now drop a NEW live a.csv into the inbox before undoing the b.csv batch.
    # (b.csv batch has no a.csv, so this exercises only the live-file guard for b.)
    # To trigger the a.csv restore we undo until the a.csv batch:
    intake.undo_last_import()                 # undo b.csv batch
    live = _stmt_csv(inbox, "a.csv", [("07/01/2026", "-42.00", "TARGET")])
    live_bytes = live.read_bytes()
    r = intake.undo_last_import()             # undo a.csv batch → restores processed/a.csv
    assert r["undone"]
    assert (inbox / "a.csv").read_bytes() == live_bytes      # live file NOT clobbered
    # the restored old copy exists under a unique name
    restored = [q for q in inbox.iterdir() if q.is_file() and q.name.startswith("a.")
                and q.name != "a.csv"]
    assert restored, "restored file should take a fresh, non-clobbering name"


def test_undo_restores_correct_file_under_processed_name_collision(inbox):
    # S1: week-1 Checking.csv (content A) imported → disposed to processed/Checking.csv.
    # week-2 Checking.csv (SAME name, DIFFERENT content B) imported → disposed →
    # collides → lands at processed/Checking.1.csv, but the DB row still names it
    # "Checking.csv". undo of the week-2 batch must restore content B (the suffixed
    # file), NEVER content A — and content A must stay safe in processed/.
    wk1 = _stmt_csv(inbox, "Checking.csv", [("06/03/2026", "-5.00", "SHELL")])   # content A
    content_a = wk1.read_bytes()
    intake.run_intake()                       # import week-1 Checking.csv (batch 1)
    # A separate trigger file in batch 2 disposes wk1's Checking.csv -> processed/.
    _stmt_csv(inbox, "trigger1.csv", [("06/05/2026", "-2.00", "T1")])
    intake.run_intake()                       # disposes wk1 -> processed/Checking.csv
    assert (inbox / "processed" / "Checking.csv").read_bytes() == content_a
    assert not (inbox / "Checking.csv").exists()
    # week-2: SAME name, DIFFERENT content (a genuinely different export).
    wk2 = _stmt_csv(inbox, "Checking.csv", [("06/10/2026", "-9.00", "WALMART")])  # content B
    content_b = wk2.read_bytes()
    assert content_a != content_b
    intake.run_intake()                       # import week-2 Checking.csv (batch 3)
    # A final trigger disposes wk2's Checking.csv → collides with the existing
    # processed/Checking.csv → lands at processed/Checking.1.csv.
    _stmt_csv(inbox, "trigger2.csv", [("06/11/2026", "-1.00", "T2")])
    intake.run_intake()
    assert (inbox / "processed" / "Checking.csv").read_bytes() == content_a
    assert (inbox / "processed" / "Checking.1.csv").read_bytes() == content_b
    # Undo peels newest-first; peel until the week-2 Checking batch (batch 3).
    intake.undo_last_import()                 # undo trigger2 batch
    r = intake.undo_last_import()             # undo week-2 Checking batch
    assert r["undone"] and r["files_restored"] == 1
    # The RESTORED inbox file must be content B (week-2), not content A.
    restored = [q for q in inbox.iterdir() if q.is_file() and q.name.startswith("Checking")]
    assert len(restored) == 1
    assert restored[0].read_bytes() == content_b      # correct file restored
    # content A (week-1) is untouched and recoverable in processed/.
    assert (inbox / "processed" / "Checking.csv").read_bytes() == content_a


def test_one_intake_one_undo_reverses_all_files(inbox):
    # S1: drop 2 files in ONE run_intake → ONE undo removes ALL rows from BOTH
    # files and restores/forgets both.
    _stmt_csv(inbox, "a.csv", [("06/03/2026", "-5.00", "SHELL")])
    _stmt_csv(inbox, "b.csv", [("06/04/2026", "-9.00", "WALMART")])
    r = intake.run_intake()
    assert r["files_imported"] == 2 and r["new_transactions"] == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        # both files share ONE run_id (one batch)
        runs = conn.execute(
            "SELECT COUNT(DISTINCT run_id) FROM inbox_files WHERE state='imported'"
        ).fetchone()[0]
        assert runs == 1
    u = intake.undo_last_import()
    assert u["undone"] and u["transactions_removed"] == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        # both files forgotten so they can be reprocessed
        assert conn.execute("SELECT COUNT(*) FROM inbox_files").fetchone()[0] == 0


def test_mutex_blocks_concurrent_intake(inbox):
    with inbox_adapter.intake_lock() as got:
        assert got is True
        r = intake.run_intake()               # re-entrant call can't acquire
        assert r["ran"] is False


def test_undo_no_ops_while_intake_lock_held(inbox):
    # S3: undo takes the SAME single intake mutex; if held, it no-ops cleanly
    # (never races a concurrent run_intake/dispose).
    _stmt_csv(inbox, "a.csv", [("06/03/2026", "-5.00", "SHELL")])
    intake.run_intake()
    with inbox_adapter.intake_lock() as got:
        assert got is True
        r = intake.undo_last_import()
        assert r["undone"] is False and r["reason"] == "another intake is in progress"
    # rows untouched by the no-op undo
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_run_intake_multifile_drift_posts_both_and_reports_possible_dup(inbox):
    # F1: a reformatted re-download arriving in a LATER file of the SAME drop
    # (shared batch run_id) must NOT silently double-count. New contract: BOTH
    # rows post (real spend counts) AND run_intake reports >=1 possible_duplicate
    # (surfaced) — never a silent double-count with 0 conflicts.
    _stmt_csv(inbox, "a.csv", [("06/03/2026", "-42.00", "AMAZON MKTPL ABC")])
    _stmt_csv(inbox, "b.csv", [("06/03/2026", "-42.00", "AMAZON MKTPL XYZ STORE")])
    r = intake.run_intake()
    assert r["files_imported"] == 2
    assert r["new_transactions"] == 2                # both posted/counted
    assert r["possible_duplicates"] >= 1             # surfaced, not silent
    with db.connect() as conn:
        posted = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0]
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert posted == 2 and open_conf == 1


def test_transient_error_self_heals_on_next_run(inbox, monkeypatch):
    # S2: a ONE-SHOT transient import failure must NOT permanently strand a valid
    # file. The errored file (attempts < cap) is re-scanned and imported on the
    # next run.
    _stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL")])
    from local_budget.ingest import importer

    calls = {"n": 0}
    orig = importer.import_file

    def flaky(path, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient WAL lock")
        return orig(path, **kw)

    monkeypatch.setattr(importer, "import_file", flaky)
    r1 = intake.run_intake()
    assert r1["files_imported"] == 0 and r1["files_errored"] == 1   # transient miss
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    # Next run: the errored file is NOT treated as seen → retried → imports.
    r2 = intake.run_intake()
    assert r2["files_imported"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
        # the inbox_files row is now 'imported' with attempts reset.
        row = conn.execute(
            "SELECT state, attempts FROM inbox_files").fetchone()
        assert row["state"] == "imported" and row["attempts"] == 0


def test_persistent_error_stops_after_cap_and_quarantines(inbox, monkeypatch):
    # S2: a PERSISTENTLY-erroring file must stop reprocessing after the cap and be
    # reported quarantined (not retried forever).
    _stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL")])
    from local_budget.ingest import importer

    def always_fail(path, **kw):
        raise RuntimeError("persistent error")

    monkeypatch.setattr(importer, "import_file", always_fail)
    # First (cap-1) runs error; the cap-th run quarantines.
    for _ in range(inbox_adapter.MAX_INTAKE_ATTEMPTS - 1):
        r = intake.run_intake()
        assert r["files_errored"] == 1
    rq = intake.run_intake()
    assert rq["files_quarantined"] == 1
    assert inbox_adapter.REPEATED_ERROR in rq["quarantine_reasons"]
    with db.connect() as conn:
        row = conn.execute("SELECT state, attempts FROM inbox_files").fetchone()
        assert row["state"] == "quarantined"
        assert row["attempts"] == inbox_adapter.MAX_INTAKE_ATTEMPTS
    # A further run no longer reprocesses it (terminal).
    rfinal = intake.run_intake()
    assert rfinal["files_errored"] == 0 and rfinal["files_quarantined"] == 0


def _ofx_with_optional_fitids(box, name, txns):
    """Build a minimal OFX where each txn dict may OMIT 'fitid' (no <FITID> tag).
    Used for red-team F-3: a FITID-less <STMTTRN> must NOT drop the whole file."""
    header = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
              "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
              "OLDFILEUID:NONE\nNEWFILEUID:NONE\n")
    parts = []
    for t in txns:
        p = ["<STMTTRN>", f"<TRNTYPE>{t['trntype']}", f"<DTPOSTED>{t['dtposted']}",
             f"<TRNAMT>{t['amount']}"]
        if t.get("fitid"):
            p.append(f"<FITID>{t['fitid']}")
        p.append(f"<NAME>{t['name']}")
        p.append("</STMTTRN>")
        parts.append("\n".join(p))
    body = "\n".join(parts)
    text = (f"{header}\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260601120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
            "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>121000248<ACCTID>1234567890"
            "<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
            "<BANKTRANLIST><DTSTART>20260601<DTEND>20260630\n"
            f"{body}\n</BANKTRANLIST>\n"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260630</LEDGERBAL>\n"
            "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n")
    p = box / name
    p.write_text(text)
    return p


def test_fitidless_ofx_row_imports_whole_file_and_dedups(inbox):
    # F-3: an OFX whose 2nd <STMTTRN> lacks <FITID> must NOT lose the whole file.
    # BOTH transactions import (the FITID-less row gets a synthesized content FITID),
    # and a re-download of the SAME OFX dedups BOTH (no double-count).
    from local_budget import db
    from local_budget.ingest import importer
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1", "name": "WALMART"},
        {"trntype": "DEBIT", "dtposted": "20260604", "amount": "-9.00", "name": "SHELL"},  # no FITID
    ]
    p = _ofx_with_optional_fitids(inbox, "stmt.ofx", txns)
    r = importer.import_file(p)
    assert r["inserted"] == 2                       # whole file imported, nothing lost
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0] == 2
    # Re-download the identical OFX → both dedup (real FITID + synthetic FITID).
    p2 = _ofx_with_optional_fitids(inbox, "stmt2.ofx", txns)
    r2 = importer.import_file(p2)
    assert r2["inserted"] == 0 and r2["skipped"] == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0] == 2


def _ofx_with_raw_txns(box, name, raw_txns):
    """Build an OFX where each entry is a raw list of STMTTRN child tags, so a test
    can inject a MALFORMED row (e.g. a non-numeric <TRNAMT>) that ofxparse discards
    as unrecoverable (not a FITID-less row). Used for red-team F1."""
    header = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\n"
              "ENCODING:USASCII\nCHARSET:1252\nCOMPRESSION:NONE\n"
              "OLDFILEUID:NONE\nNEWFILEUID:NONE\n")
    body = "\n".join("<STMTTRN>\n" + "\n".join(tags) + "\n</STMTTRN>" for tags in raw_txns)
    text = (f"{header}\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260601120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
            "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>121000248<ACCTID>1234567890"
            "<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
            "<BANKTRANLIST><DTSTART>20260601<DTEND>20260630\n"
            f"{body}\n</BANKTRANLIST>\n"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260630</LEDGERBAL>\n"
            "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n")
    p = box / name
    p.write_text(text)
    return p


def test_malformed_ofx_row_dropped_count_surfaced_good_row_imports(inbox):
    # F1: an OFX with 1 GOOD row + 1 row with a malformed (non-numeric) amount.
    # The good row must import AND the loss must be SURFACED (dropped_rows >= 1),
    # never silently disposed. The row-count reconcile still passes (the malformed
    # row is a parse-time drop, separate from `seen`).
    from local_budget.ingest import importer
    p = _ofx_with_raw_txns(inbox, "stmt.ofx", [
        ["<TRNTYPE>DEBIT", "<DTPOSTED>20260603", "<TRNAMT>-50.00", "<FITID>G1", "<NAME>WALMART"],
        ["<TRNTYPE>DEBIT", "<DTPOSTED>20260604", "<TRNAMT>NOTANUMBER", "<FITID>G2", "<NAME>SHELL"],
    ])
    r = importer.import_file(p)
    assert r["inserted"] == 1                 # the good row imported
    assert r["dropped_rows"] >= 1             # the malformed row's loss is surfaced
    assert r["status"] == "success"           # good rows still import, file not aborted
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0] == 1


def test_run_intake_surfaces_dropped_rows(inbox):
    # F1 (intake path): run_intake SUMS dropped_rows across files and reports it.
    _ofx_with_raw_txns(inbox, "stmt.ofx", [
        ["<TRNTYPE>DEBIT", "<DTPOSTED>20260603", "<TRNAMT>-50.00", "<FITID>G1", "<NAME>WALMART"],
        ["<TRNTYPE>DEBIT", "<DTPOSTED>20260604", "<TRNAMT>NOTANUMBER", "<FITID>G2", "<NAME>SHELL"],
    ])
    r = intake.run_intake()
    assert r["files_imported"] == 1
    assert r["new_transactions"] == 1
    assert r["dropped_rows"] >= 1


def test_all_fitidless_ofx_validates_and_imports_on_intake_path(inbox):
    # S1: an all-FITID-less OFX must pass validate_export (no <FITID> gate) AND import
    # its transactions via run_intake (recovery reachable on the intake path).
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "name": "WALMART"},
        {"trntype": "DEBIT", "dtposted": "20260604", "amount": "-9.00", "name": "SHELL"},
    ]
    p = _ofx_with_optional_fitids(inbox, "stmt.ofx", txns)   # all rows omit <FITID>
    ok, reason = inbox_adapter.validate_export(p)
    assert ok and reason is None
    r = intake.run_intake()
    assert r["files_imported"] == 1 and r["new_transactions"] == 2
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0] == 2


def test_orphaned_in_progress_run_reaped_and_undoable(inbox):
    # S2: an orphaned in_progress run (crash between begin/finish) with committed
    # rows must be reaped to 'success' on the next run_intake (under the lock) so it
    # becomes a reachable, undoable batch — and the subsequent undo reverses the
    # CORRECT (orphaned) batch, not an older one.
    from local_budget.ingest import importer
    # Simulate the orphan: open a batch, commit a row tagged with it, then crash
    # (never call finish_batch_run) → row stays, run stays 'in_progress'.
    run_id = importer.begin_batch_run("intake")
    f = _stmt_csv(inbox, "orphan.csv", [("05/01/2026", "-77.00", "ORPHANED")])
    files = intake.scan()
    importer.import_file(files[0], run_id=run_id)
    # The crash happened AFTER the row committed; the file was already consumed.
    # Remove it so the next run_intake has nothing new to import (it only reaps).
    f.unlink()
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM import_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        assert st == "in_progress"          # orphaned
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1

    # Next intake (nothing new to import) reaps the orphan under the lock.
    intake.run_intake()
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM import_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert st == "success"                  # finalized → reachable + undoable

    # Undo now targets the reaped orphan (the correct, most-recent success batch).
    u = intake.undo_last_import()
    assert u["undone"] and u["run_id"] == run_id and u["transactions_removed"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_non_intake_in_progress_run_not_reaped(inbox):
    # S2 (tightened): a concurrent NON-batched `budget import` opens its own
    # in_progress run tagged with the FILE's basename (NOT 'intake') and does NOT
    # hold the intake lock. The reaper must scope to source_name='intake' only, so
    # this live direct-import run is NEVER mis-finalized.
    from local_budget import db
    from local_budget.ingest import importer
    with db.connect() as conn:
        rid = importer._begin_run(conn, db.now_iso(), "manual_export.qfx")  # not 'intake'
        conn.commit()
    n = importer.reap_orphaned_runs()
    assert n == 0
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM import_runs WHERE run_id = ?", (rid,)).fetchone()[0]
    assert st == "in_progress"   # untouched — out of the reaper's scope


def test_orphaned_empty_in_progress_run_reaped_to_undone(inbox):
    # S2: an orphaned in_progress run with NO committed rows is finalized 'undone'
    # so it never becomes a stray "last import" target.
    from local_budget.ingest import importer
    run_id = importer.begin_batch_run("intake")   # opened, never finished, no rows
    intake.run_intake()
    with db.connect() as conn:
        st = conn.execute("SELECT status FROM import_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    assert st == "undone"


def test_record_seen_conn_rolls_back_with_transaction(inbox):
    # F7-1: record_seen(conn=...) writes inside the caller's transaction, so a
    # rollback discards the seen-record (no crash window where it persists alone).
    import sqlite3

    from local_budget import paths
    h = "deadbeef"
    conn = sqlite3.connect(paths.budget_db_path())
    conn.row_factory = sqlite3.Row
    try:
        inbox_adapter.record_seen(h, "x.csv", "imported", 99, None, conn=conn)
        # visible inside the open transaction...
        assert conn.execute(
            "SELECT COUNT(*) FROM inbox_files WHERE content_hash = ?", (h,)).fetchone()[0] == 1
        conn.rollback()                      # ...but the crash rolls it back
    finally:
        conn.close()
    assert inbox_adapter.already_seen(h) is False   # nothing persisted


def test_direct_import_writes_no_seen_record(inbox):
    # F7-1 (d): direct `budget import` (no content_hash) writes NO inbox_files
    # seen-record — current behavior preserved, the atomic path is intake-only.
    from local_budget.ingest import importer
    p = _stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL")])
    r = importer.import_file(p)                       # no content_hash
    assert r["inserted"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM inbox_files").fetchone()[0] == 0


def test_imported_seen_record_atomic_same_run_as_rows(inbox):
    # F7-1: after a successful intake, the inbox_files 'imported' row for the
    # file's hash carries the SAME run_id as the batch that holds its transactions
    # (the seen-record is written inside import_file's transaction, not a separate
    # one). undo of that batch then deletes the rows AND restores the file AND
    # forgets the hash, so a re-drop re-imports it — no decoupling.
    p = _stmt_csv(inbox, "stmt.csv", [("06/03/2026", "-5.00", "SHELL")])
    h = inbox_adapter.content_hash(p)
    intake.run_intake()
    with db.connect() as conn:
        seen_run = conn.execute(
            "SELECT run_id, state FROM inbox_files WHERE content_hash = ?", (h,)
        ).fetchone()
        txn_run = conn.execute(
            "SELECT DISTINCT import_run_id FROM transactions").fetchall()
    assert seen_run["state"] == "imported"
    assert len(txn_run) == 1
    assert seen_run["run_id"] == txn_run[0]["import_run_id"]   # bound to owning batch

    u = intake.undo_last_import()
    assert u["undone"] and u["transactions_removed"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        # hash forgotten (coupled to the owning batch) → a re-drop re-imports it
        assert conn.execute(
            "SELECT COUNT(*) FROM inbox_files WHERE content_hash = ?", (h,)).fetchone()[0] == 0
    # The file was never disposed (single intake run → still in the inbox), so it
    # is recoverable in place; undo forgot its hash so the next run re-imports it.
    assert (inbox / "stmt.csv").exists()
    r2 = intake.run_intake()                                   # re-drop recovers
    assert r2["files_imported"] == 1 and r2["new_transactions"] == 1


def test_crash_window_no_silent_spend_loss(inbox):
    # F7-1 (reviewer's −$5 SHELL scenario): the file's rows commit under a batch but
    # the 'imported' seen-record is LOST (simulate the old crash between the import
    # commit and the SEPARATE record_seen). The file is then re-dropped/re-scanned.
    #
    # OLD bug: the re-import exact-deduped every row → 0 NEW rows → a competing 0-row
    # 'undone' batch, and the seen-record bound to THAT batch. A later undo of the
    # OWNING batch deleted the real −$5 spend but could not forget the hash (still
    # 'imported' → already_seen) nor restore the file → the charge vanished forever.
    #
    # FIX: on a 0-new-row re-import the seen-record binds to the OWNING batch (the
    # one already holding the matching rows), re-coupling undo. Assert recovery.
    p = _stmt_csv(inbox, "shell.csv", [("06/03/2026", "-5.00", "SHELL")])
    h = inbox_adapter.content_hash(p)
    intake.run_intake()
    owning_run = intake.last_import()["run_id"]
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions").fetchone()[0] == -500

    # Simulate the crash: rows committed, seen-record vanished.
    inbox_adapter.forget(h)
    assert inbox_adapter.already_seen(h) is False              # unseen again

    # Re-scan + re-import → every row exact-dedups → 0 NEW rows. The seen-record is
    # bound (atomically) to the OWNING batch, not a competing 0-row batch.
    r2 = intake.run_intake()
    assert r2["files_imported"] == 1 and r2["new_transactions"] == 0
    with db.connect() as conn:
        bound_run = conn.execute(
            "SELECT run_id FROM inbox_files WHERE content_hash = ?", (h,)).fetchone()["run_id"]
    assert bound_run == owning_run                             # re-coupled to the owner

    # last_import still points at the owning (row-changing) batch (M3: the 0-row
    # re-import batch is 'undone', never a stray undo target).
    assert intake.last_import()["run_id"] == owning_run

    # Undo the owning batch: spend removed AND hash forgotten → no stranded
    # already-seen file. A re-drop then fully recovers the −$5 charge.
    u = intake.undo_last_import()
    assert u["undone"] and u["run_id"] == owning_run and u["transactions_removed"] == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM inbox_files WHERE content_hash = ?", (h,)).fetchone()[0] == 0
    assert inbox_adapter.already_seen(h) is False              # not stranded
    assert (inbox / "shell.csv").exists()                      # raw file still present
    rfinal = intake.run_intake()                               # re-drop recovers
    assert rfinal["new_transactions"] == 1                     # charge back, NOT lost
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions").fetchone()[0] == -500


def test_path_traversal_filename_confined(inbox):
    # A crafted name can't escape the inbox via the confinement check.
    weird = inbox / "evil.csv"
    weird.write_text('"06/03/2026","-5.00","*","","X"\n')
    assert inbox_adapter._confined(weird) is True
    assert inbox_adapter._confined(inbox / ".." / ".." / "etc" / "passwd") is False
