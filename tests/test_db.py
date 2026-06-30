"""budget.db schema, settings, generation stamp, HMAC acct_hash."""
from __future__ import annotations

from local_budget import db


def test_init_schema_idempotent(data_dir):
    db.init_schema()
    db.init_schema()  # safe to call repeatedly
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"accounts", "transactions", "import_conflicts", "category_rules",
            "budgets", "import_runs", "settings"} <= tables


def test_settings_roundtrip(data_dir):
    db.init_schema()
    assert db.get_setting("user_name") is None
    db.set_setting("user_name", "Nate")
    assert db.get_setting("user_name") == "Nate"


def test_acct_hash_is_deterministic_and_keyed(data_dir):
    db.init_schema()
    h1 = db.acct_hash("121000248", "1234567")
    h2 = db.acct_hash("121000248", "1234567")
    assert h1 == h2                      # deterministic
    assert h1 != db.acct_hash("121000248", "7654321")  # different acct -> different
    # HMAC, not a bare sha256 of the message.
    import hashlib
    assert h1 != hashlib.sha256(b"121000248|1234567").hexdigest()
