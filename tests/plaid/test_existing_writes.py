import pytest
from click.testing import CliRunner
from local_budget import db, intake, normalize, reconcile
from local_budget.cli import main
from local_budget.connectors.plaid import store
from local_budget.ingest import importer, parse


def mapping(conn, item, enabled=False):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    store.discover_accounts(
        conn,
        item_id=item,
        accounts=[
            {
                "account_id": "a1",
                "type": "depository",
                "subtype": "checking",
                "balances": {"iso_currency_code": "USD"},
                "mask": "1234",
            }
        ],
    )
    store.map_account(
        conn,
        item_id=item,
        provider_account_id="a1",
        local_account_id=1,
        currency="USD",
        start_date="2026-10-01",
    )
    if enabled:
        conn.execute("UPDATE plaid_accounts SET enabled=1 WHERE item_id=?", (item,))


def transaction(conn, fitid="t1", run=None):
    return conn.execute(
        "INSERT INTO transactions(account_id,fitid,posted_date,amount_cents,imported_at,import_run_id) VALUES(1,?,'2026-10-02',-100,'now',?)",
        (fitid, run),
    ).lastrowid


def own(conn, item, txn):
    mapping(conn, item)
    conn.execute(
        "INSERT INTO plaid_transactions(item_id,transaction_id,provider_account_id,local_txn_id,active_owner,disposition) VALUES(?,'p1','a1',?,1,'posted')",
        (item, txn),
    )


def test_reset_refuses_existing_provider_mapping(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        mapping(conn, item)
    result = CliRunner().invoke(main, ["reset", "--yes"])
    assert result.exit_code != 0
    assert "Plaid" in result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM plaid_accounts").fetchone()[0] == 1


@pytest.mark.parametrize(
    "protected_date,enabled,blocked",
    [
        ("2026-10-02", True, True),
        ("2026-09-30", True, False),
        ("2026-10-02", False, False),
    ],
)
def test_entire_mixed_import_guard(
    plaid_db, seed_item, monkeypatch, tmp_path, protected_date, enabled, blocked
):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        mapping(conn, item, enabled)
        conn.execute(
            "UPDATE accounts SET acct_hash=? WHERE account_id=1",
            (db.acct_hash("b", "a"),),
        )
    accounts = [
        parse.ParsedAccount(
            "other",
            "other",
            "checking",
            "TEST",
            [parse.ParsedTxn("safe", "2026-10-03", "-1", None, "SHOP", None)],
        ),
        parse.ParsedAccount(
            "b",
            "a",
            "checking",
            "TEST",
            [parse.ParsedTxn("owned", protected_date, "-1", None, "SHOP", None)],
        ),
    ]
    monkeypatch.setattr(parse, "parse_file", lambda p: accounts)
    if blocked:
        with pytest.raises(store.ProviderOwnershipError, match="source_overlap"):
            importer.import_file(tmp_path / "mixed.csv")
    else:
        assert importer.import_file(tmp_path / "mixed.csv")["inserted"] == 2
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == (
            0 if blocked else 2
        )
        assert conn.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0] == 0
        if blocked:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM import_runs WHERE status='success'"
                ).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize("action", ["accept_incoming", "merge", "keep_one"])
def test_reconcile_protected_destructive_paths(plaid_db, seed_item, action):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        a = transaction(conn)
        b = transaction(conn, "t2")
        own(conn, item, b if action == "keep_one" else a)
        kind = "fitid_collision" if action == "accept_incoming" else "near_duplicate"
        cid = conn.execute(
            "INSERT INTO import_conflicts(account_id,kind,existing_txn_id,incoming_txn_id,incoming_amount_cents,incoming_posted_date,detected_at) VALUES(1,?,?,?,-200,'2026-10-03','now')",
            (kind, a, b),
        ).lastrowid
    with pytest.raises(reconcile.ReconcileError, match="provider_owned"):
        reconcile.resolve(cid, action)
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        assert conn.execute("SELECT resolved FROM import_conflicts").fetchone()[0] == 0


def test_undo_refuses_entire_owned_batch(plaid_db, seed_item):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        run = conn.execute(
            "INSERT INTO import_runs(started_at,status,source_name) VALUES('now','success','test')"
        ).lastrowid
        transaction(conn, run=run)
        b = transaction(conn, "t2", run)
        own(conn, item, b)
    result = intake.undo_last_import()
    assert result["undone"] is False and "provider_owned" in result["reason"]
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM plaid_transactions").fetchone()[0] == 1
        )


def test_normalize_stale_batch_is_atomic(plaid_db):
    with db.connect(plaid_db) as conn:
        txn = transaction(conn)
        conn.execute(
            "UPDATE transactions SET canonical_merchant='NEW' WHERE txn_id=?", (txn,)
        )
        conn.execute(
            "INSERT INTO merchant_aliases(pattern,canonical,source) VALUES('PATTERN','NEW','manual')"
        )
        conn.execute(
            "INSERT INTO normalize_changes(batch_id,txn_id,old_canonical,invalidated_reason) VALUES(1,?,'OLD','provider_modified')",
            (txn,),
        )
        conn.execute(
            "INSERT INTO normalize_changes(batch_id,txn_id,new_pattern) VALUES(1,0,'PATTERN')"
        )
    result = normalize.undo_last()
    assert result["undone"] is False
    with db.connect(plaid_db) as conn:
        assert (
            conn.execute("SELECT canonical_merchant FROM transactions").fetchone()[0]
            == "NEW"
        )
        assert conn.execute(
            "SELECT 1 FROM merchant_aliases WHERE pattern='PATTERN'"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM normalize_changes").fetchone()[0] == 2


def test_inactive_provider_collision_rejects_import(
    plaid_db, seed_item, monkeypatch, tmp_path
):
    item = seed_item()
    with db.connect(plaid_db) as conn:
        txn = transaction(conn)
        own(conn, item, txn)
        conn.execute(
            "UPDATE accounts SET acct_hash=? WHERE account_id=1",
            (db.acct_hash("b", "a"),),
        )
    monkeypatch.setattr(
        parse,
        "parse_file",
        lambda p: [
            parse.ParsedAccount(
                "b",
                "a",
                "checking",
                "TEST",
                [parse.ParsedTxn("t1", "2026-10-02", "-2", None, "SHOP", None)],
            )
        ],
    )
    with pytest.raises(store.ProviderOwnershipError, match="provider_owned"):
        importer.import_file(tmp_path / "collision.csv")
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0] == 0
        assert (
            conn.execute("SELECT amount_cents FROM transactions").fetchone()[0] == -100
        )


def test_provider_owned_manual_split_and_categories_remain_usable(plaid_db, seed_item):
    from local_budget import splits
    from local_budget.categorize import manual

    item = seed_item()
    with db.connect(plaid_db) as conn:
        txn = transaction(conn)
        own(conn, item, txn)
    with db.agent_connect(write=True) as conn:
        db.begin_immediate(conn)
        manual.set_transaction_category(txn, "Subscriptions", conn=conn)
        assert (
            splits.apply(conn, txn, [{"amount_cents": -100, "category": "Groceries"}])
            == 1
        )
        manual.split_subscriptions(conn=conn)
        assert splits.verify(conn) == []


@pytest.mark.parametrize("suffix", ["qfx", "csv"])
def test_real_statement_source_overlap_records_safe_intake_rejection(
    plaid_db, seed_item, tmp_path, monkeypatch, suffix
):
    from local_budget import inbox_adapter
    from ofx_fixtures import write_ofx

    item = seed_item()
    box = tmp_path / "inbox"
    box.mkdir()
    db.set_setting("inbox_dir", str(box))
    monkeypatch.setattr(inbox_adapter, "STABILITY_SECS", 0)
    file = box / f"statement.{suffix}"
    if suffix == "qfx":
        write_ofx(
            file,
            [
                {
                    "trntype": "DEBIT",
                    "dtposted": "20261002",
                    "amount": "-1.00",
                    "fitid": "F1",
                    "name": "SHOP",
                }
            ],
        )
    else:
        file.write_text('"10/02/2026","-1.00","*","","SHOP"\n')
    account = parse.parse_file(file)[0]
    with db.connect(plaid_db) as conn:
        mapping(conn, item, True)
        conn.execute(
            "UPDATE accounts SET acct_hash=? WHERE account_id=1",
            (db.acct_hash(account.bankid, account.acctid),),
        )
    result = intake.run_intake()
    assert result["files_quarantined"] == 1
    with db.connect(plaid_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM import_conflicts").fetchone()[0] == 0
        record = conn.execute("SELECT state,reason,run_id FROM inbox_files").fetchone()
        assert (
            record["state"] == "quarantined" and record["reason"] == "source_overlap"
        )
        assert record["run_id"] is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM import_runs WHERE status='success'"
            ).fetchone()[0]
            == 0
        )
    assert file.exists()
