import threading
import sqlite3
import pytest
from local_budget import db, splits
from .test_existing_writes import transaction


def test_split_validation_reserves_parent(plaid_db, monkeypatch):
    with db.connect(plaid_db) as conn:
        txn = transaction(conn)
    validated = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    blocked = threading.Event()
    corrected = threading.Event()
    failures = []
    original = splits._validate

    def pause(conn, tid, lines):
        result = original(conn, tid, lines)
        validated.set()
        assert release.wait(5)
        assert not corrected.is_set()
        return result

    monkeypatch.setattr(splits, "_validate", pause)

    def writer():
        try:
            with db.connect(plaid_db) as conn:
                splits.apply(
                    conn, txn, [{"amount_cents": -100, "category": "Groceries"}]
                )
        except BaseException as e:
            failures.append(e)
            validated.set()

    def correction():
        try:
            with db.connect(plaid_db) as conn:
                attempted.set()
                conn.execute("PRAGMA busy_timeout=0")
                try:
                    db.begin_immediate(conn)
                except sqlite3.OperationalError as e:
                    assert "locked" in str(e)
                    blocked.set()
                    assert release.wait(5)
                    conn.execute("PRAGMA busy_timeout=5000")
                    db.begin_immediate(conn)
                conn.execute(
                    "UPDATE transactions SET amount_cents=-200 WHERE txn_id=?", (txn,)
                )
                corrected.set()
        except BaseException as e:
            failures.append(e)

    a = threading.Thread(target=writer)
    b = threading.Thread(target=correction)
    a.start()
    try:
        assert validated.wait(5)
        b.start()
        assert attempted.wait(5)
        assert blocked.wait(5), "correction interleaved during split validation"
    finally:
        release.set()
        a.join(5)
        if b.ident:
            b.join(5)
    assert not a.is_alive() and not b.is_alive()
    assert not failures
    assert corrected.is_set()


def test_correction_before_split_uses_new_amount(plaid_db, monkeypatch):
    with db.connect(plaid_db) as conn:
        txn = transaction(conn)
    attempting = threading.Event()
    failures = []
    original = db.begin_immediate

    def reserve(conn):
        attempting.set()
        original(conn)

    monkeypatch.setattr(db, "begin_immediate", reserve)

    def writer():
        try:
            with db.connect(plaid_db) as conn:
                with pytest.raises(splits.SplitError, match="transaction .* is -200"):
                    splits.apply(
                        conn, txn, [{"amount_cents": -100, "category": "Groceries"}]
                    )
                assert (
                    conn.execute("SELECT COUNT(*) FROM txn_splits").fetchone()[0] == 0
                )
        except BaseException as e:
            failures.append(e)

    thread = threading.Thread(target=writer)
    try:
        with db.connect(plaid_db) as conn:
            original(conn)
            conn.execute(
                "UPDATE transactions SET amount_cents=-200 WHERE txn_id=?", (txn,)
            )
            thread.start()
            assert attempting.wait(5)
        # Correction commits while the competing writer is entering its reservation.
    finally:
        if thread.ident:
            thread.join(5)
    assert not thread.is_alive()
    assert not failures
