import pytest
from local_budget import db, paths


@pytest.fixture
def plaid_db(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    path = paths.budget_db_path()
    db.init_schema(path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO accounts(account_id,institution,acct_type,acct_last4,acct_hash) VALUES(1,'TEST','checking','1234','test-1')")
    return path


@pytest.fixture
def seed_item(plaid_db):
    def create(provider_id='test-item', environment='sandbox'):
        from local_budget.connectors.plaid import store
        with db.connect(plaid_db) as conn:
            return store.add_item(conn, environment=environment,
                                  provider_item_id=provider_id,
                                  credential_ref=f'test-ref-{provider_id}')
    return create
