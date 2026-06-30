"""Merchant normalization: canonical vendor identity (deterministic brand rules +
cached/manual aliases) + retroactive reversible merge (design 2026-06-12)."""
from __future__ import annotations

from local_budget import db, merchants, normalize, reports
from local_budget.categorize.manual import friendly_name


def _seed_subs(conn, merchant_norms):
    conn.execute("INSERT INTO accounts (institution,acct_type,acct_last4,acct_hash,own_account,created_at) "
                 "SELECT 'WF','CHK','1','h',1,'2026-01-01' WHERE NOT EXISTS (SELECT 1 FROM accounts)")
    aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    for i, m in enumerate(merchant_norms):
        conn.execute(
            "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
            "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
            "VALUES (?,?,?,?,'posted','M','M','M',?,'Subscriptions',NULL,'x','',?,1)",
            (aid, f"t{i}", "2026-05-10", -1599, m, "2026-06-01"))


# ── canonical resolution (rules) ─────────────────────────────────────────────
def test_builtin_collapses_anthropic_spellings(data_dir):
    db.init_schema()
    with db.connect() as c:
        al = merchants.active_aliases(c)
    for m in ("ANTHROPIC", "ANTHROPIC CLAUDE", "CLAUDE ANTHROPIC", "PURCHASE ANTHROPIC"):
        assert merchants.canonical_alias(m, al) == "Anthropic"
    # "HLU HULU" still contains the whole token HULU -> collapses via built-in.
    assert merchants.canonical_alias("HLU HULU", al) == "Hulu"
    assert merchants.canonical_alias("HULU", al) == "Hulu"


def test_audible_outranks_amzn(data_dir):
    db.init_schema()
    with db.connect() as c:
        al = merchants.active_aliases(c)
    assert merchants.canonical_alias("AUDIBLE AMZN", al) == "Audible"   # specific service wins
    assert merchants.canonical_alias("AMZN MKTP US", al) == "Amazon"


def test_unaliased_returns_none_keeps_own_display(data_dir):
    db.init_schema()
    with db.connect() as c:
        al = merchants.active_aliases(c)
    assert merchants.canonical_alias("WALMART ACCT 1234", al) is None
    # display falls back to the friendly title-case name (never None)
    assert merchants.canonical_merchant("WALMART ACCT 1234", al)


def test_add_alias_rejects_short_pattern(data_dir):
    db.init_schema()
    try:
        merchants.add_alias("AB", "Foo")
        raise AssertionError("expected ValueError for <3-char pattern")
    except ValueError:
        pass


# ── retroactive apply + undo ─────────────────────────────────────────────────
def test_apply_collapses_subscriptions_and_sets_canonical(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["ANTHROPIC", "ANTHROPIC CLAUDE", "CLAUDE ANTHROPIC",
                          "PURCHASE ANTHROPIC", "HULU", "AUDIBLE AMZN"])
    r = normalize.apply_aliases()
    assert r["txns_updated"] >= 1
    with db.connect() as conn:
        subs = sorted({row["subcategory"] for row in conn.execute(
            "SELECT subcategory FROM transactions WHERE category='Subscriptions'").fetchall()})
        canon = {row["canonical_merchant"] for row in conn.execute(
            "SELECT canonical_merchant FROM transactions").fetchall()}
    assert subs == ["Anthropic", "Audible", "Hulu"]   # 6 spellings -> 3 vendors
    assert "Anthropic" in canon and "Audible" in canon and "Hulu" in canon


def test_undo_restores_prior_state(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["ANTHROPIC", "CLAUDE ANTHROPIC"])
    normalize.apply_aliases()
    u = normalize.undo_last()
    assert u["undone"] and u["restored"] >= 1
    with db.connect() as conn:
        canon = {row["canonical_merchant"] for row in conn.execute(
            "SELECT canonical_merchant FROM transactions").fetchall()}
    assert canon == {None}            # canonical cleared back to NULL


def test_apply_is_idempotent(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["ANTHROPIC", "CLAUDE ANTHROPIC"])
    normalize.apply_aliases()
    second = normalize.apply_aliases()
    assert second["txns_updated"] == 0   # nothing left to change


def test_renamed_subcategory_preserved(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["ANTHROPIC CLAUDE"])
        conn.execute("UPDATE transactions SET subcategory = 'My Custom Claude' "
                     "WHERE merchant_norm = 'ANTHROPIC CLAUDE'")
    normalize.apply_aliases()
    with db.connect() as conn:
        sub = conn.execute("SELECT subcategory FROM transactions WHERE merchant_norm='ANTHROPIC CLAUDE'").fetchone()[0]
    assert sub == "My Custom Claude"   # not clobbered; canonical_merchant column still set
    with db.connect() as conn:
        cm = conn.execute("SELECT canonical_merchant FROM transactions WHERE merchant_norm='ANTHROPIC CLAUDE'").fetchone()[0]
    assert cm == "Anthropic"


# ── confirm() (explicit user-driven alias group) ────────────────────────────
def test_confirm_caps_canonical_to_64_chars(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO ONE"])
    # an over-long canonical is stored truncated to 64 chars.
    normalize.confirm("X" * 200, ["WIDGETCO ONE"])
    with db.connect() as conn:
        cm = conn.execute("SELECT canonical_merchant FROM transactions "
                          "WHERE merchant_norm='WIDGETCO ONE'").fetchone()[0]
    assert cm == "X" * 64


def test_confirm_adds_alias_and_applies(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO ONE", "WIDGETCO TWO"])
    normalize.confirm("WidgetCo", ["WIDGETCO ONE", "WIDGETCO TWO"])
    with db.connect() as conn:
        subs = {row["subcategory"] for row in conn.execute(
            "SELECT subcategory FROM transactions WHERE category='Subscriptions'").fetchall()}
    assert subs == {"WidgetCo"}


# ── FIX 1: confirm tolerates non-string members (drops them) ─────────────────
def test_confirm_drops_non_string_members(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO ONE", "WIDGETCO TWO"])
    # ints are dropped; the valid string still drives the merge (no AttributeError).
    normalize.confirm("WidgetCo", [1, 2, "WIDGETCO ONE", 3])
    with db.connect() as conn:
        subs = {row["subcategory"] for row in conn.execute(
            "SELECT subcategory FROM transactions WHERE merchant_norm='WIDGETCO ONE'").fetchall()}
    assert subs == {"WidgetCo"}
    # all non-string members -> nothing remains -> ValueError (becomes 400 at the API)
    try:
        normalize.confirm("WidgetCo", [1, 2, 3])
        raise AssertionError("expected ValueError when no valid members remain")
    except ValueError:
        pass


# ── FIX 3: undo removes the added alias; a later apply does NOT re-collapse ───
def test_undo_removes_alias_and_no_recollapse(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO ONE", "WIDGETCO TWO"])
    normalize.confirm("WidgetCo", ["WIDGETCO ONE", "WIDGETCO TWO"])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM merchant_aliases WHERE pattern='WIDGETCO ONE'").fetchone()[0] == 1
    normalize.undo_last()
    with db.connect() as conn:
        # the llm/manual alias is gone (durable undo) ...
        assert conn.execute("SELECT COUNT(*) FROM merchant_aliases WHERE pattern='WIDGETCO ONE'").fetchone()[0] == 0
        # ... and a built-in alias was NOT collateral-deleted
        assert conn.execute("SELECT COUNT(*) FROM merchant_aliases WHERE pattern='ANTHROPIC'").fetchone()[0] == 1
    # a subsequent apply must NOT re-collapse the un-aliased vendor
    normalize.apply_aliases()
    with db.connect() as conn:
        canon = {row["canonical_merchant"] for row in conn.execute(
            "SELECT canonical_merchant FROM transactions WHERE merchant_norm LIKE 'WIDGETCO%'").fetchall()}
    assert canon == {None}                        # stays un-normalized after undo


# ── FIX 4: a corrected canonical updates BOTH columns; user rename preserved ──
def test_corrected_canonical_resyncs_subcategory(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO ONE"])
    normalize.confirm("WidgetCo", ["WIDGETCO ONE"])
    with db.connect() as conn:
        r = conn.execute("SELECT canonical_merchant, subcategory FROM transactions "
                         "WHERE merchant_norm='WIDGETCO ONE'").fetchone()
    assert r["canonical_merchant"] == "WidgetCo" and r["subcategory"] == "WidgetCo"
    # A correcting confirm: WidgetCo -> WidgetCorp must move BOTH columns.
    normalize.confirm("WidgetCorp", ["WIDGETCO ONE"])
    with db.connect() as conn:
        r2 = conn.execute("SELECT canonical_merchant, subcategory FROM transactions "
                          "WHERE merchant_norm='WIDGETCO ONE'").fetchone()
    assert r2["canonical_merchant"] == "WidgetCorp" and r2["subcategory"] == "WidgetCorp"


def _seed_row(conn, merchant_norm, category, cents=-5000, date_="2026-06-10"):
    conn.execute(
        "INSERT INTO accounts (institution,acct_type,acct_last4,acct_hash,own_account,created_at) "
        "SELECT 'WF','CHK','1','h',1,'2026-01-01' WHERE NOT EXISTS (SELECT 1 FROM accounts)")
    aid = conn.execute("SELECT account_id FROM accounts LIMIT 1").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    conn.execute(
        "INSERT INTO transactions (account_id,fitid,posted_date,amount_cents,status,txn_type,"
        "payee,memo,merchant_norm,category,subcategory,category_source,raw_ofx,imported_at,import_run_id) "
        "VALUES (?,?,?,?,'posted','M','M','M',?,?,NULL,'x','',?,1)",
        (aid, f"t{n}", date_, cents, merchant_norm, category, "2026-06-01"))


# ── FIX A: canonical_merchant scoped to Subscriptions — no cross-category collapse ──
def test_canonical_scoped_to_subscriptions_only(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_row(conn, "DISNEYPLUS", "Subscriptions", cents=-5000)
        _seed_row(conn, "DISNEY STORE", "Shopping", cents=-5000)
        _seed_row(conn, "WALT DISNEY WORLD", "Travel", cents=-5000)
    normalize.apply_aliases()
    with db.connect() as conn:
        rows = {r["merchant_norm"]: r["canonical_merchant"] for r in conn.execute(
            "SELECT merchant_norm, canonical_merchant FROM transactions").fetchall()}
    # (a) the Subscriptions row collapses to Disney+
    assert rows["DISNEYPLUS"] == "Disney+"
    # (b) the Shopping / Travel rows keep canonical_merchant NULL
    assert rows["DISNEY STORE"] is None
    assert rows["WALT DISNEY WORLD"] is None
    # (c) top_merchants does NOT merge retail/travel spend into the Disney+ row
    s = reports.month_summary("2026-06")
    tm = {m["merchant"]: m for m in s["top_merchants"]}
    assert tm["Disney+"]["spent_cents"] == 5000 and tm["Disney+"]["count"] == 1
    assert "DISNEY STORE" in tm and tm["DISNEY STORE"]["spent_cents"] == 5000
    assert "WALT DISNEY WORLD" in tm and tm["WALT DISNEY WORLD"]["spent_cents"] == 5000


def test_stale_non_sub_canonical_reset_to_null(data_dir):
    # A non-sub row carrying a stale non-NULL canonical from a prior (wider) apply must
    # be reset to NULL by the scoped apply path (change-detection + undo-able snapshot).
    db.init_schema()
    with db.connect() as conn:
        _seed_row(conn, "DISNEY STORE", "Shopping", cents=-5000)
        conn.execute("UPDATE transactions SET canonical_merchant='Disney+' "
                     "WHERE merchant_norm='DISNEY STORE'")
    r = normalize.apply_aliases()
    assert r["txns_updated"] == 1
    with db.connect() as conn:
        cm = conn.execute("SELECT canonical_merchant FROM transactions "
                          "WHERE merchant_norm='DISNEY STORE'").fetchone()[0]
    assert cm is None
    # the reset is undo-able: undo restores the stale value from the snapshot
    normalize.undo_last()
    with db.connect() as conn:
        cm2 = conn.execute("SELECT canonical_merchant FROM transactions "
                           "WHERE merchant_norm='DISNEY STORE'").fetchone()[0]
    assert cm2 == "Disney+"


# ── FIX B: multi-word alias matches by token-subset, not raw substring ──────────
def test_multiword_alias_token_precise_no_overcollapse(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["QORP A"])
    normalize.confirm("Qorp", ["QORP A"])
    # a separately-inserted future merchant sharing the QORP token must NOT resolve.
    with db.connect() as conn:
        _seed_row(conn, "QORP ABC INC", "Subscriptions")
    normalize.apply_aliases()
    with db.connect() as conn:
        rows = {r["merchant_norm"]: r["canonical_merchant"] for r in conn.execute(
            "SELECT merchant_norm, canonical_merchant FROM transactions").fetchall()}
    assert rows["QORP A"] == "Qorp"          # exact pattern still collapses
    assert rows["QORP ABC INC"] is None      # token 'A' not present -> no over-collapse


def test_singleword_manual_alias_exact_no_overcollapse(data_dir):
    """A confirmed SINGLE-token alias ('ACME') must match only an exact bare 'ACME'
    row, never an unrelated 'ACME GYM MEMBERSHIP'. Built-in single tokens stay
    token-anywhere (designed broad match)."""
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["ACME"])
    normalize.confirm("Acme Software", ["ACME"])
    # separately-inserted rows that share / don't share the ACME token set.
    with db.connect() as conn:
        _seed_row(conn, "ACME GYM MEMBERSHIP", "Subscriptions")
        _seed_row(conn, "PURCHASE ANTHROPIC", "Subscriptions")
    normalize.apply_aliases()
    with db.connect() as conn:
        rows = {r["merchant_norm"]: r["canonical_merchant"] for r in conn.execute(
            "SELECT merchant_norm, canonical_merchant FROM transactions").fetchall()}
    assert rows["ACME"] == "Acme Software"           # bare 'ACME' still resolves (exact)
    assert rows["ACME GYM MEMBERSHIP"] is None       # NOT over-collapsed by single-token manual alias
    assert rows["PURCHASE ANTHROPIC"] == "Anthropic"  # built-in token-anywhere preserved


def test_user_renamed_subcategory_survives_reapply(data_dir):
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO ONE"])
    normalize.confirm("WidgetCo", ["WIDGETCO ONE"])
    # A genuine USER rename: sub != canonical_merchant column.
    with db.connect() as conn:
        conn.execute("UPDATE transactions SET subcategory='My WidgetCo' WHERE merchant_norm='WIDGETCO ONE'")
    normalize.apply_aliases()
    with db.connect() as conn:
        sub = conn.execute("SELECT subcategory FROM transactions WHERE merchant_norm='WIDGETCO ONE'").fetchone()[0]
    assert sub == "My WidgetCo"                    # user rename preserved across re-apply


# ── FIX 4: confirming a bare built-in token never downgrades/shadows/deletes it ──
def test_confirm_builtin_token_not_downgraded_or_deleted(data_dir):
    """POSTing confirm() with a member equal to an existing BUILT-IN pattern
    ('ANTHROPIC') must NOT flip its source to manual, must NOT shrink its
    token-anywhere matching, and must NOT let undo_last delete the built-in row."""
    db.init_schema()
    with db.connect() as conn:
        merchants.seed_builtin_aliases(conn)
        _seed_subs(conn, ["ANTHROPIC"])
    normalize.confirm("Something Else", ["ANTHROPIC"])
    # (a) built-in row unchanged: still source=builtin, canonical=Anthropic.
    with db.connect() as conn:
        row = conn.execute(
            "SELECT source, canonical FROM merchant_aliases WHERE pattern='ANTHROPIC'").fetchone()
        assert row["source"] == "builtin"
        assert row["canonical"] == "Anthropic"
        al = merchants.active_aliases(conn)
    # (b) token-anywhere matching preserved.
    assert merchants.canonical_alias("PURCHASE ANTHROPIC", al) == "Anthropic"
    assert merchants.canonical_alias("ANTHROPIC CLAUDE", al) == "Anthropic"
    # (c) a subsequent undo does NOT delete the built-in ANTHROPIC row.
    normalize.undo_last()
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM merchant_aliases WHERE pattern='ANTHROPIC' "
            "AND source='builtin'").fetchone()[0] == 1


# ── FIX 5: undoing a CORRECTION must not delete the shared pre-existing alias ──
def test_confirm_then_correct_then_undo_keeps_shared_alias(data_dir):
    """confirm a canonical, then CORRECT it (re-confirm the same member pattern with a
    new canonical), then undo the correction. The txn snapshot restores the
    pre-correction canonical, but the shared alias (created by the FIRST batch, only
    re-confirmed by the second) must SURVIVE — else the next apply finds no alias and
    silently de-collapses canonical_merchant->NULL / subcategory->raw friendly name."""
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO SUBSCRIPTION"])
    # batch 1: create the alias + collapse.
    normalize.confirm("WidgetCo", ["WIDGETCO SUBSCRIPTION"])
    with db.connect() as conn:
        r = conn.execute("SELECT canonical_merchant, subcategory FROM transactions "
                         "WHERE merchant_norm='WIDGETCO SUBSCRIPTION'").fetchone()
    assert r["canonical_merchant"] == "WidgetCo" and r["subcategory"] == "WidgetCo"
    # batch 2: CORRECT the canonical (re-confirms the SAME pre-existing pattern).
    normalize.confirm("WidgetCorp", ["WIDGETCO SUBSCRIPTION"])
    with db.connect() as conn:
        r2 = conn.execute("SELECT canonical_merchant, subcategory FROM transactions "
                          "WHERE merchant_norm='WIDGETCO SUBSCRIPTION'").fetchone()
    assert r2["canonical_merchant"] == "WidgetCorp" and r2["subcategory"] == "WidgetCorp"
    # undo the correction.
    u = normalize.undo_last()
    assert u["undone"]
    with db.connect() as conn:
        # (a) txn restored to the pre-correction value.
        r3 = conn.execute("SELECT canonical_merchant, subcategory FROM transactions "
                          "WHERE merchant_norm='WIDGETCO SUBSCRIPTION'").fetchone()
        assert r3["canonical_merchant"] == "WidgetCo"
        # (b) the shared alias STILL EXISTS (not collateral-deleted by the undo).
        assert conn.execute(
            "SELECT COUNT(*) FROM merchant_aliases WHERE pattern='WIDGETCO SUBSCRIPTION'"
        ).fetchone()[0] == 1
    # (c) a subsequent apply does NOT silently de-collapse: the shared alias survives,
    # so the row stays collapsed to a real canonical (NOT NULL / raw friendly name).
    # The alias still carries the corrected canonical, so apply re-derives WidgetCorp —
    # the key guarantee is "no silent reset to NULL", which the buggy delete violated.
    raw = friendly_name("WIDGETCO SUBSCRIPTION")
    normalize.apply_aliases()
    with db.connect() as conn:
        r4 = conn.execute("SELECT canonical_merchant, subcategory FROM transactions "
                          "WHERE merchant_norm='WIDGETCO SUBSCRIPTION'").fetchone()
    assert r4["canonical_merchant"] is not None     # NOT silently de-collapsed to NULL
    assert r4["canonical_merchant"] == "WidgetCorp"  # alias survived w/ corrected canonical
    assert r4["subcategory"] != raw                 # subcategory NOT reset to raw friendly name
    assert r4["subcategory"] == "WidgetCorp"


def test_fresh_alias_single_confirm_undo_still_decollapses(data_dir):
    """Regression guard for the correct/expected behavior: a brand-NEW alias created by
    a single confirm IS fully undoable — undo deletes the newly-created alias and the
    next apply de-collapses (a fresh alias should be removed on undo)."""
    db.init_schema()
    with db.connect() as conn:
        _seed_subs(conn, ["WIDGETCO SOLO"])
    normalize.confirm("WidgetCo", ["WIDGETCO SOLO"])
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM merchant_aliases WHERE pattern='WIDGETCO SOLO'"
        ).fetchone()[0] == 1
    normalize.undo_last()
    with db.connect() as conn:
        # the brand-new alias is gone (durable undo).
        assert conn.execute(
            "SELECT COUNT(*) FROM merchant_aliases WHERE pattern='WIDGETCO SOLO'"
        ).fetchone()[0] == 0
    normalize.apply_aliases()
    with db.connect() as conn:
        cm = conn.execute("SELECT canonical_merchant FROM transactions "
                          "WHERE merchant_norm='WIDGETCO SOLO'").fetchone()[0]
    assert cm is None                                # de-collapses, as expected
