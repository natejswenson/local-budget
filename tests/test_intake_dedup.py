"""Intake data-integrity core: CSV occurrence-ordinal FITID (red-team F1) + undo.

The central guarantee: across overlapping CSV re-downloads, the same charge is
NEVER double-counted, yet two genuinely-distinct identical charges are NEVER
silently dropped.
"""
from __future__ import annotations

import pytest

from local_budget import db, intake
from local_budget.ingest import importer


def _csv(tmp_path, rows, name="wf.csv"):
    # Wells Fargo HEADERLESS shape: Date, Amount, *, blank, Description
    p = tmp_path / name
    p.write_text("".join(f'"{d}","{a}","*","","{desc}"\n' for d, a, desc in rows))
    return p


def _count(merchant=None):
    with db.connect() as conn:
        if merchant:
            return conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE merchant_norm LIKE ?",
                (f"%{merchant}%",)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0]


def test_two_identical_same_day_charges_both_kept(data_dir, tmp_path):
    # Two $5 coffees, same shop, same day → occurrence 0 and 1 → BOTH kept (F1).
    db.init_schema()
    importer.import_file(_csv(tmp_path, [
        ("06/03/2026", "-5.00", "STARBUCKS"),
        ("06/03/2026", "-5.00", "STARBUCKS"),
    ]))
    assert _count("STARBUCKS") == 2


def test_overlapping_reimport_does_not_double_count(data_dir, tmp_path):
    db.init_schema()
    may = [("05/{:02d}/2026".format(d), f"-{d}.00", "WALMART") for d in (10, 20, 28)]
    importer.import_file(_csv(tmp_path, may, "may.csv"))
    assert _count() == 3
    # Re-download an OVERLAPPING window: May 20–28 repeats + June 5 is new.
    overlap = [("05/20/2026", "-20.00", "WALMART"),
               ("05/28/2026", "-28.00", "WALMART"),
               ("06/05/2026", "-9.00", "WALMART")]
    importer.import_file(_csv(tmp_path, overlap, "jun.csv"))
    assert _count() == 4   # only the genuinely-new June 5 added; overlap deduped


def test_window_slide_keeps_occurrence_stable(data_dir, tmp_path):
    # The occurrence ordinal is per-KEY, not a global row index: adding an earlier
    # unrelated row must NOT shift the dedup of identical-key repeats.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [
        ("06/03/2026", "-5.00", "STARBUCKS"),
        ("06/03/2026", "-5.00", "STARBUCKS"),
    ], "a.csv"))
    # Re-download with an EARLIER unrelated row prepended (window slid back).
    importer.import_file(_csv(tmp_path, [
        ("06/01/2026", "-99.00", "SHELL"),
        ("06/03/2026", "-5.00", "STARBUCKS"),
        ("06/03/2026", "-5.00", "STARBUCKS"),
    ], "b.csv"))
    assert _count("STARBUCKS") == 2   # the two coffees deduped, not re-inserted
    assert _count("SHELL") == 1
    assert _count() == 3


def test_fitid_is_content_only_no_row_index(data_dir, tmp_path):
    # Same content imported under a different filename/order yields identical FITIDs.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-5.00", "STARBUCKS")], "x.csv"))
    with db.connect() as conn:
        fitid = conn.execute("SELECT fitid FROM transactions").fetchone()[0]
    assert fitid.startswith("csv:") and ":0" not in fitid  # hash, not date:amount:desc:index


def test_undo_last_import_removes_exactly_that_batch(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("05/10/2026", "-100.00", "WALMART")], "a.csv"))
    importer.import_file(_csv(tmp_path, [("06/10/2026", "-150.00", "TARGET")], "b.csv"))
    assert _count() == 2
    r = intake.undo_last_import()
    assert r["undone"] and r["transactions_removed"] == 1
    assert _count() == 1
    assert _count("TARGET") == 0 and _count("WALMART") == 1   # only the latest batch gone


def test_undo_with_nothing_to_undo(data_dir):
    db.init_schema()
    assert intake.undo_last_import()["undone"] is False


def test_zero_new_row_intake_batch_finalizes_undone_not_undo_target(data_dir, tmp_path, monkeypatch):
    # M3: a batch that imports a file but inserts 0 NEW rows (every row exact-
    # deduped — e.g. a re-import after a crash between commit and record_seen) must
    # finalize 'undone', NOT 'success'. last_import() must still point at the most
    # recent batch that actually changed the ledger, so undo isn't a confusing no-op.
    from local_budget import db, inbox_adapter
    db.init_schema()
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    box.joinpath("real.csv").write_text(
        '"06/03/2026","-5.00","*","","SHELL"\n')
    r1 = intake.run_intake()                  # real batch: 1 new row → success
    assert r1["files_imported"] == 1 and r1["new_transactions"] == 1
    real_run = intake.last_import()["run_id"]

    # Simulate the crash-window re-import: the file's content was already committed
    # but its seen-record was lost (forget it) so it is re-scanned & re-imported,
    # exact-deduping every row → 0 net inserts.
    h = inbox_adapter.content_hash(box / "real.csv")
    inbox_adapter.forget(h)
    r2 = intake.run_intake()                  # re-import: file imported, 0 new rows
    assert r2["files_imported"] == 1 and r2["new_transactions"] == 0

    # The empty batch is 'undone', not a stray success undo target.
    with db.connect() as conn:
        empty_run = conn.execute(
            "SELECT run_id, status FROM import_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert empty_run["status"] == "undone"
    # last_import still points at the real (row-changing) batch.
    assert intake.last_import()["run_id"] == real_run


def test_import_tags_rows_with_run_id(data_dir, tmp_path):
    db.init_schema()
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-5.00", "STARBUCKS")]))
    with db.connect() as conn:
        rid = conn.execute("SELECT import_run_id FROM transactions").fetchone()[0]
    assert rid == r["run_id"]


def test_description_drift_flagged_not_double_counted(data_dir, tmp_path):
    # Re-download where WF reformatted the merchant text → same charge, different
    # description, in a LATER file. New contract (red-team F1/S1): the drift
    # candidate is ALWAYS posted (real spend never silently dropped) AND an
    # advisory conflict is recorded (the possible duplicate is SURFACED, not a
    # silent double-count). Both rows count; the over-count is the irreducible
    # residual of an undecidable case, resolved in the SAFE direction.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-42.00", "AMAZON MKTPL ABC")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-42.00", "AMAZON MKTPL XYZ STORE")], "b.csv"))
    assert r["conflicts"] == 1                  # advisory conflict recorded (surfaced)
    assert r["possible_duplicates"] == 1        # reported, not silent
    with db.connect() as conn:
        posted = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0]
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE status='posted'"
        ).fetchone()[0]
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert posted == 2                          # BOTH posted → real spend always counts (S1)
    assert total == -8400                       # both -$42 included
    assert open_conf == 1                       # recoverable via the reconcile path


def test_walmart_reformat_csv_redownload_surfaced_not_doubled(data_dir, tmp_path):
    # S1 (unified): WALMART STORE → WALMART SUPERCENTER is a WF reformat of the SAME
    # CSV charge across two files. token-Jaccard is only 1/3 (0.33), so the old ≥0.5
    # floor MISSED it → silent double-count. The unified predicate (shared first
    # token, not a base+descriptor extension) catches it: BOTH post (total correct)
    # AND 1 advisory possible_duplicate surfaced.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-52.40", "WALMART STORE")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-52.40", "WALMART SUPERCENTER")], "b.csv"))
    assert r["inserted"] == 1                      # never silently dropped
    assert r["possible_duplicates"] == 1           # surfaced (was a silent double-count)
    assert r["conflicts"] == 1
    assert _count() == 2                            # both post → total stays correct
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE status='posted'"
        ).fetchone()[0]
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert total == -10480                          # both -$52.40 counted
    assert open_conf == 1


def test_walmart_reformat_cross_boundary_surfaced_not_doubled(data_dir, tmp_path):
    # F1 (unified): the SAME reformat pair WALMART STORE → WALMART SUPERCENTER, but
    # one side is an OFX FITID-less row (synthetic) and the other a real-FITID row.
    # Exact (account, fitid) dedup misses; the unified predicate fires regardless of
    # the synthetic/real boundary (different text, shared first token) → BOTH post +
    # surfaced, never a silent double-count.
    db.init_schema()
    importer.import_file(_ofx_opt(tmp_path, "dl1.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40",
         "name": "WALMART STORE"}]))                      # FITID-less → synthetic
    r = importer.import_file(_ofx_opt(tmp_path, "dl2.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40",
         "fitid": "BANKFIT1", "name": "WALMART SUPERCENTER"}]))   # real FITID
    assert r["inserted"] == 1
    assert r["possible_duplicates"] >= 1
    assert _count() == 2
    with db.connect() as conn:
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert open_conf == 1


def test_distinct_drift_twins_across_files_both_posted(data_dir, tmp_path):
    # S1: a genuinely-DISTINCT charge arriving in a LATER file that twins an
    # earlier one (same day/amount, similar merchant) must NEVER be dropped — BOTH
    # posted (counted), 1 advisory conflict (surfaced, dismissable). Total correct.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-25.00", "UBER TRIP HELP UBER COM")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-25.00", "UBER EATS HELP UBER COM")], "b.csv"))
    assert r["inserted"] == 1                   # never silently dropped
    assert _count() == 2                         # both posted/counted
    assert r["possible_duplicates"] == 1         # surfaced for review


def test_truncated_merchant_redownload_surfaced_not_doubled(data_dir, tmp_path):
    # S3: a CSV cut mid-final-description ("WALM") passes shape validation and
    # imports a corrupted merchant; on full re-download "WALMART" yields a
    # different synthetic FITID so exact dedup misses and token-Jaccard drift is
    # False (different first tokens are equal here but the PREFIX rule is what
    # catches it). Same day + amount + prefix relationship → drift candidate →
    # BOTH posted + 1 advisory conflict reported (never a silent double-count).
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-30.00", "WALM")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-30.00", "WALMART")], "b.csv"))
    assert r["possible_duplicates"] == 1         # caught + surfaced
    assert _count() == 2                          # both posted (real spend counts)
    with db.connect() as conn:
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert open_conf == 1                         # not 2 silent unrelated rows


def test_distinct_merchants_same_amount_not_flagged_as_drift(data_dir, tmp_path):
    # Two genuinely different merchants, same day + amount → NOT drift (no shared
    # first token) → both posted, no bogus conflict.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-20.00", "CHIPOTLE")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-20.00", "SHELL OIL")], "b.csv"))
    assert r["conflicts"] == 0
    assert _count() == 2


def test_identical_repeats_not_flagged_as_drift(data_dir, tmp_path):
    # Two identical same-day charges in ONE file → occurrence ordinal keeps them
    # distinct; the drift guard must not fire (identical text).
    db.init_schema()
    r = importer.import_file(_csv(tmp_path, [
        ("06/03/2026", "-5.00", "STARBUCKS"),
        ("06/03/2026", "-5.00", "STARBUCKS"),
    ]))
    assert r["conflicts"] == 0 and _count() == 2


def test_distinct_similar_charges_same_file_both_posted(data_dir, tmp_path):
    # F1: two GENUINELY-DISTINCT charges in ONE CSV sharing (account,date,amount)
    # with SIMILAR merchant text (shared first token, Jaccard >= 0.5) — e.g.
    # UBER TRIP vs UBER EATS — must BOTH post and count; NEITHER silently dropped
    # as drift. Drift applies only ACROSS runs, never within one statement.
    db.init_schema()
    r = importer.import_file(_csv(tmp_path, [
        ("06/03/2026", "-25.00", "UBER TRIP HELP UBER COM"),
        ("06/03/2026", "-25.00", "UBER EATS HELP UBER COM"),
    ]))
    assert r["conflicts"] == 0            # neither flagged
    assert r["possible_duplicates"] == 0  # zero advisory conflicts within one file
    assert r["inserted"] == 2
    with db.connect() as conn:
        posted = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0]
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE status='posted'"
        ).fetchone()[0]
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert posted == 2                    # BOTH count toward spend
    assert total == -5000                 # both -$25 charges included
    assert open_conf == 0                 # clean: no advisory conflict in-file


def test_two_target_stores_same_day_amount_both_posted(data_dir, tmp_path):
    # F1 (second case): two different Target stores, same day + amount, in one CSV
    # → both distinct, both posted.
    db.init_schema()
    r = importer.import_file(_csv(tmp_path, [
        ("06/03/2026", "-30.00", "TARGET STORE 1234 MPLS"),
        ("06/03/2026", "-30.00", "TARGET STORE 5678 EDINA"),
    ]))
    assert r["conflicts"] == 0 and r["inserted"] == 2 and _count() == 2


# ── S-1: _is_prefix fires only on MID-WORD truncation ────────────────────────
def test_midword_truncation_still_flagged_as_drift(data_dir, tmp_path):
    # WALM (mid-word cut) vs WALMART in a LATER file → still a drift candidate
    # (both posted, surfaced) — the S3 truncated-re-download case is preserved.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-30.00", "WALM")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-30.00", "WALMART")], "b.csv"))
    assert r["possible_duplicates"] == 1 and _count() == 2


@pytest.mark.parametrize("short_long", [
    ("COSTCO", "COSTCO GAS"),
    ("SHELL", "SHELL OIL"),
    ("AMZN", "AMZN MKTP"),
])
def test_word_boundary_same_chain_surfaced_not_silent(data_dir, tmp_path, short_long):
    # S10-1: a word-boundary extension (COSTCO vs COSTCO GAS) is token-for-token
    # INDISTINGUISHABLE from a WF reformat-by-appending-a-word (COSTCO vs COSTCO
    # WHOLESALE). Per the non-destructive contract we flag INCLUSIVELY: BOTH rows
    # always post (total never silently dropped) AND the possible duplicate is
    # SURFACED (advisory conflict). An over-flag is benign — the user dismisses it,
    # and keep_one's destructive delete is confirm()-gated; a SILENT double-count is
    # the bug we refuse to allow.
    db.init_schema()
    short, long = short_long
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-30.00", short)], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-30.00", long)], "b.csv"))
    assert r["inserted"] == 1                    # never silently dropped
    assert r["possible_duplicates"] >= 1         # SURFACED, not silent
    assert r["conflicts"] >= 1
    assert _count() == 2                          # both post → total stays correct
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE status='posted'"
        ).fetchone()[0]
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert total == -6000                         # both -$30 counted (never a silent drop)
    assert open_conf >= 1                         # recoverable via the reconcile path


def test_append_word_reformat_surfaced_not_silent(data_dir, tmp_path):
    # S10-1 lock: COSTCO -85.00 then (separate file) COSTCO WHOLESALE -85.00 is a WF
    # reformat that APPENDS a word. It slips past exact dedup (different merchant_norm)
    # AND used to slip past the possible-duplicate flag (word-boundary-extension
    # exclusion) → BOTH posted with 0 surfaced conflicts = a SILENT double-count.
    # FIX: both rows STILL post (total counts both, by design — undecidable) but the
    # possible duplicate is now SURFACED, never silent.
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-85.00", "COSTCO")], "a.csv"))
    r = importer.import_file(_csv(tmp_path, [("06/03/2026", "-85.00", "COSTCO WHOLESALE")], "b.csv"))
    assert r["inserted"] == 1                    # both rows post — never a silent drop
    assert r["possible_duplicates"] >= 1         # the fix: SURFACED, not silent
    assert r["conflicts"] >= 1
    assert _count() == 2
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE status='posted'"
        ).fetchone()[0]
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert total == -17000                        # both post (by design); now it is SURFACED
    assert open_conf >= 1                         # advisory conflict recorded


def test_is_prefix_unit_midword_vs_wordboundary():
    # Direct unit assertions on the tightened predicate (red-team S-1).
    assert importer._is_prefix("WALM", "WALMART") is True       # mid-word truncation
    assert importer._is_prefix("COSTCO", "COSTCO GAS") is False  # distinct descriptor
    assert importer._is_prefix("SHELL", "SHELL OIL") is False
    assert importer._is_prefix("AMZN", "AMZN MKTP") is False
    assert importer._is_prefix("WA", "WALMART") is False         # len<3 floor preserved


def test_shares_first_token_predicate():
    # The unified reformat signal: shared first token, no Jaccard floor.
    assert importer._shares_first_token("WALMART STORE", "WALMART SUPERCENTER") is True  # Jaccard 0.33
    assert importer._shares_first_token("AMAZON MKTPL ABC", "AMAZON MKTPL XYZ STORE") is True
    assert importer._shares_first_token("CHIPOTLE", "SHELL OIL") is False   # different first token
    assert importer._shares_first_token("", "WALMART") is False


# ── F2: cross-download synthetic↔real content-twin (same charge, two fitids) ──
# The same WF account's OFX export can drop a charge's <FITID> in one download
# (recovered → synthetic `csv:` FITID) and carry the real bank FITID in another.
# Both downloads share ONE real account_id, so exact (account, fitid) dedup misses
# and both post unless the cross-boundary content-twin guard fires (red-team F2).
def _ofx_opt(box, name, txns):
    """OFX in a fixed real account; each txn dict may OMIT 'fitid' (→ FITID-less,
    recovered to a synthetic FITID at import). Same account across calls."""
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
    text = (f"{header}\n<OFX>\n"
            "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260601120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
            "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>121000248<ACCTID>1234567890"
            "<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
            "<BANKTRANLIST><DTSTART>20260601<DTEND>20260630\n"
            f"{chr(10).join(parts)}\n</BANKTRANLIST>\n"
            "<LEDGERBAL><BALAMT>1000.00<DTASOF>20260630</LEDGERBAL>\n"
            "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n")
    p = box / name
    p.write_text(text)
    return p


def test_synthetic_then_real_same_charge_surfaced_not_doubled(data_dir, tmp_path):
    # dl1: WALMART -52.40 FITID-less (recovered → synthetic `csv:` FITID). dl2: the
    # SAME charge in the SAME account with a real bank FITID. Exact dedup misses
    # (different fitids); without the content-twin guard both post with 0 conflicts —
    # a silent double-count. New contract: both post AND possible_duplicates>=1.
    db.init_schema()
    importer.import_file(_ofx_opt(tmp_path, "dl1.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40", "name": "WALMART"}]))
    r = importer.import_file(_ofx_opt(tmp_path, "dl2.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40",
         "fitid": "BANKFIT1", "name": "WALMART"}]))
    assert r["inserted"] == 1                       # never silently dropped
    assert r["possible_duplicates"] >= 1            # surfaced, not silent
    assert _count() == 2                            # both posted
    with db.connect() as conn:
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert open_conf == 1


def test_real_then_synthetic_same_charge_surfaced_not_doubled(data_dir, tmp_path):
    # Reverse ordering: real-FITID first, then FITID-less (synthetic) of the same
    # charge in the same account → same surfaced-not-silent guarantee.
    db.init_schema()
    importer.import_file(_ofx_opt(tmp_path, "dl1.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40",
         "fitid": "BANKFIT1", "name": "WALMART"}]))
    r = importer.import_file(_ofx_opt(tmp_path, "dl2.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40", "name": "WALMART"}]))
    assert r["inserted"] == 1
    assert r["possible_duplicates"] >= 1
    assert _count() == 2
    with db.connect() as conn:
        open_conf = conn.execute(
            "SELECT COUNT(*) FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    assert open_conf == 1


def test_content_twin_keep_one_collapses_to_single_spend(data_dir, tmp_path):
    # F2: after the advisory conflict, reconcile keep_one collapses the over-count
    # so the spend is the single -52.40 (not -104.80).
    from local_budget import reconcile
    db.init_schema()
    importer.import_file(_ofx_opt(tmp_path, "dl1.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40", "name": "WALMART"}]))
    importer.import_file(_ofx_opt(tmp_path, "dl2.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-52.40",
         "fitid": "BANKFIT1", "name": "WALMART"}]))
    with db.connect() as conn:
        cid = conn.execute(
            "SELECT conflict_id FROM import_conflicts WHERE resolved = 0").fetchone()[0]
    reconcile.resolve(cid, "keep_one")
    with db.connect() as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE status='posted'"
        ).fetchone()[0]
    assert total == -5240                           # collapsed to one charge


def test_two_distinct_real_fitid_charges_not_flagged_cross_boundary(data_dir, tmp_path):
    # NEGATIVE: two DISTINCT real-FITID charges, same account/day/amount/merchant, in
    # separate files (both real → same side of the boundary) → both post, NO
    # content-twin flag (cross-boundary guard must not fire same-side).
    db.init_schema()
    importer.import_file(_ofx_opt(tmp_path, "a.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-30.00",
         "fitid": "REAL1", "name": "TARGET"}]))
    r = importer.import_file(_ofx_opt(tmp_path, "b.ofx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-30.00",
         "fitid": "REAL2", "name": "TARGET"}]))
    assert r["inserted"] == 1
    assert r["possible_duplicates"] == 0            # both real → not cross-boundary
    assert _count() == 2


def test_two_identical_csv_occurrences_not_flagged_cross_boundary(data_dir, tmp_path):
    # NEGATIVE: two identical CSV occurrences of a charge in ONE file → occurrence
    # ordinal keeps them distinct (both synthetic, same side) → both post, NO
    # content-twin flag.
    db.init_schema()
    r = importer.import_file(_csv(tmp_path, [
        ("06/03/2026", "-15.00", "STARBUCKS"),
        ("06/03/2026", "-15.00", "STARBUCKS"),
    ]))
    assert r["inserted"] == 2
    assert r["possible_duplicates"] == 0            # both synthetic → not cross-boundary
    assert _count() == 2


# ── S-2: per-file row-count reconciliation before disposal ───────────────────
def test_row_count_reconciles_for_mixed_file(data_dir, tmp_path):
    # A normal mixed file (inserts + a re-import skip + a fitid_collision) must
    # reconcile (seen == inserted + skipped + collisions_no_insert) and import
    # cleanly. Build the collision via OFX (stable FITID, materially changed).
    from ofx_fixtures import write_ofx
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "a.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-10.00", "fitid": "F1", "name": "SHELL"},
    ]))
    # Re-import: F1 unchanged → skip; F1' same fitid but changed amount → collision
    # (inserts nothing); F2 new → insert. seen=3 = inserted(1)+skipped(1)+collision(1).
    r = importer.import_file(write_ofx(tmp_path / "b.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-10.00", "fitid": "F1", "name": "SHELL"},
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-99.00", "fitid": "F1", "name": "SHELL"},
        {"trntype": "DEBIT", "dtposted": "20260604", "amount": "-20.00", "fitid": "F2", "name": "TARGET"},
    ]))
    assert r["rows_seen"] == 3
    assert r["inserted"] == 1 and r["skipped"] == 1 and r["conflicts"] == 1
    assert r["status"] == "success"


def test_row_count_mismatch_raises_and_rolls_back(data_dir, tmp_path, monkeypatch):
    # A forced accounting gap (a row that is neither inserted/skipped/collision)
    # must RAISE so the per-file transaction rolls back — nothing committed, file
    # not silently disposed (red-team S-2).
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("05/01/2026", "-7.00", "SEED")], "seed.csv"))
    before = _count()

    real = importer._ingest_txn

    def gappy(*a, **kw):
        real(*a, **kw)
        # Simulate a silent gap: a row that resolved to NOTHING (lost).
        return {"inserted": 0, "skipped": 0, "conflict": 0, "possible_dup": 0,
                "collision_no_insert": 0}

    monkeypatch.setattr(importer, "_ingest_txn", gappy)
    with pytest.raises(RuntimeError, match="reconciliation failed"):
        importer.import_file(_csv(tmp_path, [("06/03/2026", "-5.00", "STARBUCKS")], "x.csv"))
    assert _count() == before   # rolled back — nothing committed


def test_processor_prefix_redownload_not_silently_doubled(data_dir, tmp_path):
    # The SAME charge re-downloaded with vs without a payment-processor prefix
    # ("SQ *COFFEE SHOP" then "COFFEE SHOP") must collapse to one merchant_norm and
    # exact-dedup — NOT a silent double-count (red-team M11-1). Synthetic-FITID match
    # is the same charge regardless of raw payee text, so it dedups cleanly (no
    # spurious fitid_collision).
    db.init_schema()
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-5.00", "SQ *COFFEE SHOP")], "a.csv"))
    importer.import_file(_csv(tmp_path, [("06/03/2026", "-5.00", "COFFEE SHOP")], "b.csv"))
    assert _count() == 1                       # deduped, not double-counted
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0] == 0  # clean, no spurious conflict
