"""Import + two-layer dedup + categorization (design §4.1, AC1, I7/I8/I8b/I8d/I8e/I9)."""
from __future__ import annotations

from local_budget import db
from local_budget.ingest import importer

from ofx_fixtures import write_ofx


def _import(tmp_path, txns, name="wf.qfx", detect_dups=False, **kw):
    db.init_schema()
    p = write_ofx(tmp_path / name, txns, **kw)
    return importer.import_file(p, detect_near_duplicates=detect_dups)


def _rows(status=None):
    sql = "SELECT * FROM transactions"
    if status:
        sql += f" WHERE status = '{status}'"
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


GROCERY = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "fitid": "F1", "name": "WALMART STORE 1234"}


def test_basic_import(data_dir, tmp_path):
    r = _import(tmp_path, [GROCERY])
    assert r["inserted"] == 1 and r["conflicts"] == 0
    rows = _rows()
    assert rows[0]["amount_cents"] == -5000
    assert rows[0]["category"] == "Groceries"   # builtin walmart rule
    assert rows[0]["status"] == "posted"


def test_account_auto_seeds_own_account(data_dir, tmp_path):
    _import(tmp_path, [GROCERY])
    with db.connect() as conn:
        own = conn.execute("SELECT own_account FROM accounts").fetchone()[0]
    assert own == 1


def test_identical_reimport_is_noop(data_dir, tmp_path):
    # I7 / AC1: re-importing the identical file = 0 new, 0 conflicts.
    _import(tmp_path, [GROCERY])
    r2 = _import(tmp_path, [GROCERY], name="wf2.qfx")
    assert r2["inserted"] == 0 and r2["skipped"] == 1 and r2["conflicts"] == 0
    assert len(_rows()) == 1


def test_overlapping_import_adds_only_new(data_dir, tmp_path):
    # I8: overlapping file imports only the new FITID.
    _import(tmp_path, [GROCERY])
    new = {"trntype": "DEBIT", "dtposted": "20260607", "amount": "-12.00", "fitid": "F2", "name": "SHELL OIL"}
    r2 = _import(tmp_path, [GROCERY, new], name="wf2.qfx")
    assert r2["inserted"] == 1 and r2["skipped"] == 1
    assert len(_rows()) == 2


def test_same_fitid_changed_amount_is_conflict(data_dir, tmp_path):
    # I8b: same FITID, materially different amount -> fitid_collision, not overwrite.
    _import(tmp_path, [GROCERY])
    changed = {**GROCERY, "amount": "-99.00"}
    r2 = _import(tmp_path, [changed], name="wf2.qfx")
    assert r2["conflicts"] == 1 and r2["inserted"] == 0
    rows = _rows()
    assert len(rows) == 1 and rows[0]["amount_cents"] == -5000  # original untouched
    with db.connect() as conn:
        c = conn.execute("SELECT kind FROM import_conflicts").fetchone()
    assert c["kind"] == "fitid_collision"


def test_pending_to_posted_is_quarantined(data_dir, tmp_path):
    # I8d / AC1: different FITID, same merchant, within 5d, bounded increase ->
    # quarantined near_duplicate, excluded from posted.
    pre = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "fitid": "PRE", "name": "SHELL OIL"}
    _import(tmp_path, [pre])
    posted = {"trntype": "DEBIT", "dtposted": "20260606", "amount": "-73.20", "fitid": "POST", "name": "SHELL OIL"}
    r2 = _import(tmp_path, [posted], name="wf2.qfx", detect_dups=True)
    assert r2["conflicts"] == 1
    assert len(_rows(status="posted")) == 1     # only the pre-auth counts
    assert len(_rows(status="conflict")) == 1   # posted row quarantined
    with db.connect() as conn:
        assert conn.execute("SELECT kind FROM import_conflicts").fetchone()["kind"] == "near_duplicate"


def test_identical_same_day_charges_both_flagged_not_dropped(data_dir, tmp_path):
    # I8e: two identical same-day same-merchant charges -> 2nd quarantined,
    # never auto-dropped (user marks distinct later).
    c1 = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-4.50", "fitid": "C1", "name": "VOLT CAFE"}
    c2 = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-4.50", "fitid": "C2", "name": "VOLT CAFE"}
    r = _import(tmp_path, [c1, c2], detect_dups=True)
    assert r["inserted"] == 2          # nothing dropped
    assert r["conflicts"] == 1         # second flagged for review
    assert len(_rows(status="conflict")) == 1


def test_investments_counts_as_spend_even_on_xfer(data_dir, tmp_path):
    # I9: Investments wins over Transfer and is spend even when TRNTYPE=XFER.
    inv = {"trntype": "XFER", "dtposted": "20260605", "amount": "-100.00", "fitid": "N1", "name": "529 PLAN CONTRIB"}
    _import(tmp_path, [inv])
    rows = _rows()
    assert rows[0]["category"] == "Investments"


def test_card_payment_is_transfer(data_dir, tmp_path):
    pay = {"trntype": "XFER", "dtposted": "20260605", "amount": "-200.00", "fitid": "P1", "name": "WF CREDIT CARD PAYMENT"}
    _import(tmp_path, [pay])
    assert _rows()[0]["category"] == "Transfer"


def test_credit_defaults_to_income(data_dir, tmp_path):
    pay = {"trntype": "CREDIT", "dtposted": "20260601", "amount": "2000.00", "fitid": "INC", "name": "ACME PAYROLL"}
    _import(tmp_path, [pay])
    assert _rows()[0]["category"] == "Income"


def test_sub_cent_amount_fails_whole_import(data_dir, tmp_path):
    # All-or-nothing: a >2-decimal amount raises and rolls back the import.
    bad = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-5.005", "fitid": "B1", "name": "ODD"}
    db.init_schema()
    p = write_ofx(tmp_path / "bad.qfx", [GROCERY, bad])
    import pytest
    with pytest.raises(Exception):
        importer.import_file(p)
    assert _rows() == []  # nothing committed
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM import_runs ORDER BY run_id DESC LIMIT 1").fetchone()["status"] == "error"


def test_quarantined_excluded_from_posted(data_dir, tmp_path):
    pre = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "fitid": "PRE", "name": "SHELL OIL"}
    _import(tmp_path, [pre])
    posted = {"trntype": "DEBIT", "dtposted": "20260606", "amount": "-73.20", "fitid": "POST", "name": "SHELL OIL"}
    _import(tmp_path, [posted], name="wf2.qfx", detect_dups=True)
    assert len(_rows(status="posted")) == 1  # quarantined row excluded from posted
