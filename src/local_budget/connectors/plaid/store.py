"""Private persistence and explicitly allowlisted public connection health."""
from datetime import datetime, timezone
import sqlite3


def add_item(conn: sqlite3.Connection, *, environment: str,
             provider_item_id: str, credential_ref: str) -> int:
    """Create a disabled connection within the caller's transaction."""
    row = conn.execute(
        'INSERT INTO plaid_items(environment,provider_item_id,credential_ref,user_handle,created_at) '
        'VALUES(?,?,?,?,?) RETURNING item_id',
        (environment, provider_item_id, credential_ref, user_handle(conn),
         datetime.now(timezone.utc).isoformat(timespec='seconds')),
    ).fetchone()
    return row[0]


def item(conn: sqlite3.Connection, item_id: int) -> dict:
    """Internal metadata only; callers must not log or publicly serialize it."""
    row = conn.execute('SELECT * FROM plaid_items WHERE item_id=?', (item_id,)).fetchone()
    if row is None:
        raise KeyError(item_id)
    return dict(row)


def public_status(conn: sqlite3.Connection) -> list[dict]:
    """Read health without provider calls or exposing private identifiers."""
    return [dict(row) for row in conn.execute(
        'SELECT local_account_id,state,last_attempt_at,last_commit_at,'
        'provider_updated_at,readiness,review_count,safe_error_code '
        'FROM plaid_status_public ORDER BY local_account_id'
    )]


def namespace(conn: sqlite3.Connection) -> str:
    """The persisted ledger identity, retained in consistent database backups."""
    return conn.execute("SELECT value FROM plaid_meta WHERE key='ledger_namespace'").fetchone()[0]


def user_handle(conn: sqlite3.Connection) -> str:
    """Stable opaque user identity shared by links in this ledger."""
    return conn.execute("SELECT value FROM plaid_meta WHERE key='user_handle'").fetchone()[0]


def add_link_session(conn: sqlite3.Connection, *, session_id: str, mode: str,
                     item_id: int | None, credential_ref: str, expires_at: str,
                     environment: str | None = None) -> None:
    """Persist a nonsecret receipt; new links explicitly bind their environment."""
    if mode not in {'link', 'update'} or (mode == 'update') != (item_id is not None):
        raise ValueError('invalid_session')
    if item_id is not None:
        item_environment = item(conn, item_id)['environment']
        if environment is not None and environment != item_environment:
            raise ValueError('invalid_environment')
        environment = item_environment
    if environment not in {'sandbox', 'production'}:
        raise ValueError('invalid_environment')
    conn.execute(
        'INSERT INTO plaid_link_sessions '
        '(session_id,environment,mode,item_id,credential_ref,expires_at,created_at) '
        'VALUES(?,?,?,?,?,?,?)',
        (session_id, environment, mode, item_id, credential_ref, expires_at,
         datetime.now(timezone.utc).isoformat(timespec='seconds')),
    )
