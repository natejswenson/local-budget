"""Phase-3 write tools — each mutates through db.agent_connect(write=True) so the
column-level authorizer is in the write path (design §1/§3). Tests exercise the
TOOL ENTRY POINTS (not the authorizer in isolation) per design-gate F1.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from local_budget import budgets as budgets_mod
from local_budget import categories, db
from local_budget.agent import tools


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir):
    """Redirect the data dir to a per-test tmp path (conftest's data_dir is not
    autouse) so init_schema/connect hit an isolated budget.db."""
    yield


@pytest.fixture(autouse=True)
def no_network_egress():
    """Override the conftest socket-block: these tests drive async handlers via
    asyncio (needs the self-pipe socket) but do no network I/O."""
    yield


def _call(name, args):
    return asyncio.run(tools.SPEC_BY_NAME[name].handler(args))


def _seed():
    db.init_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (account_id, acct_last4, acct_hash, created_at) "
                     "VALUES (1,'1234','h',?)", (db.now_iso(),))
        for fitid, cat, sub, mnorm in [("G1", "Uncategorized", None, "WALMART"),
                                       ("S1", "Subscriptions", None, "NETFLIX")]:
            conn.execute(
                "INSERT INTO transactions (account_id, fitid, posted_date, amount_cents, status, "
                "txn_type, payee, memo, merchant_norm, category, subcategory, raw_ofx, imported_at) "
                "VALUES (1, ?, '2026-06-03', -5000, 'posted', 'DEBIT', ?, 'm', ?, ?, ?, 'raw', ?)",
                (fitid, mnorm, mnorm, cat, sub, db.now_iso()))


def _cat_of(merchant):
    with db.connect() as conn:
        return conn.execute("SELECT category FROM transactions WHERE merchant_norm=?",
                            (merchant,)).fetchone()[0]


# ── each write tool PERSISTS through the guarded write connection ──
def test_set_merchant_category_persists():
    _seed()
    r = _call("set_merchant_category", {"merchant_norm": "WALMART", "category": "Groceries"})
    assert r["ok"] and _cat_of("WALMART") == "Groceries"


def test_set_txn_category_persists():
    _seed()
    with db.connect() as c:
        tid = c.execute("SELECT txn_id FROM transactions WHERE merchant_norm='WALMART'").fetchone()[0]
    assert _call("set_txn_category", {"txn_id": tid, "category": "Dining Out"})["ok"]
    assert _cat_of("WALMART") == "Dining Out"


def test_add_and_remove_custom_category():
    _seed()
    assert _call("add_custom_category", {"name": "Hobbies"})["ok"]
    assert "Hobbies" in categories.all_categories()
    # remove_category merges Hobbies into Groceries (move any of its rows, then hide it)
    assert _call("remove_category", {"name": "Hobbies", "merge_into": "Groceries"})["ok"]
    assert "Hobbies" not in categories.spend_categories()


def test_set_and_clear_budget_limit():
    _seed()
    assert _call("set_budget_limit", {"category": "Groceries", "amount_cents": 40000})["ok"]
    with db.connect() as conn:
        assert any(c == "Groceries" for (c, _s) in budgets_mod.active_limits(conn))
    assert _call("clear_budget_limit", {"category": "Groceries"})["ok"]
    with db.connect() as conn:
        assert not any(c == "Groceries" for (c, _s) in budgets_mod.active_limits(conn))


def test_set_expected_income_writes_whitelisted_setting():
    _seed()
    assert _call("set_expected_income", {"cents": 500000})["ok"]
    assert db.get_setting(budgets_mod.EXPECTED_INCOME_KEY) == "500000"


def test_split_subscriptions_runs_under_authorizer():
    """Returns ok (not an error) — proving it runs under agent_connect(write=True)
    AND that the merchant_aliases seed-write was dropped (it would abort otherwise)."""
    _seed()
    r = _call("split_subscriptions", {})
    assert r["ok"] and "error" not in r
    with db.connect() as conn:
        assert conn.execute("SELECT subcategory FROM transactions WHERE merchant_norm='NETFLIX'"
                            ).fetchone()[0]  # got a subcategory


def test_bad_input_returns_error_not_crash():
    _seed()
    r = _call("set_merchant_category", {"merchant_norm": "WALMART", "category": "NotARealCategory"})
    assert "error" in r and _cat_of("WALMART") == "Uncategorized"  # nothing committed


# ── authorizer IS in the write path (entry-point, not in-isolation) ──
def test_write_tool_opens_guarded_write_connection(monkeypatch):
    """The tool must open db.agent_connect(write=True) — NOT db.connect() — so the
    authorizer gates the write. Spy proves the guarded path is taken."""
    _seed()
    seen = []
    real = db.agent_connect
    def spy(*a, write=False, **k):
        seen.append(write)
        return real(*a, write=write, **k)
    monkeypatch.setattr(db, "agent_connect", spy)
    _call("set_budget_limit", {"category": "Groceries", "amount_cents": 30000})
    assert seen == [True]  # opened exactly once, with write=True


def test_guarded_write_conn_denies_status_and_unlisted_table():
    """The connection the write tools use cannot mutate status or a non-allowlisted
    table (the firewall the entry points run behind)."""
    _seed()
    with db.agent_connect(write=True) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("UPDATE transactions SET status='void' WHERE txn_id=1")
    with db.agent_connect(write=True) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO merchant_aliases (pattern, canonical, source) VALUES ('a','b','m')")


# ── save_brief: file-backed, period-validated, path-confined ──
def test_save_brief_valid_period_writes():
    _seed()
    r = _call("save_brief", {"period": "2026-06", "markdown": "# June\nspent $50"})
    assert r["ok"]
    from local_budget import paths
    assert (paths.briefings_dir() / "2026-06.md").read_text().startswith("# June")


@pytest.mark.parametrize("bad", ["../../etc/passwd", "2026/06", "..", "2026-6", "../escape"])
def test_save_brief_rejects_escaping_period(bad):
    _seed()
    from local_budget import paths
    before = set(paths.briefings_dir().glob("*"))
    r = _call("save_brief", {"period": bad, "markdown": "x"})
    assert "error" in r
    assert set(paths.briefings_dir().glob("*")) == before  # nothing written


def test_all_write_tools_registered_and_schemas_serialize():
    import json
    writes = {"set_merchant_category", "set_txn_category", "add_custom_category", "remove_category",
              "set_budget_limit", "clear_budget_limit", "set_expected_income", "split_subscriptions",
              "save_brief"}
    assert writes <= set(tools.SPEC_BY_NAME)
    for name in writes:
        json.dumps(tools.SPEC_BY_NAME[name].input_schema)  # JSON-serializable over stdio
