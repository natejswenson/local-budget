"""Reports, budgets, reconcile (design §4.3/§4.4, I11, I11b, I8c/I8d/I8e)."""
from __future__ import annotations

from click.testing import CliRunner

from local_budget import budgets, categories, db, reconcile, reports
from local_budget.cli import main
from local_budget.ingest import importer

from ofx_fixtures import write_ofx

JUNE = [
    {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1", "name": "WALMART"},
    {"trntype": "DEBIT", "dtposted": "20260610", "amount": "-30.00", "fitid": "D1", "name": "VOLT CAFE"},
    {"trntype": "XFER", "dtposted": "20260605", "amount": "-200.00", "fitid": "P1", "name": "WF CREDIT CARD PAYMENT"},
    {"trntype": "CREDIT", "dtposted": "20260601", "amount": "2000.00", "fitid": "I1", "name": "ACME PAYROLL"},
    {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-100.00", "fitid": "N1", "name": "529 PLAN"},
]


def _import(tmp_path, txns, name="stmt.qfx"):
    db.init_schema()
    return importer.import_file(write_ofx(tmp_path / name, txns))


def test_spend_total_is_sum_of_spend_categories(data_dir, tmp_path):
    _import(tmp_path, JUNE)
    s = reports.month_summary("2026-06")
    # Spend = Walmart 50 + Volt 30 + Investments 100 = 180 (Transfer & Income excluded).
    assert s["spend_total_cents"] == 18000
    assert s["spend_by_category"]["Investments"] == 10000  # Investments counts as spend (XFER override would not matter; it's DEBIT here)
    assert "Transfer" not in s["spend_by_category"]
    assert "Income" not in s["spend_by_category"]


def test_floor_marked_spend_category_reports_as_savings(data_dir, tmp_path):
    # Investments (floor-marked) moves out of spend_by_category/spend_total_cents
    # and into savings_by_category/savings_total_cents — money relocated, not spent.
    _import(tmp_path, JUNE)
    categories.mark_floor_category("Investments")
    s = reports.month_summary("2026-06")
    assert "Investments" not in s["spend_by_category"]
    assert s["spend_total_cents"] == 8000          # Walmart 50 + Volt 30, Investments excluded
    assert s["savings_by_category"]["Investments"] == 10000
    assert s["savings_total_cents"] == 10000


def test_transfer_and_income_accounted_separately(data_dir, tmp_path):
    _import(tmp_path, JUNE)
    s = reports.month_summary("2026-06")
    assert s["income_cents"] == 200000
    assert s["transfer_cents"] == -20000


def test_spend_equals_sum_of_category_totals_invariant(data_dir, tmp_path):
    # I11: spend total == sum of spend-category totals.
    _import(tmp_path, JUNE)
    s = reports.month_summary("2026-06")
    assert s["spend_total_cents"] == sum(s["spend_by_category"].values())


def test_quarantined_excluded_and_surfaced(data_dir, tmp_path):
    # I11b: a quarantined near-dup is excluded from spend AND surfaced.
    _import(tmp_path, JUNE)
    posted = {"trntype": "DEBIT", "dtposted": "20260604", "amount": "-55.00", "fitid": "G2", "name": "WALMART"}
    importer.import_file(write_ofx(tmp_path / "stmt2.qfx", [posted]), detect_near_duplicates=True)
    s = reports.month_summary("2026-06")
    # The $55 Walmart near-dup is quarantined -> NOT in spend total (still 180).
    assert s["spend_total_cents"] == 18000
    assert s["unresolved_conflicts"]["count"] == 1
    assert s["unresolved_conflicts"]["total_cents"] == 5500


def test_uncategorized_spend_surfaced_and_excluded(data_dir, tmp_path):
    # S1: a matched debit (WALMART->Groceries) plus an UNMATCHED debit
    # (->Uncategorized) — the uncategorized debit is surfaced AND excluded.
    db.init_schema()
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "G1", "name": "WALMART"},
        {"trntype": "DEBIT", "dtposted": "20260607", "amount": "-42.50", "fitid": "U1", "name": "SOME RANDOM VENDOR XYZ"},
    ]
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", txns))
    s = reports.month_summary("2026-06")
    assert s["uncategorized_spend"]["count"] == 1
    assert s["uncategorized_spend"]["total_cents"] == 4250
    # The uncategorized debit is NOT in the spend total (only the $50 Walmart).
    assert s["spend_total_cents"] == 5000
    assert "Uncategorized" not in s["spend_by_category"]


def test_month_over_month_delta(data_dir, tmp_path):
    db.init_schema()
    may = [{"trntype": "DEBIT", "dtposted": "20260515", "amount": "-100.00", "fitid": "M1", "name": "WALMART"}]
    importer.import_file(write_ofx(tmp_path / "may.qfx", may))
    importer.import_file(write_ofx(tmp_path / "jun.qfx",
                                   [{"trntype": "DEBIT", "dtposted": "20260615", "amount": "-150.00", "fitid": "J1", "name": "WALMART"}]))
    s = reports.month_summary("2026-06")
    assert s["prev_spend_total_cents"] == 10000
    assert s["mom_delta_cents"] == 5000


def test_budget_over_under(data_dir, tmp_path):
    _import(tmp_path, JUNE)
    budgets.set_limit("Groceries", 4000, effective_from="2026-06-01")  # $40 limit, $50 spent
    s = reports.month_summary("2026-06")
    grocery = next(b for b in s["budgets"] if b["category"] == "Groceries")
    assert grocery["actual_cents"] == 5000
    assert grocery["over_cents"] == 1000  # $10 over


def test_top_merchants(data_dir, tmp_path):
    _import(tmp_path, JUNE)
    s = reports.month_summary("2026-06")
    names = [m["merchant"] for m in s["top_merchants"]]
    assert "529 PLAN" in names or "WALMART" in names


# ── cli set-limit (money goes through cents_from_amount_str, no float math) ──
def test_cli_set_limit_whole_dollars(data_dir, tmp_path):
    db.init_schema()
    result = CliRunner().invoke(main, ["set-limit", "Dining Out", "400"])
    assert result.exit_code == 0, result.output
    limit = next(b for b in budgets.list_limits() if b["category"] == "Dining Out")
    assert limit["limit_cents"] == 40000  # $400


def test_cli_set_limit_cents_exact(data_dir, tmp_path):
    # $19.99 must be exactly 1999 cents (no int(float*100) penny loss).
    db.init_schema()
    result = CliRunner().invoke(main, ["set-limit", "Dining Out", "19.99"])
    assert result.exit_code == 0, result.output
    limit = next(b for b in budgets.list_limits() if b["category"] == "Dining Out")
    assert limit["limit_cents"] == 1999


def test_cli_set_limit_rejects_sub_cent(data_dir, tmp_path):
    db.init_schema()
    result = CliRunner().invoke(main, ["set-limit", "Dining Out", "19.999"])
    assert result.exit_code != 0  # raises rather than silently rounding money


def test_cli_report_label_direction_aware_per_row(data_dir, tmp_path):
    # `report()` computes the label per-row off each row's own category — a mix
    # of a floor category under target ([UNDER]) and a ceiling category over
    # its limit ([OVER]) in the same output.
    db.init_schema()
    categories.mark_floor_category("Investments")
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-200.00", "fitid": "N1", "name": "529 PLAN"},
        {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-300.00", "fitid": "D1", "name": "CHIPOTLE"}]))
    from local_budget.categorize.manual import set_merchant_category as setc
    setc("529 PLAN", "Investments")
    setc("CHIPOTLE", "Dining Out")
    budgets.set_limit("Investments", 30000)   # $300 target, $200 spent -> under
    budgets.set_limit("Dining Out", 10000)    # $100 limit, $300 spent -> over
    result = CliRunner().invoke(main, ["report", "--month", "2026-06"])
    assert result.exit_code == 0, result.output
    assert "Investments" in result.output and "[UNDER]" in result.output
    assert "Dining Out" in result.output and "[OVER]" in result.output


def test_cli_subscriptions_label_floor_marked(data_dir, tmp_path):
    # `subscriptions()` hoists a single is_floor("Subscriptions") check since
    # its rows carry no per-row category field.
    from local_budget.categorize import manual as manual_mod
    db.init_schema()
    categories.mark_floor_category("Subscriptions")
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260405", "amount": "-15.49", "fitid": "N1", "name": "NETFLIX.COM"}]))
    manual_mod.set_merchant_category("NETFLIX", "Subscriptions")
    manual_mod.split_subscriptions()
    budgets.set_limit("Subscriptions", 2000, subcategory="Netflix")  # $20 target, $15.49 spent -> under
    result = CliRunner().invoke(main, ["subscriptions", "--month", "all"])
    assert result.exit_code == 0, result.output
    assert "[UNDER]" in result.output
    assert "[OVER]" not in result.output


# ── reconcile ────────────────────────────────────────────────────────────────
def _setup_neardup(tmp_path):
    db.init_schema()
    pre = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "fitid": "PRE", "name": "SHELL OIL"}
    importer.import_file(write_ofx(tmp_path / "a.qfx", [pre]))
    post = {"trntype": "DEBIT", "dtposted": "20260606", "amount": "-73.20", "fitid": "POST", "name": "SHELL OIL"}
    importer.import_file(write_ofx(tmp_path / "b.qfx", [post]), detect_near_duplicates=True)
    return reconcile.list_open()[0]["conflict_id"]


def _posted_count():
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0]


def test_reconcile_keep_one(data_dir, tmp_path):
    cid = _setup_neardup(tmp_path)
    reconcile.resolve(cid, "keep_one")
    assert _posted_count() == 1
    assert reconcile.list_open() == []


def test_reconcile_mark_distinct_both_count(data_dir, tmp_path):
    # I8e: both legitimate charges count after mark_distinct.
    cid = _setup_neardup(tmp_path)
    reconcile.resolve(cid, "mark_distinct")
    assert _posted_count() == 2
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0] == 2


def test_reconcile_merge_keeps_posted_amount(data_dir, tmp_path):
    cid = _setup_neardup(tmp_path)
    reconcile.resolve(cid, "merge")
    with db.connect() as conn:
        rows = conn.execute("SELECT amount_cents FROM transactions WHERE status='posted'").fetchall()
    assert len(rows) == 1 and rows[0]["amount_cents"] == -7320  # posted (real) amount kept


def test_merge_two_conflicts_against_same_existing_no_double_count(data_dir, tmp_path):
    # F1/I8e: import the SAME $50 charge three times. The near-dup scan only
    # matches status='posted', so BOTH quarantined rows reference the original
    # posted txn A. Resolving both conflicts with `merge` must leave exactly ONE
    # posted row at $50 — not two posted rows totalling $100.
    db.init_schema()
    base = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "name": "WALMART"}
    importer.import_file(write_ofx(tmp_path / "a.qfx", [{**base, "fitid": "G1"}]))
    importer.import_file(write_ofx(tmp_path / "b.qfx", [{**base, "fitid": "G2"}]), detect_near_duplicates=True)
    importer.import_file(write_ofx(tmp_path / "c.qfx", [{**base, "fitid": "G3"}]), detect_near_duplicates=True)

    conflicts = reconcile.list_open()
    assert len(conflicts) == 2  # C1 and C2, both referencing the original posted A
    assert all(c["existing_txn_id"] == conflicts[0]["existing_txn_id"] for c in conflicts)

    for c in conflicts:
        reconcile.resolve(c["conflict_id"], "merge")

    assert _posted_count() == 1
    s = reports.month_summary("2026-06")
    assert s["spend_total_cents"] == 5000  # one $50 charge, never $100
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE status='posted'").fetchone()[0] == 1


def test_reconcile_keep_one_noop_when_incoming_gone(data_dir, tmp_path):
    # keep_one must no-op safely if a sibling resolution already consumed incoming.
    db.init_schema()
    base = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "name": "WALMART"}
    importer.import_file(write_ofx(tmp_path / "a.qfx", [{**base, "fitid": "G1"}]))
    importer.import_file(write_ofx(tmp_path / "b.qfx", [{**base, "fitid": "G2"}]), detect_near_duplicates=True)
    cid = reconcile.list_open()[0]["conflict_id"]
    incoming = reconcile.list_open()[0]["incoming_txn_id"]
    # Delete the incoming row out from under the conflict, then resolve.
    with db.connect() as conn:
        conn.execute("DELETE FROM transactions WHERE txn_id = ?", (incoming,))
    reconcile.resolve(cid, "keep_one")  # must not raise
    assert _posted_count() == 1


def test_reconcile_collision_accept_incoming(data_dir, tmp_path):
    db.init_schema()
    g = {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-50.00", "fitid": "F1", "name": "WALMART"}
    importer.import_file(write_ofx(tmp_path / "a.qfx", [g]))
    importer.import_file(write_ofx(tmp_path / "b.qfx", [{**g, "amount": "-99.00"}]))
    cid = reconcile.list_open()[0]["conflict_id"]
    reconcile.resolve(cid, "accept_incoming")
    with db.connect() as conn:
        amt = conn.execute("SELECT amount_cents FROM transactions").fetchone()["amount_cents"]
    assert amt == -9900


def test_all_time_aggregates_across_months(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "may.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260515", "amount": "-100.00", "fitid": "M1", "name": "WALMART"}]))
    importer.import_file(write_ofx(tmp_path / "jun.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260615", "amount": "-150.00", "fitid": "J1", "name": "WALMART"}]))
    s = reports.month_summary("all")
    assert s["spend_total_cents"] == 25000          # both months summed
    assert s["prev_month"] is None                  # MoM not meaningful for all-time
    assert len(s["trend"]) == 2                      # two months in the trend


def test_monthly_trend_oldest_first(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "may.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260515", "amount": "-100.00", "fitid": "M1", "name": "WALMART"}]))
    importer.import_file(write_ofx(tmp_path / "jun.qfx",
        [{"trntype": "DEBIT", "dtposted": "20260615", "amount": "-150.00", "fitid": "J1", "name": "WALMART"}]))
    with db.connect() as conn:
        tr = reports.monthly_trend(conn)
    assert [x["month"] for x in tr] == ["2026-05", "2026-06"]
    assert tr[0]["spend_cents"] == 10000 and tr[1]["spend_cents"] == 15000


def test_timeframe_lastn_scope(data_dir, tmp_path, monkeypatch):
    # 'last3' includes only the trailing 3 months relative to today.
    import datetime as _dt
    monkeypatch.setattr(reports, "date", type("D", (), {"today": staticmethod(lambda: _dt.date(2026, 6, 15))}))
    db.init_schema()
    for m in ("01", "04", "05", "06"):
        importer.import_file(write_ofx(tmp_path / f"{m}.qfx",
            [{"trntype": "DEBIT", "dtposted": f"2026{m}05", "amount": "-100.00", "fitid": f"F{m}", "name": "WALMART"}]))
    assert reports.month_summary("last3")["spend_total_cents"] == 30000   # 04+05+06
    assert reports.month_summary("last12")["spend_total_cents"] == 40000  # all four
    assert reports.month_summary("all")["spend_total_cents"] == 40000


def test_monthly_trend_includes_income(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-100.00", "fitid": "S1", "name": "WALMART"},
        {"trntype": "CREDIT", "dtposted": "20260601", "amount": "2000.00", "fitid": "I1", "name": "ACME PAYROLL"}]))
    with db.connect() as conn:
        tr = reports.monthly_trend(conn)
    jun = next(x for x in tr if x["month"] == "2026-06")
    assert jun["spend_cents"] == 10000 and jun["income_cents"] == 200000


def test_insights_over_budget_and_discretionary(data_dir, tmp_path):
    db.init_schema()
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-300.00", "fitid": "D1", "name": "CHIPOTLE"},
        {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-500.00", "fitid": "S1", "name": "AMAZON"}]))
    from local_budget.categorize.manual import set_merchant_category as setc
    setc("CHIPOTLE", "Dining Out")
    setc("AMAZON", "Shopping")
    budgets.set_limit("Dining Out", 10000)  # $100 limit, $300 spent -> over by $200
    ins = reports.insights("all")
    kinds = [i["kind"] for i in ins]
    assert "over_budget" in kinds          # Dining Out over budget surfaced
    assert "reduce" in kinds               # discretionary categories surfaced
    over = next(i for i in ins if i["kind"] == "over_budget")
    assert over["amount_cents"] == 20000   # $200 over


def test_insights_floor_category_under_target(data_dir, tmp_path):
    # Investments: a floor category — spending LESS than the target is bad.
    db.init_schema()
    categories.mark_floor_category("Investments")
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-200.00", "fitid": "N1", "name": "529 PLAN"}]))
    from local_budget.categorize.manual import set_merchant_category as setc
    setc("529 PLAN", "Investments")
    budgets.set_limit("Investments", 30000)  # $300 target, $200 spent -> $100 short
    ins = reports.insights("all")
    kinds = [i["kind"] for i in ins]
    assert "under_target" in kinds
    assert "over_budget" not in kinds
    miss = next(i for i in ins if i["kind"] == "under_target")
    assert miss["amount_cents"] == 10000  # $100 short


def test_insights_floor_category_at_or_above_target_is_silent(data_dir, tmp_path):
    db.init_schema()
    categories.mark_floor_category("Investments")
    importer.import_file(write_ofx(tmp_path / "stmt.qfx", [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-400.00", "fitid": "N1", "name": "529 PLAN"}]))
    from local_budget.categorize.manual import set_merchant_category as setc
    setc("529 PLAN", "Investments")
    budgets.set_limit("Investments", 30000)  # $300 target, $400 spent -> above target
    ins = reports.insights("all")
    kinds = [i["kind"] for i in ins]
    assert "under_target" not in kinds
    assert "over_budget" not in kinds


# ── transactions_in_category (drill-down modal backend, §2) ───────────────────
def test_transactions_in_category_nets_to_bar_with_refund(data_dir, tmp_path):
    # All posted rows incl. a refund (positive) are returned signed, so their
    # signed SUM(amount_cents) == -spend_by_category[cat] — the modal nets to the
    # bar it opened from (S1). An outflow-only filter would over-state the bar.
    _import(tmp_path, [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-50.00", "fitid": "A", "name": "WALMART"},
        {"trntype": "DEBIT", "dtposted": "20260610", "amount": "-30.00", "fitid": "B", "name": "WALMART"},
        {"trntype": "CREDIT", "dtposted": "20260615", "amount": "12.00", "fitid": "C", "name": "WALMART RETURN"},
    ])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Shopping' WHERE status='posted'")
        conn.commit()
    rows = reports.transactions_in_category("Shopping", "2026-06")
    assert len(rows) == 3
    assert any(r["amount_cents"] > 0 for r in rows)              # the refund row is included, signed +
    assert set(rows[0]) == {"merchant_norm", "amount_cents", "posted_date"}  # sanitized columns only
    signed_sum = sum(r["amount_cents"] for r in rows)            # -5000 -3000 +1200 = -6800
    bar = reports.month_summary("2026-06")["spend_by_category"]["Shopping"]
    assert signed_sum == -bar                                   # net-equals-bar (S1)


def test_transactions_in_category_name_with_ampersand(data_dir, tmp_path):
    # A category name with '&'/spaces round-trips to the exact WHERE category = ? match (S1).
    _import(tmp_path, [
        {"trntype": "DEBIT", "dtposted": "20260603", "amount": "-25.00", "fitid": "G", "name": "RED CROSS"},
    ])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Gifts & Donations' WHERE status='posted'")
        conn.commit()
    rows = reports.transactions_in_category("Gifts & Donations", "2026-06")
    assert len(rows) == 1 and rows[0]["amount_cents"] == -2500


def test_transactions_in_category_scope_and_status(data_dir, tmp_path):
    _import(tmp_path, [
        {"trntype": "DEBIT", "dtposted": "20260610", "amount": "-10.00", "fitid": "X", "name": "VOLT CAFE"},
    ])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Dining Out' WHERE status='posted'")
        conn.commit()
    assert len(reports.transactions_in_category("Dining Out", "all")) == 1
    assert len(reports.transactions_in_category("Dining Out", "2026-06")) == 1
    assert reports.transactions_in_category("Dining Out", "2099-01") == []   # out-of-scope month


# ── Budgets tab: zero-based envelopes (design 2026-06-12) ─────────────────────
def _seed_txn(conn, date_, cents, category, subcategory=None):
    conn.execute(
        "INSERT INTO accounts (institution,acct_type,acct_last4,acct_hash,own_account,created_at) "
        "SELECT 'BANK','CHK','1','h',1,'2026-01-01' WHERE NOT EXISTS (SELECT 1 FROM accounts)")
    aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.execute(
        "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
        "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
        "VALUES (?,?,?,?,'posted','M','M','M','M',?,?,'x','',?,1)",
        (aid, f"t{n}", date_, cents, category, subcategory, "2026-06-20"))


def test_same_day_budget_edit_keeps_one_active_row(data_dir):
    db.init_schema()
    budgets.set_limit("Groceries", 40000)
    budgets.set_limit("Groceries", 55000)   # same-day re-edit must REPLACE, not duplicate
    rows = [r for r in budgets.list_limits() if r["category"] == "Groceries"]
    assert len(rows) == 1 and rows[0]["limit_cents"] == 55000


def test_active_row_tiebreaks_on_latest_budget_id(data_dir):
    # Two rows with the SAME effective_from (e.g. a legacy duplicate) -> exactly one active.
    db.init_schema()
    with db.connect() as conn:
        for cents in (40000, 60000):
            conn.execute("INSERT INTO budgets (category,subcategory,limit_cents,effective_from,created_at) "
                         "VALUES ('Groceries',NULL,?, '2026-06-01','x')", (cents,))
    rows = [r for r in budgets.list_limits() if r["category"] == "Groceries"]
    assert len(rows) == 1 and rows[0]["limit_cents"] == 60000   # latest budget_id wins


def test_expected_income_get_set_clear(data_dir):
    db.init_schema()
    assert budgets.get_expected_income() == 0
    budgets.set_expected_income(420000)
    assert budgets.get_expected_income() == 420000
    budgets.clear_expected_income()
    assert budgets.get_expected_income() == 0


def test_to_allocate_counts_top_level_only(data_dir):
    db.init_schema()
    budgets.set_expected_income(100000)
    budgets.set_limit("Groceries", 50000)
    budgets.set_limit("Subscriptions", 6000)
    budgets.set_limit("Subscriptions", 1600, subcategory="Netflix")  # sub MUST NOT add to total
    ov = reports.budget_overview("2026-06")
    assert ov["total_budgeted_cents"] == 56000          # 50000 + 6000, subcategory excluded
    assert ov["to_allocate_cents"] == 44000             # 100000 - 56000
    subs = [c for c in ov["categories"] if c["category"] == "Subscriptions"][0]
    assert subs["sub_total_cents"] == 1600 and subs["subs_exceed"] is False


def test_subs_exceed_flag(data_dir):
    db.init_schema()
    budgets.set_limit("Subscriptions", 2000)
    budgets.set_limit("Subscriptions", 1600, subcategory="Netflix")
    budgets.set_limit("Subscriptions", 1500, subcategory="Spotify")   # 3100 > 2000
    sub = [c for c in reports.budget_overview("2026-06")["categories"] if c["category"] == "Subscriptions"][0]
    assert sub["subs_exceed"] is True and sub["sub_total_cents"] == 3100


def test_over_and_pct(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-06-04", -42000, "Groceries")
    budgets.set_limit("Groceries", 50000)
    g = [c for c in reports.budget_overview("2026-06")["categories"] if c["category"] == "Groceries"][0]
    assert g["spent_cents"] == 42000 and g["over"] is False and g["pct"] == 84
    budgets.set_limit("Groceries", 30000)   # now over
    g = [c for c in reports.budget_overview("2026-06")["categories"] if c["category"] == "Groceries"][0]
    assert g["over"] is True and g["pct"] == 140


def _last_full_months(n=3):
    """The n YYYY-MM strings of the last n FULL months (excludes current), in order."""
    start, _ = reports._full_months_window(n)
    sy, sm = (int(x) for x in start.split("-"))
    idx = sy * 12 + (sm - 1)
    return [f"{(idx + k) // 12:04d}-{(idx + k) % 12 + 1:02d}" for k in range(n)]


def test_apply_suggestions_fills_empties_not_user_values(data_dir):
    # Suggestions average the last 3 FULL months (current partial month excluded).
    db.init_schema()
    m1, m2, m3 = _last_full_months(3)
    with db.connect() as conn:
        _seed_txn(conn, f"{m1}-10", -40000, "Groceries")
        _seed_txn(conn, f"{m2}-10", -50000, "Groceries")
        _seed_txn(conn, f"{m3}-10", -42000, "Groceries")
        _seed_txn(conn, f"{m3}-08", -3000, "Dining Out")
    budgets.set_limit("Dining Out", 99900)        # user-set — must be preserved
    n = reports.apply_suggestions()
    assert n >= 1
    lim = {r["category"]: r["limit_cents"] for r in budgets.list_limits() if r["subcategory"] is None}
    assert lim["Dining Out"] == 99900             # NOT overwritten
    assert lim["Groceries"] == (40000 + 50000 + 42000) // 3   # avg over the 3 full months


def test_set_limit_rejects_structural_categories(data_dir):
    # S-1 (category level): budgets must only exist on SPEND categories.
    db.init_schema()
    for struct in ("Transfer", "Income", "Uncategorized"):
        try:
            budgets.set_limit(struct, 5000)
        except budgets.BudgetError:
            pass
        else:
            raise AssertionError(f"set_limit({struct!r}) should raise BudgetError")
    # Spend categories — builtin and Investments — still work.
    budgets.set_limit("Groceries", 5000)
    budgets.set_limit("Investments", 10000)
    lim = {r["category"]: r["limit_cents"] for r in budgets.list_limits() if r["subcategory"] is None}
    assert lim["Groceries"] == 5000 and lim["Investments"] == 10000


def test_set_limit_rejects_structural_categories_at_subcategory_level(data_dir):
    # S-1 (subcategory level): structural category is rejected even with a subcategory.
    db.init_schema()
    for struct in ("Transfer", "Income", "Uncategorized"):
        try:
            budgets.set_limit(struct, 5000, subcategory="Wire")
        except budgets.BudgetError:
            pass
        else:
            raise AssertionError(f"set_limit({struct!r}, sub) should raise BudgetError")


def test_apply_suggestions_skips_structural_subcategory(data_dir):
    # S-1 + M-5: a Transfer/Wire history sub must NOT get a budget, and the false
    # "Over budget: Transfer / Wire" insight must not appear after suggest-all.
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-04-10", -30000, "Transfer", subcategory="Wire")
        _seed_txn(conn, "2026-05-10", -30000, "Transfer", subcategory="Wire")
        _seed_txn(conn, "2026-06-05", -30000, "Transfer", subcategory="Wire")
        _seed_txn(conn, "2026-06-05", -40000, "Groceries")
    reports.apply_suggestions("2026-06")
    # No budget row created under the structural Transfer category (cat or sub).
    assert not any(r["category"] == "Transfer" for r in budgets.list_limits())
    # And no false over-budget insight for Transfer / Wire.
    labels = [i.get("label") for i in reports.insights("2026-06") if i.get("kind") == "over_budget"]
    assert not any(lbl and lbl.startswith("Transfer") for lbl in labels)


def test_avg3_suggestions_are_rounded_not_floored(data_dir):
    # M-3: 3-mo average is round()-to-cents, not floor-divided. Averaged over the
    # last 3 FULL months.
    db.init_schema()
    m1, m2, m3 = _last_full_months(3)
    with db.connect() as conn:
        _seed_txn(conn, f"{m1}-10", -5000, "Shopping")
        _seed_txn(conn, f"{m2}-10", -5000, "Shopping")
        _seed_txn(conn, f"{m3}-05", -10000, "Shopping")   # total 20000 over 3 months
    reports.apply_suggestions()
    lim = {r["category"]: r["limit_cents"] for r in budgets.list_limits() if r["subcategory"] is None}
    assert lim["Shopping"] == round(20000 / 3)   # 6667, not floor 6666
    assert lim["Shopping"] == 6667


def test_suggestions_average_last_3_FULL_months_excluding_current(data_dir):
    # The current in-progress month must NOT drag suggestions (income or spending) down.
    db.init_schema()
    cur = reports.current_month()
    m1, m2, m3 = _last_full_months(3)
    with db.connect() as conn:
        for m in (m1, m2, m3):
            _seed_txn(conn, f"{m}-15", 400000, "Income")     # $4000 salary / full month
            _seed_txn(conn, f"{m}-10", -50000, "Groceries")  # $500 groceries / full month
        # tiny current-month amounts that MUST be excluded from the averages
        _seed_txn(conn, f"{cur}-05", 50000, "Income")
        _seed_txn(conn, f"{cur}-03", -2000, "Groceries")
    ov = reports.budget_overview(cur)
    assert ov["suggested_income_cents"] == 400000   # avg salary of the 3 full months, NOT pulled down
    g = [c for c in ov["categories"] if c["category"] == "Groceries"][0]
    assert g["suggested_cents"] == 50000            # spending limit rec, current month excluded


# ── S-2: point-in-time budget resolution (past month uses the then-effective budget) ──
def test_budget_uses_current_limit_for_all_scopes(data_dir):
    # Redesign 2026-06-13: budgets are evaluated against the CURRENT limit in EVERY
    # scope (point-in-time `_as_of` removed) — "how did I do in January vs MY budget?".
    # Groceries last edited to $300; viewing January ($400 spent) compares against the
    # latest $300 → over, pct 133 (NOT the old Jan-effective $500).
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-01-15", -40000, "Groceries")   # $400 Jan spend
    budgets.set_limit("Groceries", 50000, effective_from="2026-01-01")
    budgets.set_limit("Groceries", 30000, effective_from="2026-06-12")   # latest wins everywhere

    jan = [c for c in reports.budget_overview("2026-01")["categories"] if c["category"] == "Groceries"][0]
    assert jan["budget_cents"] == 30000 and jan["monthly_budget_cents"] == 30000
    assert jan["spent_cents"] == 40000 and jan["over"] is True and jan["pct"] == 133


def test_current_budget_shown_for_any_past_month(data_dir):
    # A budget set NOW (June) applies to a January review — the current budget is used
    # for any month, so an old month is never empty just because the budget is recent.
    db.init_schema()
    budgets.set_limit("Groceries", 30000, effective_from="2026-06-12")
    jan = [c for c in reports.budget_overview("2026-01")["categories"] if c["category"] == "Groceries"][0]
    assert jan["budget_cents"] == 30000 and jan["spent_cents"] == 0 and jan["over"] is False


def test_budget_status_uses_current_limit(data_dir):
    # budget_status / insights apply the CURRENT cap to a past month (by design): a later
    # edit to a smaller cap DOES make a past overspend show as over.
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-01-15", -40000, "Groceries")
    budgets.set_limit("Groceries", 50000, effective_from="2026-01-01")
    budgets.set_limit("Groceries", 30000, effective_from="2026-06-12")

    s = reports.month_summary("2026-01")
    grocery = next(b for b in s["budgets"] if b["category"] == "Groceries")
    assert grocery["limit_cents"] == 30000 and grocery["over_cents"] == 10000   # 40000 − 30000, over
    over_labels = [i.get("label") for i in reports.insights("2026-01") if i.get("kind") == "over_budget"]
    assert any(lbl and lbl.startswith("Groceries") for lbl in over_labels)


# ── income sources ("Where your money comes from") ───────────────────────────
def test_income_source_key_merges_variants(data_dir):
    # GoodLeap's stored merchant_norm variants all collapse to one source key.
    for m in ("310977 GOODLEAP DIR", "S REDACTED GOODLEAP", "GOODLEAP LLC DIRECT"):
        assert reports.income_source_key(m) == "Goodleap"
    assert reports.income_source_key("OPTUM SERVICES PAYROLL") == "Optum Services"
    assert reports.income_source_key("CLAIM PAYMENT CLAIM") == "Claim"   # collapse-consecutive-dup step
    assert reports.income_source_key("UNKNOWN") == "Unknown"             # literal-UNKNOWN guard
    assert reports.income_source_key("") == "Unknown"                    # no tokens → Unknown


def test_income_source_key_output_is_metachar_free(data_dir):
    # (i) CA-2: output is alphanumerics + spaces only — no HTML metacharacter survives.
    for m in ("O'DONNELL INC", "AT&T MOBILITY", "A.B.C. CORP", "<img> CO"):
        key = reports.income_source_key(m)
        assert not any(c in key for c in "&'.<>\""), f"{m!r} -> {key!r} carries a metachar"


def test_income_by_source_groups_folds_and_sums(data_dir, tmp_path):
    _import(tmp_path, [
        {"trntype": "CREDIT", "dtposted": "20260601", "amount": "2000.00", "fitid": "A", "name": "GOODLEAP PAYROLL"},
        {"trntype": "CREDIT", "dtposted": "20260615", "amount": "1500.00", "fitid": "B", "name": "310977 GOODLEAP DIR"},
        {"trntype": "CREDIT", "dtposted": "20260605", "amount": "900.00", "fitid": "C", "name": "OPTUM SERVICES DIR"},
        {"trntype": "CREDIT", "dtposted": "20260607", "amount": "5.00", "fitid": "D", "name": "TINY CO"},
    ])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Income' WHERE status='posted'")
        conn.commit()
        rows = reports.income_by_source("2026-06", conn)
        # GoodLeap's two variants merge into one source totaling $3,500.
        gl = next(r for r in rows if r["source"] == "Goodleap")
        assert gl["total_cents"] == 350000 and gl["count"] == 2 and gl["other"] is False
        # source totals (incl. any fold) sum to income_cents on the SAME snapshot (#9).
        inc = reports.month_summary("2026-06", conn)["income_cents"]
        assert sum(r["total_cents"] for r in rows) == inc


def test_income_by_source_folds_tail_beyond_top_n(data_dir, tmp_path):
    # 8 distinct sources → top 6 render, the rest fold into one "Other sources" row.
    txns = [{"trntype": "CREDIT", "dtposted": "20260601", "amount": f"{(i + 1) * 100}.00",
             "fitid": f"S{i}", "name": f"PAYER{i:02d} CORP"} for i in range(8)]
    _import(tmp_path, txns)
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Income' WHERE status='posted'")
        conn.commit()
        rows = reports.income_by_source("2026-06", conn)
        reals = [r for r in rows if not r["other"]]
        fold = [r for r in rows if r["other"]]
        assert len(reals) == reports.INCOME_TOP_N == 6
        assert len(fold) == 1 and fold[0]["source"] == "Other sources" and fold[0]["count"] == 2


def test_income_transactions_drill_and_unmatched(data_dir, tmp_path):
    _import(tmp_path, [
        {"trntype": "CREDIT", "dtposted": "20260601", "amount": "2000.00", "fitid": "A", "name": "GOODLEAP PAYROLL"},
        {"trntype": "CREDIT", "dtposted": "20260615", "amount": "1500.00", "fitid": "B", "name": "GOODLEAP DIR"},
    ])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Income' WHERE status='posted'")
        conn.commit()
        rows = reports.income_transactions("Goodleap", "2026-06", conn)
        assert len(rows) == 2 and sum(r["amount_cents"] for r in rows) == 350000
        assert set(rows[0]) == {"merchant_norm", "amount_cents", "posted_date"}
        assert reports.income_transactions("Nonexistent", "2026-06", conn) == []   # unmatched → []


def test_income_real_other_keyed_payer_drills(data_dir, tmp_path):
    # (e) S1: a real payer normalizing to key "Other" is a clickable source, drills to its own rows.
    _import(tmp_path, [
        {"trntype": "CREDIT", "dtposted": "20260601", "amount": "42.00", "fitid": "O", "name": "OTHER PAYMENTS"},
    ])
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET category='Income' WHERE status='posted'")
        conn.commit()
        assert reports.income_source_key("OTHER PAYMENTS") == "Other"
        rows = reports.income_transactions("Other", "2026-06", conn)   # real "Other", not the fold sentinel
        assert len(rows) == 1 and rows[0]["amount_cents"] == 4200


def test_income_unknown_bucket(data_dir, tmp_path):
    # (c) #3: a merchant_norm="UNKNOWN" income row keys to the "Unknown" source bucket.
    _import(tmp_path, [
        {"trntype": "CREDIT", "dtposted": "20260601", "amount": "10.00", "fitid": "U", "name": ""},
    ])
    with db.connect() as conn:
        # force the merchant_norm to the literal UNKNOWN sentinel (empty payee path)
        conn.execute("UPDATE transactions SET category='Income', merchant_norm='UNKNOWN' WHERE status='posted'")
        conn.commit()
        rows = reports.income_by_source("2026-06", conn)
        assert any(r["source"] == "Unknown" and not r["other"] for r in rows)


# ── timeframe drives all tabs: per-active-month averaging (design 2026-06-12) ──
import pytest  # noqa: E402  (test-only import, grouped with this feature's tests)


@pytest.mark.parametrize("good", ["all", "last1", "last3", "last12", "last999", "2026-01", "2025-12"])
def test_validate_scope_accepts_valid(good):
    # None coerces to ALL — also valid (the GET default).
    reports.validate_scope(good)
    reports.validate_scope(None)


@pytest.mark.parametrize("bad", [
    "lastABC", "last0", "last", "2026-13", "2026-00", "2026-5", "20260-1",
    "garbage", "../etc", "all\n", "last3\n", "2026-01 ", " 2026-01",
])
def test_validate_scope_rejects_malformed(bad):
    with pytest.raises(ValueError):
        reports.validate_scope(bad)


def test_budget_overview_single_month_actual_is_exact_not_averaged(data_dir):
    # A single-month scope divides by 1 active month — the actual is the raw sum,
    # unchanged from before the averaging feature.
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-05-04", -42000, "Groceries")
    budgets.set_limit("Groceries", 50000, effective_from="2026-05-01")  # effective for the May view
    g = [c for c in reports.budget_overview("2026-05")["categories"] if c["category"] == "Groceries"][0]
    assert g["spent_cents"] == 42000 and g["over"] is False and g["pct"] == 84


def test_budget_overview_total_for_period(data_dir, monkeypatch):
    # Redesign 2026-06-13: a multi-month window shows the un-averaged TOTAL spend vs the
    # monthly budget × whole-month factor (NOT a per-active-month average). Data spans
    # Mar–May 2026; today is pinned to 2026-06, so "all" = the 3 completed months (factor 3).
    import datetime as _dt
    monkeypatch.setattr(reports, "date", type("D", (), {"today": staticmethod(lambda: _dt.date(2026, 6, 15))}))
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-03-10", -30000, "Groceries")
        _seed_txn(conn, "2026-04-10", -50000, "Groceries")
        _seed_txn(conn, "2026-05-10", -40000, "Groceries")
        _seed_txn(conn, "2026-04-08", -9000, "Dining Out")
    budgets.set_limit("Groceries", 35000)
    budgets.set_limit("Dining Out", 10000)
    ov = reports.budget_overview("all")
    assert ov["factor"] == 3
    cats = {c["category"]: c for c in ov["categories"]}
    # Groceries: total 120000 vs 35000×3 = 105000 → over. monthly target preserved.
    assert cats["Groceries"]["spent_cents"] == 120000 and cats["Groceries"]["budget_cents"] == 105000
    assert cats["Groceries"]["monthly_budget_cents"] == 35000 and cats["Groceries"]["over"] is True
    # Dining Out: total 9000 vs 10000×3 = 30000 → under (one month of spend, full period budget).
    assert cats["Dining Out"]["spent_cents"] == 9000 and cats["Dining Out"]["budget_cents"] == 30000
    assert cats["Dining Out"]["over"] is False


def test_budget_spent_is_net_and_matches_overview(data_dir):
    # The Budgets tab and the Overview "where your money goes" must agree on a single
    # month: both NET (a refund/credit reduces the category). A debit + a refund in the
    # same category nets to (debit − refund), not the gross debit.
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-05-10", -50000, "Transportation")   # $500 spend
        _seed_txn(conn, "2026-05-20",  20000, "Transportation")   # $200 refund/credit
    budgets.set_limit("Transportation", 40000)
    bo = {c["category"]: c for c in reports.budget_overview("2026-05")["categories"]}["Transportation"]
    ov = reports.month_summary("2026-05")["spend_by_category"]["Transportation"]
    assert bo["spent_cents"] == 30000 and ov == 30000          # net $300, both screens agree
    assert bo["over"] is False                                 # 30000 < 40000 limit


def test_budget_window_excludes_current_in_progress_month(data_dir):
    # Ranges review COMPLETED months only — the current in-progress month is dropped;
    # selecting the current month explicitly still shows it (factor 1, partial spend).
    db.init_schema()
    cur = reports.current_month()
    with db.connect() as conn:
        _seed_txn(conn, f"{cur}-05", -10000, "Groceries")                        # current (live) month
        _seed_txn(conn, f"{reports.prev_month(cur)}-05", -20000, "Groceries")    # last completed month
    budgets.set_limit("Groceries", 50000)
    ov = reports.budget_overview("last1")          # = last COMPLETED month, not the live one
    g = {c["category"]: c for c in ov["categories"]}["Groceries"]
    assert ov["factor"] == 1 and g["spent_cents"] == 20000   # excludes the live month's 10000
    cur_g = {c["category"]: c for c in reports.budget_overview(cur)["categories"]}["Groceries"]
    assert cur_g["spent_cents"] == 10000           # explicit current-month selection shows it


def test_period_factor_counts_completed_months(data_dir):
    # factor = whole-month count of the completed-months window (last3 → 3; all spans
    # first-data..last-completed). The live month is never counted in a range.
    db.init_schema()
    cur_i = reports._month_index(reports.current_month())
    with db.connect() as conn:
        for k in (1, 2, 3):
            _seed_txn(conn, f"{reports._index_to_month(cur_i - k)}-10", -1000, "Groceries")
    assert reports.budget_overview("last3")["factor"] == 3
    assert reports.budget_overview("all")["factor"] == 3     # 3 completed months of data


def test_budget_window_no_completed_months_fallback(data_dir):
    # Only current-month data → ranges fall back to factor 1 over the current month so
    # the tab still renders (rather than an empty completed-months window).
    db.init_schema()
    cur = reports.current_month()
    with db.connect() as conn:
        _seed_txn(conn, f"{cur}-05", -10000, "Groceries")
    budgets.set_limit("Groceries", 50000)
    ov = reports.budget_overview("all")
    assert ov["factor"] == 1
    assert {c["category"]: c for c in ov["categories"]}["Groceries"]["spent_cents"] == 10000


def test_budget_overview_zero_spend_budgeted_category_no_div_by_zero(data_dir):
    # A budgeted category with no spend in scope reports 0 (not a ZeroDivisionError:
    # the divisor is max(1, active_months)).
    db.init_schema()
    budgets.set_limit("Shopping", 5000)
    shop = [c for c in reports.budget_overview("all")["categories"] if c["category"] == "Shopping"][0]
    assert shop["spent_cents"] == 0 and shop["over"] is False and shop["pct"] == 0


def test_budget_overview_income_averaged_per_active_income_month(data_dir):
    # Actual income is averaged per active INCOME month so it compares to the
    # MONTHLY expected income; a single-month scope is exact (÷1).
    db.init_schema()
    with db.connect() as conn:
        for m in ("2026-03", "2026-04", "2026-05"):
            _seed_txn(conn, f"{m}-15", 400000, "Income")
    assert reports.budget_overview("all")["actual_income_cents"] == 400000        # 1.2M / 3 mo
    assert reports.budget_overview("2026-04")["actual_income_cents"] == 400000     # single month, exact


def test_budget_status_agrees_with_overview_total_for_period(data_dir, monkeypatch):
    # CA-2: the over/under verdict from budget_status matches budget_overview for a
    # window scope — both use the same closed window × factor (total-for-period).
    # Today is pinned to 2026-06 so "all" resolves to the 3 completed months (factor 3).
    import datetime as _dt
    monkeypatch.setattr(reports, "date", type("D", (), {"today": staticmethod(lambda: _dt.date(2026, 6, 15))}))
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-03-10", -30000, "Groceries")
        _seed_txn(conn, "2026-04-10", -50000, "Groceries")
        _seed_txn(conn, "2026-05-10", -40000, "Groceries")
    budgets.set_limit("Groceries", 35000)   # set OUTSIDE the seeding txn (own connection)
    with db.connect() as conn:
        row = [r for r in reports.budget_status(conn, "all") if r["category"] == "Groceries"][0]
    ov = {c["category"]: c for c in reports.budget_overview("all")["categories"]}["Groceries"]
    # total 120000 vs 35000×3 = 105000 → over by 15000; status and overview agree.
    assert row["actual_cents"] == 120000 and row["limit_cents"] == 105000 and row["over_cents"] == 15000
    assert ov["spent_cents"] == row["actual_cents"] and ov["over"] is (row["over_cents"] > 0)


def test_budget_overview_floor_category_direction_aware(data_dir):
    # A floor category (category-level AND subcategory-level) flips `over`:
    # under the target is `over: true`; at/above it is `over: false`. The
    # allocation-consistency check `subs_exceed` is unaffected by direction —
    # still plain limit-vs-limit.
    db.init_schema()
    categories.mark_floor_category("Investments")
    with db.connect() as conn:
        _seed_txn(conn, "2026-06-10", -200000, "Investments", subcategory="401k")
    budgets.set_limit("Investments", 300000)               # $3000 target
    budgets.set_limit("Investments", 350000, subcategory="401k")  # sub target > parent
    ov = {c["category"]: c for c in reports.budget_overview("2026-06")["categories"]}["Investments"]
    assert ov["over"] is True   # $2000 spent < $3000 target -> bad
    sub = {s["subcategory"]: s for s in ov["subcategories"]}["401k"]
    assert sub["over"] is True  # same shortfall at the subcategory level
    # subs_exceed: sub_total ($3500) > monthly ($3000) -> True regardless of
    # direction (a pure allocation check, not a spend-vs-target check).
    assert ov["subs_exceed"] is True

    categories.unmark_floor_category("Investments")
    budgets.clear_limit("Investments", subcategory="401k")
    with db.connect() as conn:
        _seed_txn(conn, "2026-06-11", -350000, "Investments")  # now $5500 total spent
    ov2 = {c["category"]: c for c in reports.budget_overview("2026-06")["categories"]}["Investments"]
    assert ov2["over"] is True  # ceiling semantics restored: spend > $3000 limit -> bad


def test_budget_overview_over_cents_direction_aware(data_dir):
    # `over_cents` is a signed, direction-aware magnitude (positive = bad),
    # mirroring `budget_status()`'s own field — added so the dashboard can
    # consume it directly instead of recomputing spent-budget locally (which
    # gets the sign backwards for floor categories).
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-06-05", -30000, "Dining Out")
    budgets.set_limit("Dining Out", 10000)   # $100 limit, $300 spent -> $200 over
    ov = {c["category"]: c for c in reports.budget_overview("2026-06")["categories"]}["Dining Out"]
    assert ov["over_cents"] == 20000

    categories.mark_floor_category("Investments")
    with db.connect() as conn:
        _seed_txn(conn, "2026-06-06", -20000, "Investments")
    budgets.set_limit("Investments", 30000)  # $300 target, $200 spent -> $100 short
    ov2 = {c["category"]: c for c in reports.budget_overview("2026-06")["categories"]}["Investments"]
    assert ov2["over_cents"] == 10000   # positive even though spend is UNDER the target


def test_budget_overview_floor_field(data_dir):
    # Category-level AND subcategory-level dicts carry a `floor` boolean so a
    # downstream chart-builder can key off the payload directly instead of
    # relying on out-of-band memory of a prior mark_floor_category call.
    db.init_schema()
    categories.mark_floor_category("Investments")
    with db.connect() as conn:
        _seed_txn(conn, "2026-06-10", -200000, "Investments", subcategory="401k")
        _seed_txn(conn, "2026-06-11", -30000, "Dining Out")
    budgets.set_limit("Investments", 300000)
    budgets.set_limit("Investments", 350000, subcategory="401k")
    budgets.set_limit("Dining Out", 10000)

    by_cat = {c["category"]: c for c in reports.budget_overview("2026-06")["categories"]}
    inv = by_cat["Investments"]
    assert inv["floor"] is True
    sub = {s["subcategory"]: s for s in inv["subcategories"]}["401k"]
    assert sub["floor"] is True

    dining = by_cat["Dining Out"]
    assert dining["floor"] is False


def test_budget_overview_exposes_onboarded_flag(data_dir):
    # The Budgets-tab payload carries the first-run signal for the setup wizard:
    # False until the budget_onboarded setting is written, then True.
    db.init_schema()
    assert reports.budget_overview("all")["onboarded"] is False
    db.set_setting("budget_onboarded", "1")
    assert reports.budget_overview("all")["onboarded"] is True
