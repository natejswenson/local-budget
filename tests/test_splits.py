"""Transaction splits — the invariant, and everything that could break it.

Almost every test here is one question asked differently: **does the ledger
still add up?** A split that doesn't sum to its parent doesn't fail loudly, it
quietly invents or destroys money in every total downstream, so these assert
conservation directly rather than trusting the arithmetic.
"""
from __future__ import annotations

import sqlite3

import pytest

from local_budget import db, reports, splits


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


def _txn(c, txn_id, cents, category="Shopping", date="2026-07-15",
         merchant="AMAZON MKTPL"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, category, imported_at) "
        "VALUES (?,1,?,?,?, 'posted', ?, ?, '2026-07-01')",
        (txn_id, f"f{txn_id}", date, cents, merchant, category))


def _by_cat(c, month="2026-07"):
    return {r["category"]: int(r["total"]) for r in c.execute(
        "SELECT category, SUM(amount_cents) AS total FROM effective_txns "
        "WHERE status='posted' AND posted_date LIKE ? GROUP BY category",
        (f"{month}-%",))}


def _ledger_total(c):
    return c.execute(
        "SELECT COALESCE(SUM(amount_cents),0) s FROM transactions "
        "WHERE status='posted'").fetchone()["s"]


def _view_total(c):
    return c.execute(
        "SELECT COALESCE(SUM(amount_cents),0) s FROM effective_txns "
        "WHERE status='posted'").fetchone()["s"]


# ── allocation arithmetic ────────────────────────────────────────────────────
@pytest.mark.parametrize("charge,lines", [
    (-31337, [-4299, -5800, -1149, -3899, -1648]),      # charge below the item list total
    (-10000, [-3333, -3333, -3334]),                    # thirds, no clean division
    (-100, [-33, -33, -34]),
    (-1, [-1]),                                          # single line
    (-999, [-1, -1, -997]),                              # wildly uneven
    (-50000, [-10000] * 7),                              # remainder across equal lines
    (-12345, [-1] * 50),                                 # many tiny lines, big charge
])
def test_allocation_always_sums_to_the_charge_exactly(charge, lines):
    """The one thing allocation must never get wrong. Scaling each line
    independently leaves a remainder; if it isn't absorbed, money appears."""
    out = splits.allocate(charge, lines)
    assert sum(out) == charge
    assert len(out) == len(lines)


def test_allocation_puts_the_remainder_on_the_largest_line():
    """Smallest relative distortion. Also makes the drift deterministic rather
    than landing wherever iteration order happens to leave it."""
    out = splits.allocate(-1000, [-1, -1, -998])
    assert sum(out) == -1000
    assert abs(out[2]) == max(abs(x) for x in out)


def test_allocation_preserves_sign_and_order():
    out = splits.allocate(-9000, [-5000, -5000])
    assert all(x < 0 for x in out) and out == [-4500, -4500]


def test_allocation_refuses_the_undefined_cases():
    with pytest.raises(splits.SplitError, match="no lines"):
        splits.allocate(-100, [])
    with pytest.raises(splits.SplitError, match="sum to zero"):
        splits.allocate(-100, [0, 0])


# ── the invariant on write ───────────────────────────────────────────────────
def test_apply_refuses_a_set_that_does_not_sum_and_writes_nothing(conn):
    _txn(conn, 1, -10000)
    with pytest.raises(splits.SplitError, match="off by"):
        splits.apply(conn, 1, [{"amount_cents": -6000, "category": "Groceries"},
                               {"amount_cents": -3000, "category": "Shopping"}])
    assert conn.execute("SELECT COUNT(*) c FROM txn_splits").fetchone()["c"] == 0


def test_a_rejected_split_leaves_an_existing_one_intact(conn):
    """Validation runs BEFORE the delete. Otherwise a bad edit would clear a
    good split and then fail, losing work the user had already confirmed."""
    _txn(conn, 1, -10000)
    splits.apply(conn, 1, [{"amount_cents": -10000, "category": "Groceries"}])
    with pytest.raises(splits.SplitError):
        splits.apply(conn, 1, [{"amount_cents": -1, "category": "Shopping"}])
    got = splits.for_txn(conn, 1)
    assert len(got) == 1 and got[0]["category"] == "Groceries"


def test_apply_refuses_an_unknown_category(conn):
    _txn(conn, 1, -10000)
    with pytest.raises(splits.SplitError, match="unknown category"):
        splits.apply(conn, 1, [{"amount_cents": -10000, "category": "Nonsense"}])


def test_apply_refuses_an_empty_split(conn):
    _txn(conn, 1, -10000)
    with pytest.raises(splits.SplitError, match="at least one line"):
        splits.apply(conn, 1, [])


# ── conservation through the view ────────────────────────────────────────────
def test_a_split_moves_money_between_categories_and_nowhere_else(conn):
    _txn(conn, 1, -10000, category="Shopping")
    _txn(conn, 2, -5000, category="Groceries")
    before_total, before = _ledger_total(conn), _by_cat(conn)
    assert before == {"Shopping": -10000, "Groceries": -5000}

    splits.apply(conn, 1, [{"amount_cents": -6000, "category": "Groceries"},
                           {"amount_cents": -4000, "category": "Education"}])

    after = _by_cat(conn)
    assert after == {"Groceries": -11000, "Education": -4000}
    assert "Shopping" not in after, "the split category is fully reapportioned"
    # what Shopping lost, the others gained — exactly
    assert after["Groceries"] - before["Groceries"] == -6000
    assert after["Education"] == -4000
    # and the total is untouched. This is the assertion that matters most.
    assert _view_total(conn) == before_total == _ledger_total(conn)


def test_the_ledger_total_never_moves_however_a_charge_is_split(conn):
    _txn(conn, 1, -31337)
    total = _ledger_total(conn)
    for lines in (
        [{"amount_cents": -31337, "category": "Groceries"}],
        [{"amount_cents": -15668, "category": "Groceries"},
         {"amount_cents": -15669, "category": "Shopping"}],
        [{"amount_cents": -1, "category": "Groceries"},
         {"amount_cents": -31336, "category": "Health"}],
    ):
        splits.apply(conn, 1, lines)
        assert _view_total(conn) == total


def test_unsplit_restores_the_original_totals(conn):
    _txn(conn, 1, -10000, category="Shopping")
    before = _by_cat(conn)
    splits.apply(conn, 1, [{"amount_cents": -6000, "category": "Groceries"},
                           {"amount_cents": -4000, "category": "Health"}])
    assert _by_cat(conn) != before
    splits.unsplit(conn, 1)
    assert _by_cat(conn) == before


def test_unsplit_transactions_are_unaffected_by_the_view(conn):
    """The 99% case. Splits must be invisible to everything not split."""
    for i, (cents, cat) in enumerate([(-100, "Shopping"), (-250, "Groceries"),
                                      (5000, "Income"), (-99, "Health")], start=1):
        _txn(conn, i, cents, category=cat)
    via_view = _by_cat(conn)
    via_table = {r["category"]: int(r["total"]) for r in conn.execute(
        "SELECT category, SUM(amount_cents) AS total FROM transactions "
        "WHERE status='posted' GROUP BY category")}
    assert via_view == via_table


# ── the audit ────────────────────────────────────────────────────────────────
def test_verify_is_empty_on_a_healthy_db_and_finds_a_planted_break(conn):
    """The invariant is the one thing here that fails silently — nothing errors,
    reports just stop adding up. So it has to be checkable after the fact."""
    _txn(conn, 1, -10000)
    splits.apply(conn, 1, [{"amount_cents": -10000, "category": "Groceries"}])
    assert splits.verify(conn) == []
    # bypass apply() to simulate corruption from any other source
    conn.execute("UPDATE txn_splits SET amount_cents = -9000 WHERE txn_id = 1")
    bad = splits.verify(conn)
    assert len(bad) == 1 and bad[0]["txn_id"] == 1 and bad[0]["drift_cents"] == 1000


# ── reports read through the view ────────────────────────────────────────────
def test_month_summary_reflects_a_split(conn):
    _txn(conn, 1, -10000, category="Shopping")
    before = reports.month_summary("2026-07", conn=conn)
    splits.apply(conn, 1, [{"amount_cents": -7000, "category": "Groceries"},
                           {"amount_cents": -3000, "category": "Education"}])
    after = reports.month_summary("2026-07", conn=conn)
    assert after["spend_total_cents"] == before["spend_total_cents"], "total unchanged"
    assert after["spend_by_category"].get("Shopping") is None
    assert after["spend_by_category"]["Groceries"] == 7000
    assert after["spend_by_category"]["Education"] == 3000


def test_top_merchants_counts_a_split_transaction_once(conn):
    """COUNT(*) over the view would count a split charge once per line. The
    dollar total is conserved either way, so only the count exposes the bug."""
    _txn(conn, 1, -10000, merchant="AMAZON MKTPL")
    splits.apply(conn, 1, [{"amount_cents": -5000, "category": "Groceries"},
                           {"amount_cents": -3000, "category": "Health"},
                           {"amount_cents": -2000, "category": "Shopping"}])
    rows = reports.top_merchants(conn, "2026-07")
    amazon = next(r for r in rows if r["merchant"] == "AMAZON MKTPL")
    assert amazon["count"] == 1, "one charge, however many categories it hits"
    assert amazon["spent_cents"] == 10000


# ── the agent boundary ───────────────────────────────────────────────────────
def test_agent_can_write_splits_but_still_not_transactions(tmp_path, monkeypatch):
    """A split is a derived judgment, like transactions.category — the agent may
    make one. The imported bank row stays immutable."""
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution) VALUES (1,'T')")
        _txn(c, 1, -10000)

    with db.agent_connect(dbp, write=True) as c:
        c.execute("INSERT INTO txn_splits (txn_id, amount_cents, category, source, "
                  "created_at) VALUES (1, -10000, 'Groceries', 'manual', 'now')")
    with db.agent_connect(dbp) as c:
        assert c.execute("SELECT COUNT(*) c FROM txn_splits").fetchone()["c"] == 1
        assert c.execute("SELECT COUNT(*) c FROM effective_txns").fetchone()["c"] == 1

    with pytest.raises(sqlite3.DatabaseError):
        with db.agent_connect(dbp, write=True) as c:
            c.execute("UPDATE transactions SET amount_cents = -1 WHERE txn_id = 1")
