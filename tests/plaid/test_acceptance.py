"""Fabricated end-to-end host acceptance; no network or native Keychain access."""
import subprocess
import sys
import json
import sqlite3

import httpx
import pytest
from click.testing import CliRunner

from local_budget import backup, db, reports, splits
from local_budget.cli import main
from local_budget.connectors.plaid import cli, store
from local_budget.connectors.plaid.client import PlaidClient
from .test_apply import health_response
from .test_integration_cli import page, transaction
from .test_link import Vault

ACCESS = "FABRICATED_ACCESS_TOKEN"
SECRET = "FABRICATED_CLIENT_SECRET"


@pytest.fixture
def host(monkeypatch):
    vault = Vault()
    vault.put("test-ref-test-item", ACCESS)
    vault.put("client", SECRET)
    replies, requests = [], []

    def handler(request):
        endpoint = request.url.path
        body = json.loads(request.content)
        assert body["secret"] == vault.get("client")
        assert body["access_token"] == vault.get("test-ref-test-item")
        assert request.url.host == "sandbox.plaid.com"
        requests.append((endpoint, body.get("cursor")))
        if endpoint in ("/item/get", "/accounts/get"):
            return httpx.Response(200, json=health_response(endpoint))
        assert endpoint == "/transactions/sync"
        reply = replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    monkeypatch.setattr(cli, "_host", lambda: None)
    monkeypatch.setattr(cli, "_interactive", lambda: None)
    monkeypatch.setattr(cli, "_configured", lambda env, **kw: (
        {"client_id": "FABRICATED_CLIENT"}, vault, vault.get("client")))
    monkeypatch.setattr(cli, "PlaidClient", lambda *args, **kw: PlaidClient(
        *args, **kw, transport=httpx.MockTransport(handler)))
    return replies, requests, vault


def invoke(args, *, approve=False):
    result = CliRunner().invoke(main, ["plaid", *args], input="y\n" if approve else None)
    assert result.exit_code == 0, result.output
    assert ACCESS not in result.output and SECRET not in result.output
    return result.output


def ledger(path):
    with db.connect(path) as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT txn_id,fitid,posted_date,amount_cents,merchant_norm,category,"
            "category_source,status FROM transactions ORDER BY txn_id")]


def cursor_commits(path):
    with db.connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM acceptance_cursor_commits").fetchone()[0]


def test_host_lifecycle_atomic_corrections_backup_replay_and_privacy(
    plaid_db, seed_item, host, caplog
):
    replies, requests, vault = host
    item = seed_item()
    connection = str(item)
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["state"] == "disabled"
        conn.execute("CREATE TABLE acceptance_cursor_commits(cursor TEXT)")
        conn.execute("CREATE TRIGGER acceptance_cursor AFTER UPDATE OF cursor ON plaid_items "
                     "WHEN OLD.cursor IS NOT NEW.cursor BEGIN "
                     "INSERT INTO acceptance_cursor_commits VALUES(NEW.cursor); END")
    invoke(["accounts", connection])
    invoke(["map", connection, "a1", "--local-account", "1", "--start-date", "2026-10-01"])
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT enabled FROM plaid_accounts").fetchone()[0] == 0
        before_preview = "\n".join(conn.iterdump())
    history = page([transaction("before-boundary", date="2026-09-30"),
                    transaction("pending", pending=True)])
    replies.append(history)
    invoke(["preview", connection])
    with db.connect(plaid_db) as conn:
        assert "\n".join(conn.iterdump()) == before_preview
    replies.append(history)
    invoke(["activate", connection, "a1"], approve=True)
    assert ledger(plaid_db) == []
    assert cursor_commits(plaid_db) == 1
    replies.append(page([transaction("t1"), transaction("t2", amount=8)], cursor="posted"))
    output = invoke(["sync", "--all", "--non-interactive", "--json"])
    assert json.loads(output)[0]["inserted"] == 2
    assert not any(s in output for s in ["FABRICATED", "SHOP", "t1", "a1"])
    assert cursor_commits(plaid_db) == 2
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE transactions SET category='Shopping',category_source='manual'")
        conn.execute("UPDATE transactions SET category='Subscriptions',category_source='manual' WHERE txn_id=1")
        splits.apply(conn, 1, [dict(amount_cents=-1200, category="Shopping")])
        assert splits.verify(conn) == []
        assert reports.month_summary("2026-10", conn=conn)["spend_total_cents"] == 2000
    saved = backup.backup(str(plaid_db.parent / "acceptance-backup.db"))
    assert saved.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(saved) as conn:
        dump = "\n".join(conn.iterdump())
    assert "plaid_meta" in dump
    for secret in vault.values.values():
        assert secret not in dump and secret.encode() not in saved.read_bytes()
    original = ledger(plaid_db)
    correction = page(cursor="partial") | dict(
        modified=[transaction("t1", amount=16, name="CORRECTED SHOP")], has_more=True)
    removal = page(removed=["t2"], cursor="corrected")
    replies.extend([correction, removal])
    invoke(["sync", connection])
    final = ledger(plaid_db)
    assert final != original
    assert cursor_commits(plaid_db) == 3  # two pages, one cursor commit
    with db.connect(plaid_db) as conn:
        assert final[0][3:] == (-1600, "CORRECTED SHOP", "Subscriptions", "manual", "posted")
        assert final[1][-1] == "removed"
        assert splits.for_txn(conn, 1) == []
        assert "-1200" in conn.execute("SELECT archived_splits FROM plaid_changes WHERE event_kind='modified'").fetchone()[0]
        summary = reports.month_summary("2026-10", conn=conn)
        assert summary["spend_total_cents"] == 1600
        assert summary["spend_by_category"] == {"Subscriptions": 1600}
        assert store.public_status(conn)[0]["review_count"] == 1
        assert store.item(conn, item)["cursor"] == "corrected"
    replies.extend([page([transaction("never-committed")], cursor="failed-part", ) | dict(has_more=True),
                    httpx.Response(400, json={"error_code": "INVALID_FIELD", "error_message": SECRET})])
    failed = CliRunner().invoke(main, ["plaid", "sync", connection])
    assert failed.exit_code != 0
    assert ACCESS not in failed.output and SECRET not in failed.output
    assert ledger(plaid_db) == final and cursor_commits(plaid_db) == 3
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["cursor"] == "corrected"
        assert conn.execute("SELECT split_needs_review FROM plaid_transactions WHERE transaction_id='t1'").fetchone()[0] == 1
        assert reports.month_summary("2026-10", conn=conn)["spend_total_cents"] == 1600
        conn.execute("CREATE VIEW acceptance_private AS SELECT cursor FROM plaid_items")
    with db.agent_connect(plaid_db) as conn:
        assert conn.execute("SELECT review_count FROM plaid_status_public").fetchone()[0] == 1
        for query in ["SELECT credential_ref FROM plaid_items", "SELECT * FROM acceptance_private",
                      "SELECT p.cursor FROM accounts a JOIN plaid_items p ON a.account_id=p.item_id"]:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(query).fetchall()
    # Restore every table and the ledger namespace, not only financial rows.
    with sqlite3.connect(saved) as src, sqlite3.connect(plaid_db) as dst:
        src.backup(dst)
    assert ledger(plaid_db) == original and cursor_commits(plaid_db) == 2
    replies.extend([correction, removal])
    invoke(["sync", connection])
    assert ledger(plaid_db) == final and cursor_commits(plaid_db) == 3
    with db.connect(plaid_db) as conn:
        assert reports.month_summary("2026-10", conn=conn)["spend_total_cents"] == 1600
    assert requests[-2:] == [("/transactions/sync", "posted"), ("/transactions/sync", "partial")]
    assert not replies
    assert ACCESS not in caplog.text and SECRET not in caplog.text


def test_native_imports_remain_lazy_under_simulated_linux():
    # Isolated process avoids replacing credential exception classes used elsewhere.
    script = """
import builtins
import sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'Security' or name.startswith('Foundation'):
        raise AssertionError('native import on Linux')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.platform = 'linux'
from local_budget.connectors.plaid import cli, credentials
assert cli.plaid.name == 'plaid'
try:
    credentials._security()
except credentials.CredentialError as error:
    assert error.code == 'unsupported_platform'
else:
    raise AssertionError('native access must be unavailable')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
