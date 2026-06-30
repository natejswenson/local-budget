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
_AGENT_WRITE_TABLES = {"category_rules", "budgets", "settings"}
_AGENT_READ_DENY = {("transactions", "raw_ofx"), ("accounts", "acct_hash"),
                    ("inbox_files", "filename"), ("import_runs", "source_name"),
                    ("import_runs", "error_message")}


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
    are denied; raw_ofx/acct_hash/inbox_files.filename/import_runs PII columns
    are read-denied (the statement aborts). ATTACH/PRAGMA/DDL always denied.

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


