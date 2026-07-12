"""Auto roll-up of variant subscription sub-budgets during normalization."""
from __future__ import annotations

from local_budget import budgets, db, merchants, normalize


def _seed_txn(conn, date_, cents, merchant, subcategory, category="Subscriptions"):
    conn.execute(
        "INSERT INTO accounts (institution,acct_type,acct_last4,acct_hash,own_account,created_at) "
        "SELECT 'BANK','CHK','1','h',1,'2026-01-01' WHERE NOT EXISTS (SELECT 1 FROM accounts)")
    aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.execute(
        "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
        "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
        "VALUES (?,?,?,?,'posted','M','M','M',?,?,?,'x','',?,1)",
        (aid, f"t{n}", date_, cents, merchant, category, subcategory, "2026-06-20"))


def _aliases(conn):
    merchants.seed_builtin_aliases(conn)
    return merchants.active_aliases(conn)


# ── the core reconciliation (unit) ───────────────────────────────────────────
def test_orphan_subbudget_collapses_into_canonical(data_dir):
    db.init_schema()
    with db.connect() as conn:
        # canonical Anthropic has transactions across 2 months ($50 + $50 → avg $50/mo)
        _seed_txn(conn, "2026-04-10", -5000, "ANTHROPIC ANTHROPIC.COM", "Anthropic")
        _seed_txn(conn, "2026-05-10", -5000, "CLAUDE.AI ANTHROPIC", "Anthropic")
    budgets.set_limit("Subscriptions", 4500, subcategory="Anthropic")          # canonical budget
    budgets.set_limit("Subscriptions", 2000, subcategory="Anthropic Claude")   # ORPHAN (no txns)
    budgets.set_limit("Subscriptions", 5211, subcategory="Claude Anthropic")   # ORPHAN (no txns)

    with db.connect() as conn:
        merged = normalize._reconcile_subscription_budgets(
            conn, _aliases(conn), real_subs={"Anthropic"})
    assert merged == 2
    active = {s: c for (cat, s), c in _active_subs().items()}
    assert "Anthropic Claude" not in active and "Claude Anthropic" not in active   # orphans gone
    assert active["Anthropic"] == 5000   # survivor = avg-per-active-month spend ($50), NOT 4500/2000/5211/sum


def _active_subs():
    with db.connect() as conn:
        return {(c, s): v for (c, s), v in budgets.active_limits(conn).items() if c == "Subscriptions"}


def test_transaction_backed_guard_preserves_deliberate_splits(data_dir):
    # Two subcategories that BOTH have spend and both canonicalize to 'Apple' must NOT be
    # merged — only a zero-spend orphan is ever a source.
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-05-01", -1100, "APPLE MUSIC", "Apple Music")
        _seed_txn(conn, "2026-05-02", -700, "APPLE TV", "Apple TV")
    budgets.set_limit("Subscriptions", 1100, subcategory="Apple Music")
    budgets.set_limit("Subscriptions", 700, subcategory="Apple TV")
    with db.connect() as conn:
        merged = normalize._reconcile_subscription_budgets(
            conn, _aliases(conn), real_subs={"Apple Music", "Apple TV"})
    assert merged == 0
    active = _active_subs()
    assert active[("Subscriptions", "Apple Music")] == 1100
    assert active[("Subscriptions", "Apple TV")] == 700


def test_orphan_with_no_real_canonical_is_left_alone(data_dir):
    db.init_schema()
    budgets.set_limit("Subscriptions", 2000, subcategory="Widgetco One")   # no txns, canon == itself
    with db.connect() as conn:
        merged = normalize._reconcile_subscription_budgets(conn, _aliases(conn), real_subs=set())
    assert merged == 0
    assert _active_subs().get(("Subscriptions", "Widgetco One")) == 2000


def test_annual_subscription_gets_nonzero_survivor(data_dir):
    # A once-a-year charge outside the trailing 3 full months still yields a non-zero
    # survivor (all-history average), not 0 / not the stale budget.
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2025-08-01", -12000, "AUDIBLE AMZN.COM BILL", "Audible")   # last year
    budgets.set_limit("Subscriptions", 999, subcategory="Audible")
    budgets.set_limit("Subscriptions", 999, subcategory="Audible Amzn")   # orphan
    with db.connect() as conn:
        merged = normalize._reconcile_subscription_budgets(
            conn, _aliases(conn), real_subs={"Audible"})
    assert merged == 1
    assert _active_subs()["Subscriptions", "Audible"] == 12000   # all-history avg (1 month), not 0


def test_reconcile_is_idempotent(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-05-10", -1395, "HLU HULU.COM BILL", "Hulu")
    budgets.set_limit("Subscriptions", 1395, subcategory="Hlu Hulu")   # orphan
    with db.connect() as conn:
        first = normalize._reconcile_subscription_budgets(conn, _aliases(conn), real_subs={"Hulu"})
        second = normalize._reconcile_subscription_budgets(conn, _aliases(conn), real_subs={"Hulu"})
    assert first == 1 and second == 0   # nothing left to merge on the second pass


# ── end-to-end through apply_aliases ─────────────────────────────────────────
def test_apply_aliases_collapses_orphan_budgets_end_to_end(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_txn(conn, "2026-04-10", -1395, "HLU HULU.COM BILL", "Hulu")
        _seed_txn(conn, "2026-05-10", -1395, "HULU", "Hulu")
    budgets.set_limit("Subscriptions", 1395, subcategory="Hulu")
    budgets.set_limit("Subscriptions", 1395, subcategory="Hlu Hulu")   # orphan from a prior split
    r = normalize.apply_aliases()
    assert r["budgets_merged"] == 1
    active = _active_subs()
    assert ("Subscriptions", "Hlu Hulu") not in active
    assert active["Subscriptions", "Hulu"] == 1395
