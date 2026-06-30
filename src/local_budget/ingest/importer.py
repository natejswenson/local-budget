"""Import orchestration (design §4.1) — atomic, two-layer dedup, agent.db rebuild.

Flow per file: parse → upsert accounts (auto-seed own_account) → for each txn:
mask/sanitize → string→cents → exact-dedup UPSERT (layer a) → near-duplicate
quarantine scan (layer b) → rules categorize → insert. The whole file imports
inside ONE budget.db transaction (all-or-nothing). On clean commit, agent.db is
atomically rebuilt. The raw export file is NOT retained (Decision B).
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import date as Date
from pathlib import Path

from .. import db, money, sanitize
from ..categorize import rules
from . import parse

# Near-duplicate knobs (design §3; tuned in OQ2).
NEAR_DUP_DAYS = 5
NEAR_DUP_INCREASE_PCT = 0.5
NEAR_DUP_INCREASE_CAP_CENTS = 5000


class ImportResult(dict):
    pass


def reap_orphaned_runs() -> int:
    """Finalize INTAKE-OWNED batch runs still 'in_progress' — orphaned from a prior
    crash between begin_batch_run and finish_batch_run (red-team S2). The caller
    MUST hold the intake lock.

    INVARIANT (red-team S2, tightened): only runs whose `source_name = 'intake'`
    are reaped — i.e. exactly the batch runs `begin_batch_run("intake")` opens and
    that the intake lock serializes. Because intake is serialized, any such
    in_progress run here is necessarily orphaned. A concurrent, NON-batched
    `budget import` (which does NOT hold the intake lock) opens its own in_progress
    run tagged with the FILE's basename as source_name, not 'intake'; that run is
    OUT OF SCOPE here and is never touched — so the reaper can never mis-finalize a
    live direct import mid-flight. (Previously this relied on the live import's row
    being WAL-hidden mid-transaction — fragile and undocumented; the source_name
    filter is the explicit, robust guard.)

    An orphan with committed transactions becomes 'success' (a reachable, undoable
    batch — last_import/undo can peel it back, so its committed rows are never
    stranded counting in reports yet unreachable by undo). An orphan with no
    committed rows becomes 'undone' (never a stray "last import" target). This only
    updates run-status bookkeeping; money stays integer cents. Returns the count
    finalized."""
    finalized = 0
    with db.connect() as conn:
        orphans = conn.execute(
            "SELECT run_id FROM import_runs WHERE status = 'in_progress' "
            "AND source_name = 'intake'"
        ).fetchall()
        for o in orphans:
            run_id = o["run_id"]
            n = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE import_run_id = ?", (run_id,)
            ).fetchone()[0]
            agg = conn.execute(
                "SELECT COUNT(*) AS seen, "
                "COALESCE(SUM(CASE WHEN status='posted' THEN 1 ELSE 0 END),0) AS inserted "
                "FROM transactions WHERE import_run_id = ?", (run_id,)
            ).fetchone()
            conflicts = conn.execute(
                "SELECT COUNT(*) FROM import_conflicts WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            status = "success" if n > 0 else "undone"
            _finish_run(conn, run_id, status, agg["seen"], agg["inserted"], 0, conflicts)
            finalized += 1
    return finalized


def begin_batch_run(source_name: str) -> int:
    """Open ONE import_runs row that several `import_file` calls can share, so a
    multi-file intake drop is ONE undoable batch (red-team S1). The row is created
    in its own committed transaction; per-file imports then tag rows with this
    run_id. Returns the run_id."""
    started = db.now_iso()
    with db.connect() as conn:
        run_id = _begin_run(conn, started, source_name)
    return run_id


def finish_batch_run(run_id: int, status: str, seen: int, inserted: int,
                     skipped: int, conflicts: int, error: str | None = None) -> None:
    """Finalize a shared batch run opened by `begin_batch_run` (red-team S1)."""
    with db.connect() as conn:
        _finish_run(conn, run_id, status, seen, inserted, skipped, conflicts, error)


def import_file(path: Path, detect_near_duplicates: bool = False,
                run_id: int | None = None, content_hash: str | None = None,
                source_filename: str | None = None) -> ImportResult:
    """Import an OFX/QFX/CSV file.

    `detect_near_duplicates` (default False): the near-duplicate QUARANTINE layer
    (layer b) catches different-FITID pending→posted churn, but on a BULK or
    historical import it over-flags legitimate recurring charges (same merchant,
    similar amount, within a few days). Exact FITID dedup (layer a) always runs
    and prevents true re-import duplicates. Enable this flag only for incremental
    re-imports of recent, overlapping statements where pending→posted churn is real.

    `run_id` (default None): when provided, this file's rows are tagged with the
    given SHARED batch run (opened via `begin_batch_run`) and no per-file run row
    is created/finalized here — the caller owns run bookkeeping so a multi-file
    drop is ONE undoable batch (red-team S1). The per-file budget.db transaction
    is still all-or-nothing, so a bad file rolls back only itself.

    `content_hash` (default None): the INTAKE path passes the file's content hash
    (+ `source_filename`); when present, import_file writes the inbox_files
    'imported' seen-record INSIDE this same budget.db transaction, just before
    commit, tagged with THIS run_id (red-team F7-1). So the commit atomically
    persists BOTH the transactions AND the seen-record under the same run that
    owns the rows: no crash window between commit and a separate record_seen, and
    on rollback neither persists (file stays unseen → correctly retried). Direct
    `budget import` passes no content_hash → no seen-record is written (current
    behavior, unaffected).
    """
    accounts = parse.parse_file(path)

    # Rows ofxparse discarded that we could NOT recover (malformed amount/date).
    # These never reach `seen` (they are not in pacct.txns), so they are separate
    # from the row-count reconcile; we surface the count non-silently (red-team F1).
    dropped_unparseable = sum(getattr(pa, "dropped_unparseable", 0) for pa in accounts)

    batched = run_id is not None
    seen = inserted = skipped = conflicts = possible_dups = 0
    collisions_no_insert = 0   # fitid_collision rows that inserted nothing (S-2)
    started = db.now_iso()
    with db.connect() as conn:
        rules.seed_builtin_rules(conn)
        from .. import merchants
        merchants.seed_builtin_aliases(conn)
        aliases = merchants.active_aliases(conn)   # built-in + cached, loaded once per file
        if not batched:
            run_id = _begin_run(conn, started, path.name)
        occ: dict = {}   # per-file (account, date, cents, merchant) → occurrence count
        # txn_ids inserted by THIS import_file call — drift detection excludes them
        # so two genuinely-distinct charges in ONE file are never flagged (red-team
        # F1). Rows from PRIOR files (same batch or earlier runs) remain drift-eligible.
        this_file_ids: set[int] = set()
        # Runs that own the rows this file exact-deduped against (true re-import).
        # Used ONLY when this file inserts 0 NEW rows, to bind the 'imported'
        # seen-record to the OWNING batch instead of this 0-row batch (red-team F7-1).
        skip_owner_runs: set[int] = set()
        try:
            for pacct in accounts:
                account_id = _upsert_account(conn, pacct)
                for ptxn in pacct.txns:
                    seen += 1
                    outcome = _ingest_txn(conn, account_id, ptxn, run_id, occ,
                                          this_file_ids, aliases, detect_near_duplicates)
                    inserted += outcome["inserted"]
                    skipped += outcome["skipped"]
                    conflicts += outcome["conflict"]
                    possible_dups += outcome["possible_dup"]
                    collisions_no_insert += outcome["collision_no_insert"]
                    owner = outcome.get("owning_run_id")
                    if owner is not None:
                        skip_owner_runs.add(owner)
            # Row-count reconciliation BEFORE the success commit (red-team S-2):
            # every parsed row resolves to exactly one outcome — inserted (incl.
            # drift/near-dup, which DO insert), skipped (true re-import), or a
            # fitid_collision that inserts nothing. If the books don't balance a
            # row was silently lost; RAISE so the per-file transaction rolls back
            # (all-or-nothing) and the file is recorded errored, never disposed.
            if seen != inserted + skipped + collisions_no_insert:
                raise RuntimeError(
                    f"row-count reconciliation failed: seen={seen} != "
                    f"inserted={inserted} + skipped={skipped} + "
                    f"collisions_no_insert={collisions_no_insert}"
                )
            if not batched:
                _finish_run(conn, run_id, "success", seen, inserted, skipped, conflicts)
            # Atomically record the 'imported' seen-record INSIDE this transaction.
            # Commit ⟺ seen-record — no crash window, and on rollback NEITHER
            # persists (file stays unseen → correctly retried) (red-team F7-1).
            # Only on the intake path (content_hash provided); direct `budget
            # import` writes no seen-record.
            #
            # Which run owns the seen-record:
            #  - normal case (this file inserted >=1 new row): THIS run_id owns the
            #    rows → bind to it. undo of this batch restores+forgets correctly.
            #  - crash-recovery case (0 new rows: every row exact-deduped against a
            #    SINGLE prior batch — the seen-record was lost in a crash and the file
            #    re-dropped): bind to the OWNING batch that already holds the rows, NOT
            #    this competing 0-row batch. Otherwise undo of the owning batch would
            #    delete the real spend yet leave the hash bound elsewhere ('imported',
            #    so already_seen) and the file stranded → permanent silent loss
            #    (reviewer's −$5 SHELL). Binding to the owner re-couples undo: it then
            #    deletes the rows AND forgets the hash AND restores the file, so a
            #    re-drop recovers the charge.
            #  - ambiguous 0-row case (rows owned by MULTIPLE prior batches, or none —
            #    e.g. all collisions): fall back to THIS run_id (no single safe owner).
            if content_hash is not None:
                seen_run_id = run_id
                if inserted == 0 and len(skip_owner_runs) == 1:
                    (only_owner,) = tuple(skip_owner_runs)
                    if only_owner is not None:
                        seen_run_id = only_owner
                from .. import inbox_adapter
                inbox_adapter.record_seen(
                    content_hash, source_filename or path.name, "imported",
                    seen_run_id, None, conn=conn)
        except Exception as e:  # noqa: BLE001 — record + roll back (all-or-nothing)
            conn.rollback()  # undoes this file's rows (and own run row, if any)
            if not batched:
                with db.connect() as c2:
                    # Fresh INSERT — the original run row was rolled back.
                    c2.execute(
                        "INSERT INTO import_runs (started_at, completed_at, status, "
                        "source_name, rows_seen, rows_inserted, rows_skipped, "
                        "rows_conflict, error_message) VALUES (?,?,?,?,?,?,?,?,?)",
                        (started, db.now_iso(), "error", path.name, seen, 0, 0, 0, str(e)),
                    )
            # Batched: the shared run row survives (separate transaction); the
            # caller decides batch status. This file's rows rolled back cleanly.
            raise

    return ImportResult(
        status="success", rows_seen=seen, inserted=inserted,
        skipped=skipped, conflicts=conflicts,
        possible_duplicates=possible_dups, dropped_rows=dropped_unparseable,
        run_id=run_id,
    )


# ── per-transaction ingest ───────────────────────────────────────────────────
def _synth_fitid(account_id: int, posted_date: str, cents: int, mnorm: str, occurrence: int) -> str:
    """Content-only, account-scoped synthetic FITID for CSV rows (red-team F1).

    Hashes (account, date, signed cents, merchant_norm, per-key occurrence ordinal)
    — NEVER a row index/file position. The occurrence ordinal (count of earlier
    same-key rows in the file) keeps two genuinely-distinct identical charges
    distinct (occ 0,1) while staying stable across overlapping re-downloads, so
    exact (account_id, fitid) dedup is correct for CSV."""
    raw = f"{account_id}|{posted_date}|{cents}|{mnorm}|{occurrence}"
    return "csv:" + hashlib.sha1(raw.encode()).hexdigest()[:24]  # noqa: S324 (not security)


def _ingest_txn(conn: sqlite3.Connection, account_id: int, ptxn: parse.ParsedTxn,
                run_id: int, occ: dict, this_file_ids: set[int],
                aliases: list | None = None,
                detect_near_duplicates: bool = False) -> dict:
    cents = money.cents_from_amount_str(ptxn.amount_str)
    mnorm = sanitize.merchant_norm(ptxn.payee, ptxn.memo)

    # CSV → compute the content-only synthetic FITID with a per-key occurrence
    # ordinal scoped to THIS file (occ dict). OFX/QFX → use the bank's stable FITID.
    if ptxn.synthetic_fitid:
        key = (account_id, ptxn.posted_date, cents, mnorm)
        n = occ.get(key, 0)
        occ[key] = n + 1
        fitid = _synth_fitid(account_id, ptxn.posted_date, cents, mnorm, n)
    else:
        fitid = ptxn.fitid

    raw_redacted = sanitize.redact_account_numbers(
        f"{fitid}|{ptxn.txn_type}|{ptxn.amount_str}|{ptxn.payee}|{ptxn.memo}"
    )

    # Layer (a): exact dedup on (account_id, fitid).
    existing = conn.execute(
        "SELECT txn_id, amount_cents, posted_date, payee, import_run_id FROM transactions "
        "WHERE account_id = ? AND fitid = ?", (account_id, fitid)
    ).fetchone()
    if existing:
        # A SYNTHETIC FITID already pins (account, date, cents, merchant_norm,
        # occurrence) by construction — a match IS the same charge re-downloaded, so
        # the raw payee text may legitimately differ ("SQ *COFFEE SHOP" vs "COFFEE
        # SHOP", a processor-prefix reformat that merchant_norm now collapses) WITHOUT
        # being a changed charge. Only a BANK FITID match whose amount/date/payee
        # materially changed is a real fitid_collision worth flagging.
        if ptxn.synthetic_fitid:
            same = True
        else:
            same = (existing["amount_cents"] == cents
                    and existing["posted_date"] == ptxn.posted_date
                    and (existing["payee"] or "") == (ptxn.payee or ""))
        if same:
            # True re-import. Surface the run that OWNS the already-present row so
            # import_file can, on a 0-new-row re-import (the crash-recovery case),
            # bind the seen-record to the OWNING batch rather than this competing
            # 0-row batch — keeping undo coupled to the real spend (red-team F7-1).
            return {"inserted": 0, "skipped": 1, "conflict": 0, "possible_dup": 0,
                    "collision_no_insert": 0, "owning_run_id": existing["import_run_id"]}
        # Materially changed -> fitid_collision, never silently overwrite.
        _record_conflict(conn, account_id, "fitid_collision", fitid,
                         existing["txn_id"], None, existing["amount_cents"],
                         existing["posted_date"], cents, ptxn.posted_date,
                         ptxn.payee, run_id)
        # This row inserts NOTHING (inserted=0, skipped=0); it is the one outcome
        # that is neither inserted nor skipped — tracked so import_file can
        # reconcile the per-file row count before disposal (red-team S-2).
        return {"inserted": 0, "skipped": 0, "conflict": 1, "possible_dup": 0,
                "collision_no_insert": 1}

    # UNIFIED possible-duplicate guard (red-team F1/S1/F2/S3). The exact
    # (account, fitid) key just missed. This single predicate runs for EVERY
    # incoming row — synthetic (CSV / FITID-less OFX) OR real — and looks for a
    # PRIOR-FILE posted row sharing (account, date, cents) that is plausibly the
    # SAME charge re-downloaded, even though exact dedup couldn't match it.
    #
    # The reformat-vs-distinct call is FUNDAMENTALLY UNDECIDABLE from
    # (account, date, cents, text) alone: WALMART STORE→WALMART SUPERCENTER (a WF
    # reformat of ONE charge) is structurally identical to UBER TRIP/UBER EATS
    # (two genuinely-distinct charges). So we NEVER silently drop and NEVER
    # silently overwrite: a possible duplicate is ALWAYS inserted 'posted' (real
    # spend always counts — both halves) AND SURFACED as an advisory near_duplicate
    # conflict + counted in `possible_dup`. Over-flagging is benign (total stays
    # correct, the user dismisses); under-flagging is the bug (silent double-count).
    #
    # Scope is per-FILE, not per-run: this_file_ids holds rows inserted by THIS
    # import_file call and is excluded, because two genuinely-distinct charges
    # always arrive in the SAME file. Rows from a PRIOR file (same batch OR an
    # earlier run) remain eligible → advisory flag only.
    #
    # Residual (undecidable, NOT fixed by design): a reformat that changes the
    # FIRST normalized token entirely (e.g. "WALMART"→"WAL MART STORE") shares no
    # first token, so it is the one narrow case still not caught here.
    drift_of = _find_possible_duplicate(
        conn, account_id, ptxn.posted_date, cents, mnorm,
        ptxn.synthetic_fitid, this_file_ids)

    # Layer (b): near-duplicate scan against existing POSTED rows — OPT-IN only
    # (over-flags recurring charges on bulk/historical imports). This layer DOES
    # quarantine (status='conflict') for explicit pending→posted churn handling.
    cand = (_find_near_duplicate(conn, account_id, cents, ptxn.posted_date, mnorm)
            if detect_near_duplicates else None)

    # Near-duplicate (opt-in) quarantines; drift NEVER does (always posted).
    status = "conflict" if cand else "posted"

    category, subcat, csource = rules.categorize(mnorm, ptxn.txn_type, cents, conn)
    # Canonical vendor identity (offline, deterministic — built-in + cached aliases).
    from .. import merchants
    al = aliases or []
    # Canonical column is scoped to Subscriptions rows only — applying brand aliases
    # to retail/travel/dining rows would over-collapse DISTINCT vendors across
    # categories in top_merchants. NULL when un-aliased OR not a Subscriptions row.
    canonical = merchants.canonical_alias(mnorm, al) if category == "Subscriptions" else None
    # A new Subscriptions row defaults its subcategory to the DISPLAY vendor name (alias
    # or friendly) so the same service collapses to one budgetable sub per vendor.
    if category == "Subscriptions" and not subcat:
        subcat = merchants.canonical_merchant(mnorm, al)
    txn_id = _insert_txn(conn, account_id, ptxn, fitid, cents, mnorm, status,
                         category, subcat, csource, raw_redacted, run_id, canonical)
    this_file_ids.add(txn_id)

    if cand:
        _record_conflict(conn, account_id, "near_duplicate", fitid,
                         cand["txn_id"], txn_id, cand["amount_cents"],
                         cand["posted_date"], cents, ptxn.posted_date,
                         ptxn.payee, run_id)
        return {"inserted": 1, "skipped": 0, "conflict": 1, "possible_dup": 0,
                "collision_no_insert": 0}

    if drift_of:
        # Both rows are POSTED (counted); surface an advisory near_duplicate
        # conflict so the possible re-download is reviewable/dismissable via the
        # existing reconcile path — never a silent double-count.
        _record_conflict(conn, account_id, "near_duplicate", fitid,
                         drift_of["txn_id"], txn_id, drift_of["amount_cents"],
                         drift_of["posted_date"], cents, ptxn.posted_date,
                         ptxn.payee, run_id)
        return {"inserted": 1, "skipped": 0, "conflict": 1, "possible_dup": 1,
                "collision_no_insert": 0}

    return {"inserted": 1, "skipped": 0, "conflict": 0, "possible_dup": 0,
            "collision_no_insert": 0}


def _find_possible_duplicate(conn, account_id, posted_date, cents, mnorm,  # noqa: ANN001
                             incoming_synthetic, this_file_ids):
    """UNIFIED post-exact-dedup-miss "possible duplicate" check (red-team F1/S1/F2/S3).

    Runs for EVERY incoming row (synthetic OR real) whose exact (account, fitid)
    dedup missed. Searches PRIOR-FILE posted rows with the same
    (account_id, posted_date, amount_cents) and classifies a prior row as a
    possible duplicate iff EITHER:

      (a) IDENTICAL merchant_norm AND the two rows are on OPPOSITE sides of the
          synthetic/real FITID boundary (incoming synthetic ↔ existing real, or
          vice versa). This is the cross-download identity change (F2): the SAME
          charge arrives FITID-less (synthetic `csv:`) in one download and with the
          real bank FITID in another, so exact dedup misses. Identical-text rows on
          the SAME side stay DISTINCT (two real-FITID OFX charges; two synthetic CSV
          occurrences already separated by the occurrence ordinal) — preserving the
          negative tests; OR

      (b) DIFFERENT merchant_norm that SHARES the first token. This is the reformat
          case (F1/S1): WALMART STORE↔WALMART SUPERCENTER (token-Jaccard only 0.33,
          so a Jaccard floor would MISS it — sharing the first token is the right
          signal), regardless of the synthetic/real boundary. We flag INCLUSIVELY:
          word-boundary extensions (COSTCO vs COSTCO WHOLESALE) ARE flagged too,
          because a WF reformat-by-appending-a-word is token-for-token
          indistinguishable from a distinct same-chain charge (COSTCO vs COSTCO GAS).
          Per the non-destructive contract both rows still post (total always
          correct) and an advisory conflict surfaces — an over-flag is benign, a
          SILENT double-count is the bug we refuse to allow (S10-1).

    Rows inserted by the CURRENT import_file call (this_file_ids) are EXCLUDED: two
    genuinely-distinct same-day, same-amount charges always arrive in the SAME file,
    so flagging within a file would mis-surface a real distinct charge. Scope is
    per-FILE, never per-run, so a multi-file drop sharing one batch run_id still
    flags a cross-file reformat.

    On a match the CALLER posts the incoming row anyway (never drops) and records an
    advisory near_duplicate conflict — the over-flag is benign (total stays correct),
    a silent double-count is the bug we refuse to allow.

    Residual (undecidable, NOT fixed): a reformat that changes the FIRST normalized
    token entirely shares no first token and so is not caught here."""
    rows = conn.execute(
        "SELECT txn_id, amount_cents, posted_date, merchant_norm, fitid "
        "FROM transactions "
        "WHERE account_id = ? AND posted_date = ? AND amount_cents = ? "
        "AND status = 'posted'",
        (account_id, posted_date, cents),
    ).fetchall()
    for r in rows:
        if r["txn_id"] in this_file_ids:
            continue
        other = r["merchant_norm"] or ""
        if other == mnorm:
            # (a) identical text → only across the synthetic/real boundary.
            existing_synthetic = (r["fitid"] or "").startswith("csv:")
            if existing_synthetic != incoming_synthetic:
                return r
            continue
        # (b) different text → reformat heuristic (shared first token).
        # Boundary-agnostic. NOTE (S10-1): we deliberately do NOT exclude
        # word-boundary extensions here. `COSTCO`→`COSTCO WHOLESALE` (a WF reformat
        # that appends a word) is token-for-token INDISTINGUISHABLE from a genuinely
        # distinct same-chain charge (`COSTCO`/`COSTCO GAS`). Suppressing it traded a
        # benign advisory for a SILENT double-count. Per the non-destructive
        # contract, we flag INCLUSIVELY: BOTH rows still post (total always correct)
        # and an advisory near_duplicate conflict is surfaced; the user dismisses
        # false positives (the destructive delete is confirm()-gated).
        if _shares_first_token(other, mnorm) or _is_prefix(other, mnorm):
            return r
    return None


def _shares_first_token(a: str, b: str) -> bool:
    """Two DIFFERENT merchant strings that share their first normalized token —
    the reformat signal (red-team F1/S1). Deterministic.

    A token-Jaccard floor (the old ≥0.5) MISSES real reformats:
    `WALMART STORE`→`WALMART SUPERCENTER` has Jaccard 1/3 ≈ 0.33 yet is plainly the
    SAME charge re-described by WF. Sharing the first token is the correct, inclusive
    signal — paired with the always-post/always-surface contract, an over-flag is
    benign while a silent double-count is prevented. The case this can NOT catch
    (undecidable) is a reformat that changes the FIRST token entirely."""
    ta, tb = a.split(), b.split()
    return bool(ta) and bool(tb) and ta[0] == tb[0]


def _is_prefix(a: str, b: str) -> bool:
    """A MID-WORD truncation of the other (red-team S3, tightened by S-1): a CSV
    cut mid-final-description (e.g. "WALM" vs "WALMART") yields a truncated merchant
    that continues the SAME token in the complete one. We fire ONLY when the longer
    string continues the prefix's final token — i.e. the char immediately after the
    prefix is NOT whitespace. So WALM/WALMART → True (mid-word), but COSTCO/COSTCO
    GAS, SHELL/SHELL OIL, AMZN/AMZN MKTP → False (a distinct space-separated
    descriptor, a real different charge), preventing a same-chain false flag that
    would pollute the surfaced duplicate list and let keep_one delete a real charge."""
    if a == b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 3 or not long.startswith(short):
        return False
    # Word-boundary extension (the longer string starts a NEW token) is a distinct
    # descriptor, not a truncation → not a drift candidate.
    return not long[len(short):len(short) + 1].isspace()


def _find_near_duplicate(conn, account_id, cents, posted_date, mnorm):  # noqa: ANN001
    """Different-FITID pending→posted churn (design §3). Same account, date
    within N days, merchant match, amount equal OR a bounded pre-auth→post
    increase. Only matches existing status='posted' rows."""
    lo = (Date.fromisoformat(posted_date) - _td(NEAR_DUP_DAYS)).isoformat()
    hi = (Date.fromisoformat(posted_date) + _td(NEAR_DUP_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT txn_id, amount_cents, posted_date FROM transactions "
        "WHERE account_id = ? AND status = 'posted' AND merchant_norm = ? "
        "AND posted_date >= ? AND posted_date <= ?",
        (account_id, mnorm, lo, hi),
    ).fetchall()
    for r in rows:
        if _amounts_near(r["amount_cents"], cents):
            return r
    return None


def _amounts_near(existing: int, incoming: int) -> bool:
    if existing == incoming:
        return True
    if (existing < 0) != (incoming < 0):  # opposite signs are not a pre-auth/post pair
        return False
    a, b = abs(existing), abs(incoming)
    if b < a:  # posted should be >= pre-auth
        return False
    delta = b - a
    return delta <= max(int(NEAR_DUP_INCREASE_PCT * a), NEAR_DUP_INCREASE_CAP_CENTS)


# ── account upsert (auto-seed own_account) ───────────────────────────────────
def _upsert_account(conn: sqlite3.Connection, pacct: parse.ParsedAccount) -> int:
    h = db.acct_hash(pacct.bankid, pacct.acctid)
    row = conn.execute("SELECT account_id FROM accounts WHERE acct_hash = ?", (h,)).fetchone()
    if row:
        return row["account_id"]
    last4 = (pacct.acctid or "")[-4:]
    conn.execute(
        "INSERT INTO accounts (institution, acct_type, acct_last4, acct_hash, "
        "own_account, created_at) VALUES (?, ?, ?, ?, 1, ?)",
        (pacct.institution, pacct.acct_type, last4, h, db.now_iso()),
    )
    return conn.execute("SELECT account_id FROM accounts WHERE acct_hash = ?", (h,)).fetchone()["account_id"]


def _insert_txn(conn, account_id, ptxn, fitid, cents, mnorm, status, category, subcat, csource, raw_redacted, run_id, canonical=None):  # noqa: ANN001
    cur = conn.execute(
        "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, "
        "status, txn_type, payee, memo, merchant_norm, category, subcategory, "
        "category_source, raw_ofx, imported_at, import_run_id, canonical_merchant) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, fitid, ptxn.posted_date, cents, status, ptxn.txn_type,
         ptxn.payee, ptxn.memo, mnorm, category, subcat, csource, raw_redacted,
         db.now_iso(), run_id, canonical),
    )
    return cur.lastrowid


# ── conflicts + run bookkeeping ──────────────────────────────────────────────
def _record_conflict(conn, account_id, kind, fitid, existing_txn_id, incoming_txn_id,
                     existing_amount, existing_date, incoming_amount, incoming_date,
                     incoming_payee, run_id):  # noqa: ANN001
    conn.execute(
        "INSERT INTO import_conflicts (account_id, kind, fitid, existing_txn_id, "
        "incoming_txn_id, existing_amount_cents, existing_posted_date, "
        "incoming_amount_cents, incoming_posted_date, incoming_payee, run_id, "
        "detected_at, resolved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (account_id, kind, fitid, existing_txn_id, incoming_txn_id, existing_amount,
         existing_date, incoming_amount, incoming_date, incoming_payee, run_id, db.now_iso()),
    )


def _begin_run(conn, started, name) -> int:  # noqa: ANN001
    cur = conn.execute(
        "INSERT INTO import_runs (started_at, status, source_name) VALUES (?, 'in_progress', ?)",
        (started, Path(name).name),  # basename only (§7.9)
    )
    return cur.lastrowid


def _finish_run(conn, run_id, status, seen, inserted, skipped, conflicts, error=None):  # noqa: ANN001
    conn.execute(
        "UPDATE import_runs SET completed_at = ?, status = ?, rows_seen = ?, "
        "rows_inserted = ?, rows_skipped = ?, rows_conflict = ?, error_message = ? "
        "WHERE run_id = ?",
        (db.now_iso(), status, seen, inserted, skipped, conflicts, error, run_id),
    )


def _td(days: int):
    from datetime import timedelta
    return timedelta(days=days)
