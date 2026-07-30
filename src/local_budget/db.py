"""budget.db — the FULL-PII ledger. The agent/skill layer opens it ONLY via
`agent_connect()`, behind a connection-scoped column-level SQLite authorizer
(design §1): imported facts are immutable to skills and PII columns are
read-denied. The deterministic core uses `connect()` for full read/write.

Schema is idempotent (`init_schema` is safe to call repeatedly). All dates are
TEXT ISO YYYY-MM-DD; all money is INTEGER cents.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id   INTEGER PRIMARY KEY,
    institution  TEXT,
    acct_type    TEXT,
    acct_last4   TEXT,
    acct_hash    TEXT UNIQUE,
    own_account  INTEGER NOT NULL DEFAULT 1,
    nickname     TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    txn_id          INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(account_id),
    fitid           TEXT NOT NULL,
    posted_date     TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'posted',
    txn_type        TEXT,
    payee           TEXT,
    memo            TEXT,
    merchant_norm   TEXT,
    category        TEXT,
    subcategory     TEXT,
    category_source TEXT,
    raw_ofx         TEXT,
    imported_at     TEXT NOT NULL,
    import_run_id   INTEGER,
    UNIQUE (account_id, fitid)
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(posted_date);
CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions(merchant_norm);
CREATE INDEX IF NOT EXISTS idx_txn_acct_date ON transactions(account_id, posted_date);
CREATE INDEX IF NOT EXISTS idx_txn_neardup ON transactions(account_id, amount_cents, posted_date);

CREATE TABLE IF NOT EXISTS import_conflicts (
    conflict_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES accounts(account_id),
    kind                  TEXT NOT NULL,
    fitid                 TEXT,
    existing_txn_id       INTEGER REFERENCES transactions(txn_id) ON DELETE SET NULL,
    incoming_txn_id       INTEGER REFERENCES transactions(txn_id) ON DELETE SET NULL,
    existing_amount_cents INTEGER,
    existing_posted_date  TEXT,
    incoming_amount_cents INTEGER,
    incoming_posted_date  TEXT,
    incoming_payee        TEXT,
    run_id                INTEGER REFERENCES import_runs(run_id),
    detected_at           TEXT NOT NULL,
    resolved              INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS category_rules (
    rule_id     INTEGER PRIMARY KEY,
    pattern     TEXT NOT NULL,
    category    TEXT NOT NULL,
    subcategory TEXT,
    priority    INTEGER NOT NULL DEFAULT 100,
    source      TEXT NOT NULL,
    created_at  TEXT,
    import_run_id INTEGER
);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id      INTEGER PRIMARY KEY,
    category       TEXT NOT NULL,
    subcategory    TEXT,
    limit_cents    INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    created_at     TEXT
);

CREATE TABLE IF NOT EXISTS import_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    status        TEXT NOT NULL,
    source_name   TEXT,
    rows_seen     INTEGER,
    rows_inserted INTEGER,
    rows_skipped  INTEGER,
    rows_conflict INTEGER,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Drop-folder intake: files seen by CONTENT HASH (not filename) so a renamed or
-- re-downloaded file isn't reprocessed, and disposal state is tracked.
CREATE TABLE IF NOT EXISTS inbox_files (
    content_hash TEXT PRIMARY KEY,
    filename     TEXT,                 -- last-seen name (server-side only; never sent to browser)
    state        TEXT NOT NULL,        -- imported | quarantined | errored
    reason       TEXT,                 -- sanitized enum when quarantined
    run_id       INTEGER,
    disposed     INTEGER NOT NULL DEFAULT 0,
    disposed_name TEXT,                  -- ACTUAL basename in processed/ (may be suffixed on collision); undo restores from this, not `filename`
    attempts     INTEGER NOT NULL DEFAULT 0,  -- failed import attempts (transient-error retry, S2)
    recorded_at  TEXT NOT NULL
);

-- Merchant normalization: a raw merchant_norm token/substring -> canonical vendor.
-- PII-free (brand names + sanitized merchant_norm tokens). source: builtin|llm|manual.
CREATE TABLE IF NOT EXISTS merchant_aliases (
    alias_id   INTEGER PRIMARY KEY,
    pattern    TEXT NOT NULL UNIQUE,     -- UPPERCASE token matched against merchant_norm
    canonical  TEXT NOT NULL,            -- display canonical name, e.g. "Anthropic"
    source     TEXT NOT NULL,            -- builtin | llm | manual
    created_at TEXT
);

-- Reversible snapshot for a merchant-normalization apply: prior canonical/subcategory
-- per changed transaction, so `normalize.undo_last()` restores the pre-merge state.
CREATE TABLE IF NOT EXISTS normalize_changes (
    change_id      INTEGER PRIMARY KEY,
    batch_id       INTEGER NOT NULL,
    txn_id         INTEGER NOT NULL,
    old_canonical  TEXT,
    old_subcategory TEXT,
    new_pattern    TEXT,                 -- the llm/manual alias this batch added (for undo)
    created_at     TEXT
);

-- ── Amazon connector ────────────────────────────────────────────────────────
-- Item-level detail behind an otherwise opaque `AMAZON MKTPL` bank charge.
-- These are IMPORTED FACTS, on the same footing as `transactions`: written by
-- the deterministic core through connect(), never by the agent (the authorizer
-- omits them from _AGENT_WRITE_TABLES, so every agent write is denied).
--
-- Deliberately NOT stored, though the source exposes them: order recipient
-- (gift recipients' names and addresses), order_details_link, image_link.
-- None are needed to answer "what did I buy", and each would be one more
-- piece of other people's data to guard.
--
-- ⚠ TWO SIGN CONVENTIONS LIVE HERE, on purpose:
--
--   amazon_transactions.grand_total_cents  SIGNED like the ledger.
--       Negative = a charge, positive = a refund. It is compared directly
--       against transactions.amount_cents by the matcher, so it must agree.
--
--   amazon_orders.* / amazon_items.*       POSITIVE magnitudes.
--       These are prices — what a thing cost — never postings, and are never
--       compared against the ledger. Printing them negated reads as a refund.
--
-- This mirrors the upstream library, whose Order.grand_total is positive while
-- Transaction.grand_total is negative for a charge. Verified against real
-- parser output in tests/test_amazon_contract.py, which fails if either flips.

CREATE TABLE IF NOT EXISTS amazon_sync_runs (
    sync_run_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    completed_at   TEXT,
    status         TEXT NOT NULL,        -- success | failed
    scope          TEXT,                 -- e.g. 'days=60' / 'year=2026'
    orders_seen    INTEGER,
    orders_upserted INTEGER,
    txns_seen      INTEGER,
    txns_upserted  INTEGER,
    error_message  TEXT
);

CREATE TABLE IF NOT EXISTS amazon_orders (
    order_number      TEXT PRIMARY KEY,
    order_placed_date TEXT NOT NULL,
    grand_total_cents INTEGER,
    subtotal_cents    INTEGER,
    tax_cents         INTEGER,
    shipping_cents    INTEGER,
    refund_total_cents INTEGER,
    payment_method    TEXT,
    item_count        INTEGER,
    cancelled         INTEGER NOT NULL DEFAULT 0,
    fetched_at        TEXT NOT NULL,
    sync_run_id       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_az_order_date ON amazon_orders(order_placed_date);

CREATE TABLE IF NOT EXISTS amazon_items (
    item_id          INTEGER PRIMARY KEY,
    order_number     TEXT NOT NULL REFERENCES amazon_orders(order_number),
    -- Position within the order. With asin it forms the natural key a re-sync
    -- upserts on: ASIN alone is not unique (the same item can appear twice in
    -- one order at different prices/conditions).
    line_no          INTEGER NOT NULL,
    asin             TEXT,
    title            TEXT,
    quantity         INTEGER,
    unit_price_cents INTEGER,
    seller           TEXT,
    condition        TEXT,
    UNIQUE (order_number, line_no)
);
CREATE INDEX IF NOT EXISTS idx_az_item_order ON amazon_items(order_number);

-- Amazon's OWN list of card charges. This is the reconciliation key: it is
-- what actually hit the card, which an order total frequently is not (one
-- order ships in three boxes and settles as three charges).
CREATE TABLE IF NOT EXISTS amazon_transactions (
    amazon_txn_id     INTEGER PRIMARY KEY,
    completed_date    TEXT NOT NULL,
    grand_total_cents INTEGER NOT NULL,
    is_refund         INTEGER NOT NULL DEFAULT 0,
    order_number      TEXT,
    payment_method    TEXT,
    seller            TEXT,
    fetched_at        TEXT NOT NULL,
    sync_run_id       INTEGER,
    -- A charge is identified by what it is, not by row order: re-syncing an
    -- overlapping window must update, never duplicate.
    UNIQUE (completed_date, grand_total_cents, order_number, payment_method)
);
CREATE INDEX IF NOT EXISTS idx_az_txn_date ON amazon_transactions(completed_date);

CREATE TABLE IF NOT EXISTS amazon_matches (
    amazon_txn_id INTEGER PRIMARY KEY REFERENCES amazon_transactions(amazon_txn_id),
    txn_id        INTEGER NOT NULL REFERENCES transactions(txn_id),
    confidence    TEXT NOT NULL,         -- exact | windowed | manual
    method        TEXT,
    matched_at    TEXT NOT NULL,
    -- One bank charge maps to at most one Amazon charge and vice versa.
    UNIQUE (txn_id)
);

-- ── Walmart connector ───────────────────────────────────────────────────────
-- Same job as the Amazon block above, against a bigger number: this ledger
-- carries ~$27.9k of Walmart charges to ~$15.4k of Amazon. Same footing too —
-- IMPORTED FACTS, written by the deterministic core through connect(), absent
-- from _AGENT_WRITE_TABLES so every agent write is denied.
--
-- Deliberately NOT stored, though the pages expose them: delivery address,
-- recipient name, driver/tracking detail, card last-4. None are needed to
-- answer "what did I buy".
--
-- ⚠ The SAME TWO SIGN CONVENTIONS as the Amazon block, for the same reasons:
--   walmart_charges.amount_cents        SIGNED like the ledger (charge < 0).
--   walmart_orders.* / walmart_items.*  POSITIVE magnitudes — prices, not
--                                       postings.
--
-- ⚠ AND ONE THING AMAZON DOES NOT HAVE. Amazon publishes its own list of card
-- charges at exactly the granularity the bank posts them. Walmart publishes
-- ORDERS. An order still settles as one or several charges, so walmart_charges
-- is the reconciliation key either way — read from the order's payment lines
-- when they exist, and otherwise SYNTHESIZED from the order total with
-- `derived = 1`. That flag is load-bearing: it is the difference between an
-- observation and an inference, and a report that cannot tell them apart is
-- quietly overstating what it knows.

CREATE TABLE IF NOT EXISTS walmart_sync_runs (
    sync_run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    status           TEXT NOT NULL,       -- success | failed
    scope            TEXT,                -- e.g. 'days=60' / 'backfill'
    orders_seen      INTEGER,
    orders_upserted  INTEGER,
    charges_seen     INTEGER,
    charges_upserted INTEGER,
    error_message    TEXT
);

CREATE TABLE IF NOT EXISTS walmart_orders (
    order_number      TEXT PRIMARY KEY,
    order_placed_date TEXT NOT NULL,
    grand_total_cents INTEGER,
    subtotal_cents    INTEGER,
    tax_cents         INTEGER,
    shipping_cents    INTEGER,
    savings_cents     INTEGER,
    refund_total_cents INTEGER,
    payment_method    TEXT,
    item_count        INTEGER,
    -- 'online' | 'in-store' | NULL. Walmart mixes both into one history, and
    -- they reconcile against different merchant strings (WALMART.COM vs
    -- WM SUPERC…). Keeping the distinction is what lets coverage report the
    -- two honestly instead of averaging a good number with a bad one.
    channel           TEXT,
    cancelled         INTEGER NOT NULL DEFAULT 0,
    -- Has the per-order detail page been fetched? Backfill resumes on THIS, not
    -- on "does it have items": a genuinely empty order (fully cancelled) has no
    -- items and would otherwise be re-fetched on every run, forever.
    detail_fetched    INTEGER NOT NULL DEFAULT 0,
    fetched_at        TEXT NOT NULL,
    sync_run_id       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_wm_order_date ON walmart_orders(order_placed_date);

CREATE TABLE IF NOT EXISTS walmart_items (
    item_id          INTEGER PRIMARY KEY,
    order_number     TEXT NOT NULL REFERENCES walmart_orders(order_number),
    line_no          INTEGER NOT NULL,
    product_id       TEXT,                -- Walmart item number, the ASIN analogue
    title            TEXT,
    quantity         INTEGER,
    unit_price_cents INTEGER,
    seller           TEXT,                -- Walmart, or a marketplace seller
    -- Walmart's own product taxonomy, when the page carries it. Amazon exposes
    -- nothing equivalent, which is why its report has to guess a bucket from
    -- keywords in the title and say so on its face. If this is populated, the
    -- Walmart report can state a fact instead of a reading. NULL is fine and
    -- expected; the report falls back to the same keyword heuristic.
    category         TEXT,
    status           TEXT,                -- delivered | cancelled | substituted…
    UNIQUE (order_number, line_no)
);
CREATE INDEX IF NOT EXISTS idx_wm_item_order ON walmart_items(order_number);

CREATE TABLE IF NOT EXISTS walmart_charges (
    walmart_charge_id INTEGER PRIMARY KEY,
    order_number      TEXT REFERENCES walmart_orders(order_number),
    charged_date      TEXT NOT NULL,
    amount_cents      INTEGER NOT NULL,   -- SIGNED: charge negative, refund positive
    is_refund         INTEGER NOT NULL DEFAULT 0,
    payment_method    TEXT,
    -- 1 = synthesized from the order total because the page showed no payment
    -- line. Reported, never hidden: a derived charge is a guess about WHEN the
    -- card was hit, and a split-shipment order will have exactly one of these
    -- standing in for two or three real ones.
    derived           INTEGER NOT NULL DEFAULT 0,
    fetched_at        TEXT NOT NULL,
    sync_run_id       INTEGER,
    -- A charge is identified by what it is, not by row order: re-syncing an
    -- overlapping window must update, never duplicate.
    UNIQUE (order_number, charged_date, amount_cents, payment_method)
);
CREATE INDEX IF NOT EXISTS idx_wm_charge_date ON walmart_charges(charged_date);

CREATE TABLE IF NOT EXISTS walmart_matches (
    walmart_charge_id INTEGER PRIMARY KEY REFERENCES walmart_charges(walmart_charge_id),
    txn_id            INTEGER NOT NULL REFERENCES transactions(txn_id),
    confidence        TEXT NOT NULL,      -- exact | windowed | manual
    method            TEXT,
    matched_at        TEXT NOT NULL,
    -- One bank charge maps to at most one Walmart charge and vice versa.
    UNIQUE (txn_id)
);

-- ── transaction splits ──────────────────────────────────────────────────────
-- One bank charge, several categories. A mixed order — groceries, a school
-- supply and a household item in one box — otherwise counts entirely against
-- whichever category the merchant rule assigned.
--
-- Splits are DERIVED, on the same footing as transactions.category: the agent
-- may write them (txn_splits is in _AGENT_WRITE_TABLES) while the imported
-- bank row stays immutable. The ledger remains the record of what the bank
-- said; a split only reapportions it.
--
-- THE INVARIANT: a transaction's splits sum to its amount, exactly. Enforced
-- on write in splits.apply() and auditable at any time via splits.verify().
-- Everything else here is bookkeeping; this is the part that must never break,
-- because a violation invents or destroys money in every report downstream.
CREATE TABLE IF NOT EXISTS txn_splits (
    split_id     INTEGER PRIMARY KEY,
    txn_id       INTEGER NOT NULL REFERENCES transactions(txn_id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,        -- signed, ledger convention
    category     TEXT    NOT NULL,
    subcategory  TEXT,
    source       TEXT    NOT NULL,        -- 'amazon' | 'walmart' | 'manual'
    item_ref     TEXT,                    -- ASIN / Walmart item number, from an order line
    note         TEXT,
    created_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_splits_txn ON txn_splits(txn_id);

-- Every category aggregate reads THIS, not `transactions`.
--
-- An unsplit row LEFT JOINs to NULL and yields itself; a split row yields one
-- line per split. That is what makes the whole feature a one-line change at
-- each of the seven aggregate sites instead of a rewrite of each — and why
-- unsplit transactions, the overwhelming majority, are untouched by design.
--
-- Only non-PII columns are exposed: the agent reads this view through the
-- authorizer, which would abort a statement touching payee/memo/raw_ofx.
CREATE VIEW IF NOT EXISTS effective_txns AS
    SELECT t.txn_id,
           t.account_id,
           t.posted_date,
           t.status,
           t.merchant_norm,
           t.canonical_merchant,
           t.txn_type,
           COALESCE(s.category,     t.category)     AS category,
           COALESCE(s.subcategory,  t.subcategory)  AS subcategory,
           COALESCE(s.amount_cents, t.amount_cents) AS amount_cents,
           (s.split_id IS NOT NULL)                 AS is_split
      FROM transactions t
      LEFT JOIN txn_splits s ON s.txn_id = t.txn_id;
"""

def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to existing DBs (idempotent). SQLite has no IF NOT EXISTS for
    ADD COLUMN, so we check pragma first."""
    def cols(table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if "subcategory" not in cols("transactions"):
        conn.execute("ALTER TABLE transactions ADD COLUMN subcategory TEXT")
    if "subcategory" not in cols("category_rules"):
        conn.execute("ALTER TABLE category_rules ADD COLUMN subcategory TEXT")
    if "subcategory" not in cols("budgets"):
        conn.execute("ALTER TABLE budgets ADD COLUMN subcategory TEXT")
    # Intake provenance: the import_runs row that inserted this txn (for undo).
    if "import_run_id" not in cols("transactions"):
        conn.execute("ALTER TABLE transactions ADD COLUMN import_run_id INTEGER")
    # The import_runs row that promoted this rule (for undo of a bad import).
    if "import_run_id" not in cols("category_rules"):
        conn.execute("ALTER TABLE category_rules ADD COLUMN import_run_id INTEGER")
    # Failed-import attempt count so a TRANSIENT error self-heals (retried) while
    # a persistent one is bounded and quarantined (red-team S2).
    if "attempts" not in cols("inbox_files"):
        conn.execute("ALTER TABLE inbox_files ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    # The ACTUAL basename a file was disposed to (may be suffixed on a processed/
    # name collision), so undo restores the CORRECT file, not the original name
    # which could collide with a pre-existing processed/ file (red-team S1).
    if "disposed_name" not in cols("inbox_files"):
        conn.execute("ALTER TABLE inbox_files ADD COLUMN disposed_name TEXT")
    # Canonical vendor identity (merchant normalization). budget.db only — NOT in the
    # agent.db sanitized projection (keeps the frozen TXN_COLUMNS / I13 unchanged).
    if "canonical_merchant" not in cols("transactions"):
        conn.execute("ALTER TABLE transactions ADD COLUMN canonical_merchant TEXT")


def get_db_path() -> Path:
    return paths.budget_db_path()


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Read-write connection to budget.db. Commits on success, rolls back on
    error, hardens file perms on the way out."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # Wait up to 5s for a write lock instead of an instant SQLITE_BUSY, so concurrent
    # writers (e.g. an import vs. a category merge) serialize rather than 500.
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        paths.harden_db_files(path)


@contextmanager
def writer(conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
    """Yield `conn` if given (the caller's transaction owns the commit — e.g. an
    ``agent_connect(write=True)`` CM), else a fresh committing ``connect()``. Lets a
    write helper run standalone (CLI/web) OR threaded under a guarded write conn so
    the authorizer is in the write path (design §1)."""
    if conn is not None:
        yield conn
    else:
        with connect() as c:
            yield c


# ── agent connection: connection-scoped column-level authorizer (design §1) ──
# The agent/skill layer's ONLY door into budget.db. Imported facts are immutable
# to skills; only the derived category columns + app-config tables are writable.
_AGENT_WRITE_COLS = {("transactions", "category"),
                     ("transactions", "subcategory"),
                     ("transactions", "category_source")}
# txn_splits is here for the same reason transactions.category is writable: a
# split is a DERIVED judgment about an imported fact, not the fact itself. The
# bank row stays immutable; only its apportionment is editable.
_AGENT_WRITE_TABLES = {"category_rules", "budgets", "settings", "txn_splits"}
_AGENT_READ_DENY = {("transactions", "raw_ofx"), ("transactions", "payee"),
                    ("transactions", "memo"), ("accounts", "acct_hash"),
                    ("inbox_files", "filename"), ("import_runs", "source_name"),
                    ("import_runs", "error_message"),
                    # Same reasoning as import_runs.error_message: a sync
                    # failure captures str(e) from ANY exception, and a sqlite
                    # error embeds the values it was binding — product titles,
                    # here. The CLI (full access) still prints it in
                    # `budget amazon status`; the agent has no need for it.
                    ("amazon_sync_runs", "error_message"),
                    ("walmart_sync_runs", "error_message")}


def _agent_authorizer(write: bool):
    def auth(action, arg1, arg2, dbname, trigger):  # noqa: ANN001
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_DENY if (arg1, arg2) in _AGENT_READ_DENY else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_UPDATE:
            if not write:
                return sqlite3.SQLITE_DENY
            if (arg1, arg2) in _AGENT_WRITE_COLS or arg1 in _AGENT_WRITE_TABLES:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_OK if (write and arg1 in _AGENT_WRITE_TABLES) else sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_OK if write else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY
    return auth


@contextmanager
def agent_connect(db_path: Path | None = None, write: bool = False) -> Iterator[sqlite3.Connection]:
    """budget.db opened for the AGENT/skill layer behind the column-level
    authorizer (design §1). ``write=False`` denies every write (PRAGMA
    query_only); ``write=True`` allows ONLY {category,subcategory,category_source}
    on transactions + INSERT/UPDATE/DELETE on {category_rules,budgets,settings}.
    Imported facts / status / transactions INSERT-DELETE / every unlisted table
    are denied; raw_ofx/payee/memo/acct_hash/inbox_files.filename/import_runs
    PII columns are read-denied (the statement aborts — the sanitized
    merchant_norm is the agent's only merchant text). ATTACH/PRAGMA/DDL always
    denied.

    Read path relies on ``PRAGMA query_only=ON`` + the authorizer rather than
    ``mode=ro`` (it must — ``write=True`` needs a writable handle); a deliberate
    defense-in-depth choice over a bare read-only handle. PRAGMAs are
    set BEFORE the authorizer, which then denies any further PRAGMA.

    Build constraint: SQLITE_READ fires for columns referenced in an UPDATE's
    WHERE clause, so a write tool MUST filter on non-PII columns only (e.g.
    txn_id) — a WHERE that reads raw_ofx aborts ("access ... is prohibited")."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if not write:
        conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(_agent_authorizer(write))
    try:
        yield conn
        if write:
            conn.commit()
    except Exception:
        if write:
            conn.rollback()
        raise
    finally:
        conn.set_authorizer(None)
        conn.close()
        if write:
            paths.harden_db_files(path)


def init_schema(db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        from . import merchants   # lazy: avoid import cycle (merchants imports db)
        merchants.seed_builtin_aliases(conn)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── settings ─────────────────────────────────────────────────────────────────
def get_setting(key: str, default: str | None = None, conn: sqlite3.Connection | None = None) -> str | None:
    if conn is not None:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
    sql = ("INSERT INTO settings (key, value) VALUES (?, ?) "
           "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
    if conn is not None:
        conn.execute(sql, (key, value))
        return
    with connect() as c:
        c.execute(sql, (key, value))


def all_settings() -> dict[str, str]:
    with connect() as c:
        rows = c.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── HMAC local key for acct_hash (design §3/M1) ──────────────────────────────
def get_or_create_local_key() -> bytes:
    p = paths.local_key_path()
    if p.exists():
        return p.read_bytes()
    key = secrets.token_bytes(32)
    p.write_bytes(key)
    paths._chmod(p, paths.FILE_MODE)
    return key


def acct_hash(bankid: str, acctid: str) -> str:
    """HMAC-SHA256(local_key, bankid|acctid) — not a bare hash (M1)."""
    key = get_or_create_local_key()
    msg = f"{bankid}|{acctid}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


