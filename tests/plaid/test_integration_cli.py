import json
from click.testing import CliRunner
import pytest

from local_budget import db, splits
from local_budget.connectors.plaid import cli, store
from .test_apply import enabled
from .test_link import Vault


def mock_provider(monkeypatch, responses):
    vault = Vault()
    vault.put("test-ref-test-item", "FABRICATED_ACCESS")
    requests = []

    class Client:
        bytes_received = 0

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def call(self, endpoint, body, **kwargs):
            requests.append((endpoint, body.get("cursor")))
            return responses.pop(0)

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli, "_interactive", lambda: None)
    monkeypatch.setattr(
        cli,
        "_configured",
        lambda env, **kw: (
            {"client_id": "FABRICATED_CLIENT"},
            vault,
            "FABRICATED_SECRET",
        ),
    )
    monkeypatch.setattr(cli, "PlaidClient", Client)
    return requests


def page(added=(), removed=(), cursor="next"):
    return dict(
        added=list(added),
        modified=[],
        removed=[{"transaction_id": r} for r in removed],
        has_more=False,
        next_cursor=cursor,
        transactions_update_status="HISTORICAL_UPDATE_COMPLETE",
    )


def transaction(id="FABRICATED_TXN", **changes):
    result = dict(
        transaction_id=id,
        account_id="a1",
        date="2026-10-01",
        amount=12,
        pending=False,
        iso_currency_code="USD",
        name="SHOP",
    )
    result.update(changes)
    return result


def test_accounts_discovery_masked_disabled(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    requests = mock_provider(
        monkeypatch,
        [
            dict(
                accounts=[
                    dict(
                        account_id="a1",
                        name="PRIVATE_ACCOUNT_NAME",
                        type="depository",
                        subtype="checking",
                        mask="1234",
                        balances={"iso_currency_code": "USD", "current": 999},
                    )
                ]
            )
        ],
    )
    result = CliRunner().invoke(cli.plaid, ["accounts", str(item)])
    assert result.exit_code == 0, result.output
    assert requests == [("/accounts/get", None)]
    assert (
        "1234" in result.output
        and "PRIVATE_ACCOUNT_NAME" not in result.output
        and "999" not in result.output
    )
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT enabled FROM plaid_accounts").fetchone()[0] == 0


def test_preview_is_readonly_full_and_delta_on_saved_cursor(
    plaid_db, seed_item, monkeypatch
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute("UPDATE plaid_items SET cursor='saved'")
    requests = mock_provider(
        monkeypatch,
        [
            page([transaction()], cursor="full"),
            page(removed=["FABRICATED_TXN"], cursor="delta"),
        ],
    )
    result = CliRunner().invoke(cli.plaid, ["preview", str(item)])
    assert result.exit_code == 0, result.output
    assert requests == [("/transactions/sync", None), ("/transactions/sync", "saved")]
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["cursor"] == "saved"
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_activation_confirm_applies_and_aborted_confirmation_preserves_state(
    plaid_db, seed_item, monkeypatch
):
    from .test_mapping import mapped

    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mapped(conn, item)
    mock_provider(monkeypatch, [page([transaction()]), page([transaction()])])
    runner = CliRunner()
    result = runner.invoke(cli.plaid, ["activate", str(item), "a1"], input="n\n")
    assert result.exit_code != 0
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["cursor"] is None
        assert conn.execute("SELECT enabled FROM plaid_accounts").fetchone()[0] == 0
    result = runner.invoke(cli.plaid, ["activate", str(item), "a1"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "complete" in result.output.lower() and "gap" in result.output.lower()
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["mapping_generation"] == 2
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1


def test_adopt_confirms_current_fact_binds_existing_txn(
    plaid_db, seed_item, monkeypatch
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')"
        )
    mock_provider(monkeypatch, [page([transaction()])])
    result = CliRunner().invoke(
        cli.plaid,
        ["adopt", str(item), "FABRICATED_TXN", "--local-txn", "1"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute(
            "SELECT local_txn_id,active_owner FROM plaid_transactions"
        ).fetchone()[:] == (1, 1)
        assert store.item(conn, item)["cursor"] is None


def test_scheduled_sync_outputs_only_safe_counts_and_local_handles(
    plaid_db, seed_item, monkeypatch
):
    item = seed_item()
    enabled(plaid_db, item)
    mock_provider(monkeypatch, [page([transaction()])])
    result = CliRunner().invoke(
        cli.plaid, ["sync", "--all", "--non-interactive", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data == [
        dict(
            connection=item,
            outcome="healthy",
            inserted=1,
            updated=0,
            removed=0,
            review_count=0,
        )
    ]
    assert all(
        token not in result.output for token in ["FABRICATED", "SHOP", "-1200", "a1"]
    )


@pytest.mark.parametrize("status", ["removed", "pending"])
def test_nonposted_merchant_reports_drilldowns_and_split_loaders(plaid_db, status):
    from local_budget.connectors.amazon import (
        report as azreport,
        match as azmatch,
        split as azsplit,
    )
    from local_budget.connectors.walmart import (
        report as wmreport,
        match as wmmatch,
        split as wmsplit,
    )

    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO transactions(txn_id,account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at,status) VALUES(1,1,'local','2026-10-01',-1200,'SHOP','now',?)",
            (status,),
        )
        conn.execute(
            "INSERT INTO txn_splits(txn_id,amount_cents,category,source,created_at) VALUES(1,-1200,'Shopping','manual','now')"
        )
        conn.execute(
            "INSERT INTO amazon_orders(order_number,order_placed_date,fetched_at) VALUES('a','2026-10-01','now')"
        )
        conn.execute(
            "INSERT INTO amazon_transactions(amazon_txn_id,completed_date,grand_total_cents,order_number,fetched_at) VALUES(1,'2026-10-01',-1200,'a','now')"
        )
        conn.execute(
            "INSERT INTO amazon_items(order_number,line_no,asin,title,quantity,unit_price_cents) VALUES('a',1,'item','Book',1,1200)"
        )
        conn.execute(
            "INSERT INTO amazon_matches(amazon_txn_id,txn_id,confidence,matched_at) VALUES(1,1,'manual','now')"
        )
        conn.execute(
            "INSERT INTO walmart_orders(order_number,order_placed_date,fetched_at) VALUES('w','2026-10-01','now')"
        )
        conn.execute(
            "INSERT INTO walmart_items(order_number,line_no,title,line_price_cents) VALUES('w',1,'Book',1200)"
        )
        conn.execute(
            "INSERT INTO walmart_matches(order_number,txn_id,confidence,matched_at) VALUES('w',1,'manual','now')"
        )
        assert splits.list_split_txns(conn) == []
        assert azmatch.breakdown(conn) == [] and wmmatch.breakdown(conn) == []
        assert wmmatch.split_settlements(conn)["orders"] == 0
        for report in [azreport, wmreport]:
            data = report.gather(conn)
            assert data["charge_count"] == 0
        for module in [azsplit, wmsplit]:
            with pytest.raises(module.NoOrderBehind):
                module.propose(conn, 1)


def test_activation_race_after_prompt_aborts_with_no_write_lock_during_confirmation(
    plaid_db, seed_item, monkeypatch
):
    from .test_mapping import mapped

    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        mapped(conn, item)
    mock_provider(monkeypatch, [page([transaction()])])

    def confirm(*args, **kwargs):
        with db.connect(plaid_db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'during-review','2026-10-01',-1200,'SHOP','now')"
            )
        return True

    monkeypatch.setattr(cli.click, "confirm", confirm)
    result = CliRunner().invoke(cli.plaid, ["activate", str(item), "a1"])
    assert result.exit_code != 0 and "review_changed" in result.output
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)["cursor"] is None
        assert conn.execute("SELECT enabled FROM plaid_accounts").fetchone()[0] == 0


def test_map_holds_intake_through_commit(plaid_db, seed_item, monkeypatch):
    from contextlib import contextmanager
    from local_budget import inbox_adapter
    from .test_mapping import discovery

    item = seed_item()
    with db.connect(plaid_db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        discovery(conn, item)
    active = [False]

    @contextmanager
    def intake():
        active[0] = True
        yield True
        with db.connect(plaid_db) as conn:
            assert (
                conn.execute("SELECT start_date FROM plaid_accounts").fetchone()[0]
                == "2026-10-01"
            )
        active[0] = False

    real = store.map_account

    def guarded(*args, **kwargs):
        assert active[0]
        return real(*args, **kwargs)

    monkeypatch.setattr(inbox_adapter, "intake_lock", intake)
    monkeypatch.setattr(store, "map_account", guarded)
    result = CliRunner().invoke(
        cli.plaid,
        ["map", str(item), "a1", "--local-account", "1", "--start-date", "2026-10-01"],
    )
    assert result.exit_code == 0, result.output


def test_client_credential_failure_records_safe_health_no_provider_call(
    plaid_db, seed_item, monkeypatch
):
    from local_budget.connectors.plaid.credentials import CredentialError

    item = seed_item()
    enabled(plaid_db, item)

    def locked(*args, **kwargs):
        assert kwargs["interactive"] is False
        raise CredentialError("credential_locked")

    monkeypatch.setattr(cli, "_configured", locked)
    result = CliRunner().invoke(
        cli.plaid, ["sync", "--all", "--non-interactive", "--json"]
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)[0]["safe_error_code"] == "credential_locked"
    with db.connect(plaid_db) as conn:
        assert store.public_status(conn)[0]["state"] == "credential_locked"
        assert store.item(conn, item)["cursor"] is None


def test_financial_preview_requires_terminal(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    result = CliRunner().invoke(cli.plaid, ["preview", str(item)])
    assert result.exit_code != 0 and "interactive_required" in result.output


def test_adoption_shows_local_candidate_facts_before_approval(
    plaid_db, seed_item, monkeypatch
):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')"
        )
    mock_provider(monkeypatch, [page([transaction()])])
    result = CliRunner().invoke(
        cli.plaid,
        ["adopt", str(item), "FABRICATED_TXN", "--local-txn", "1"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert (
        "Local candidate 1 | account=1 | 2026-10-01 | -1200 cents | SHOP | posted"
        in result.output
    )


def test_nonposted_split_detail_excluded(plaid_db):
    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at,status) VALUES(1,'removed','2026-10-01',-1200,'SHOP','now','removed')"
        )
        conn.execute(
            "INSERT INTO txn_splits(txn_id,amount_cents,category,source,created_at) VALUES(1,-1200,'Shopping','manual','now')"
        )
        assert splits.for_txn(conn, 1) == []


def test_adoption_deadline_includes_confirmation(plaid_db, seed_item, monkeypatch):
    from local_budget.connectors.plaid import sync

    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute(
            "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,merchant_norm,imported_at) VALUES(1,'csv','2026-10-01',-1200,'SHOP','now')"
        )
    mock_provider(monkeypatch, [page([transaction()])])
    now = sync._monotonic()
    clock = [now]
    monkeypatch.setattr(sync, "_monotonic", lambda: clock[0])

    def expired(*args, **kwargs):
        clock[0] += 301
        return True

    monkeypatch.setattr(cli.click, "confirm", expired)
    result = CliRunner().invoke(
        cli.plaid, ["adopt", str(item), "FABRICATED_TXN", "--local-txn", "1"]
    )
    assert result.exit_code != 0 and "sync_deadline" in result.output
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM plaid_transactions").fetchone()[0] == 0
        )


def test_rebuild_cli_full_fetch_confirmation_and_no_automatic_reset(plaid_db, seed_item, monkeypatch):
    from .test_apply import apply
    from .test_mapping import fact
    item = seed_item()
    enabled(plaid_db, item)
    apply(plaid_db, item, ('added', fact('t1')), next_cursor='saved')
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        store.set_lifecycle(conn, item, 'recovery_required')
    requests = mock_provider(monkeypatch, [page([transaction('t1')], cursor='full')])
    result = CliRunner().invoke(cli.plaid, ['rebuild', str(item)], input='y\n')
    assert result.exit_code == 0, result.output
    assert 'Unknown coverage' in result.output
    assert requests == [('/transactions/sync', None)]
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['cursor'] == 'full'
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1


def test_rebuild_cli_decline_keeps_recovery_cursor(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    enabled(plaid_db, item)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE plaid_items SET cursor='saved' WHERE item_id=?", (item,))
        store.set_lifecycle(conn, item, 'recovery_required')
    mock_provider(monkeypatch, [page(cursor='full')])
    result = CliRunner().invoke(cli.plaid, ['rebuild', str(item)], input='n\n')
    assert result.exit_code != 0
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item)['cursor'] == 'saved'
        assert store.item(conn, item)['state'] == 'recovery_required'


def test_disconnect_rotate_cli_available(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    requests = mock_provider(monkeypatch, [{'request_id': 'safe'}])
    result = CliRunner().invoke(cli.plaid, ['disconnect', str(item)], input='y\n')
    assert result.exit_code == 0, result.output
    assert requests == [('/item/remove', None)]
    assert 'disconnected' in result.output


def test_replace_cli_explicit_match_and_disabled_result(plaid_db, seed_item, monkeypatch):
    from .test_apply import apply
    from .test_mapping import fact, mapped
    old, new = seed_item('old'), seed_item('new')
    enabled(plaid_db, old)
    apply(plaid_db, old, ('added', fact('old-t')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
        tid = conn.execute('SELECT txn_id FROM transactions').fetchone()[0]
    requests = mock_provider(monkeypatch, [page([transaction('new-t')])])
    # The fake host credential is also scoped to the replacement Item.
    original = cli._configured
    def configured(env, **kw):
        config, vault, secret = original(env, **kw)
        vault.put('test-ref-new', 'FABRICATED_NEW')
        return config, vault, secret
    monkeypatch.setattr(cli, '_configured', configured)
    result = CliRunner().invoke(cli.plaid, ['replace', str(old), str(new)], input=f'{tid}\ny\ny\n')
    assert result.exit_code == 0, result.output
    assert 'Local candidate' in result.output and 'New mappings remain disabled' in result.output
    assert requests == [('/transactions/sync', None)]
    with db.connect(plaid_db) as conn:
        assert store.item(conn, old)['state'] == 'replaced'
        assert store.item(conn, new)['state'] == 'disabled'
        assert conn.execute('SELECT SUM(enabled) FROM plaid_accounts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 1
        assert conn.execute('SELECT item_id FROM plaid_transactions WHERE active_owner=1').fetchone()[0] == new


def test_replace_cli_stale_local_review_rolls_back(plaid_db, seed_item, monkeypatch):
    from .test_apply import apply
    from .test_mapping import fact, mapped
    old, new = seed_item('old'), seed_item('new')
    enabled(plaid_db, old)
    apply(plaid_db, old, ('added', fact('old-t')))
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        mapped(conn, new)
    mock_provider(monkeypatch, [page([transaction('new-t')])])
    original = cli._configured
    def configured(env, **kw):
        config, vault, secret = original(env, **kw)
        vault.put('test-ref-new', 'FABRICATED_NEW')
        return config, vault, secret
    monkeypatch.setattr(cli, '_configured', configured)
    def confirm(message, **kw):
        if message.startswith('Retire'):
            with db.connect(plaid_db) as conn:
                conn.execute("UPDATE transactions SET payee='changed' WHERE txn_id=1")
        return True
    monkeypatch.setattr(cli.click, 'confirm', confirm)
    result = CliRunner().invoke(cli.plaid, ['replace', str(old), str(new)], input='1\n')
    assert result.exit_code != 0 and 'stale_review' in result.output
    with db.connect(plaid_db) as conn:
        assert store.item(conn, old)['state'] == 'active'
        assert conn.execute('SELECT COUNT(*) FROM plaid_transactions WHERE item_id=?', (new,)).fetchone()[0] == 0


def test_rotate_cli_uses_new_access_token_field(plaid_db, seed_item, monkeypatch):
    item = seed_item()
    requests = mock_provider(monkeypatch, [{'new_access_token': 'FABRICATED_NEW'}])
    result = CliRunner().invoke(cli.plaid, ['rotate', str(item)], input='y\n')
    assert result.exit_code == 0, result.output
    assert requests == [('/item/access_token/invalidate', None)]
    assert 'FABRICATED' not in result.output
