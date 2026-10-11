# Plaid Bank Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep approved bank accounts current in the local ledger without leaking credentials, duplicating spending, or losing corrections.

**Architecture:** A macOS host CLI uses Hosted Link and Keychain, then polls Plaid's cursor API. Complete batches commit atomically with mappings, audit, and cursor state. Existing dashboard/MCP processes read financial facts and safe health; they never manage connections or receive credentials.

**Tech Stack:** Python 3.12+, Click, SQLite, HTTPX, PyObjC Security binding on macOS, pytest, macOS launchd. Reuse the existing money, sanitization, category, merchant and report functions.

**Spec:** [Plaid bank synchronization design](2026-10-10-plaid-bank-sync-design.md). Read the design and this plan before execution. The user's instruction to create this plan accepts the design for planning; no runtime changes or Production enrollment have occurred.

## Global Constraints

- Host-only macOS connector; no public webhooks or dashboard bank-management routes.
- USD-only posted spending; pending charges do not appear in v1 spending totals.
- `products=[transactions]`; request 90 days of initial history unless explicitly configured before linking.
- Secrets stay outside the ledger, general settings, dashboard, MCP responses, logs, launchd plists, Git, and database backups.
- Keychain service labels contain environment, persisted ledger namespace, and opaque local handles; no plaintext fallback.
- Owner-only directories/files: 0700/0600. Same-user shell access is outside the credential-isolation guarantee.
- Linking lifetime: 15 minutes; session polling: five seconds, bounded by expiry and backoff.
- Sync limits: 500 records per page, 50,000 updates and 64 MiB response data per run.
- At most three pagination restarts and three transient HTTP retries; five-minute deadline per Item run.
- Certificate-verified HTTPS; fixed provider hosts; Decimal amount parsing; no raw response/exception logging.
- Item locks in ascending local-ID order, then intake lock; `BEGIN IMMEDIATE` before dependent write-validation reads.
- Ledger facts, provider mappings, derived-data invalidation, audit, public health, and cursor commit together.
- Automation off by default; six-hour LaunchAgent schedule after an explicitly authorized successful pilot.
- Stale threshold: 48 hours; historical completion requires `HISTORICAL_UPDATE_COMPLETE`.
- Fabricated fixtures and mocked provider/Keychain in CI; retain the existing no-network-egress fixture.
- Preserve unrelated work. Use an implementation feature worktree at execution time; do not implement on the documentation branch.
- Each task's tests must fail before implementing its behavior, then pass before its scoped commit. Suggested snippets define contracts and critical algorithms, not permission to skip remaining assertions.
- Deliver code with scoped commits, a pushed feature branch and draft PR when credentials permit. Merging, deployment, live linking and schedule installation remain separate authorized actions.

## File map and task dependencies

| File | Responsibility | Tasks |
| --- | --- | --- |
| `src/local_budget/connectors/plaid/types.py` | Immutable normalized DTOs and safe enums | 1 |
| `src/local_budget/connectors/plaid/store.py` | Private schema operations, mappings, ownership, health | 1, 5, 7, 9, 10 |
| `src/local_budget/connectors/plaid/credentials.py` | Lazy native Keychain binding and nonsecret config | 2 |
| `src/local_budget/connectors/plaid/client.py` | Fixed-host HTTP, bounded retry, Decimal decoding | 3 |
| `src/local_budget/connectors/plaid/link.py` | Session receipts, Hosted Link, exchange and reconnect | 4 |
| `src/local_budget/connectors/plaid/locks.py` | Sorted Item file locks | 5 |
| `src/local_budget/connectors/plaid/sync.py` | Fetch and atomic application orchestration | 6, 7 |
| `src/local_budget/connectors/plaid/lifecycle.py` | Revocation and rotation recovery | 9 |
| `src/local_budget/connectors/plaid/cli.py` | Host Click command group | 4, 5, 7, 9, 10, 11 |
| `src/local_budget/db.py` | Schema, private-table authorizer, write isolation helper | 1, 8 |
| `src/local_budget/ingest/importer.py`, `intake.py`, `reconcile.py`, `normalize.py` | Source ownership and undo guards | 8 |
| `src/local_budget/splits.py`, `connectors/amazon/{match,split}.py`, `connectors/walmart/{match,split}.py` | Dependent-value locking and invalidation | 7, 8 |
| `src/local_budget/reports.py`, `agent/tools.py`, `agent/render.py` | Safe read-only connection status | 10 |
| `ops/` and `docs/plaid-operations.md` | Explicit schedule setup, pilot and restore procedures | 11, 12 |

## Host command contract

Commands use opaque local connection/session handles. Provider account IDs are displayed only during interactive mapping, never in scheduled status/logs. No command accepts secret values as options.

| Command | Contract |
| --- | --- |
| `budget plaid setup --environment sandbox` | Prompt for client ID/hidden API secret; verify Keychain storage; save nonsecret config |
| `budget plaid link --environment sandbox --country US --days-requested 90` | Display sharing consent, create receipt, open browser and poll to expiry |
| `budget plaid link --resume SESSION` | Resume the existing receipt without creating another Link token |
| `budget plaid accounts CONNECTION` | Interactive masked discovery and local mapping choices |
| `budget plaid map CONNECTION PROVIDER_ACCOUNT --local-account 1 --start-date 2026-10-01` | Create disabled mapping; never activate as a side effect |
| `budget plaid preview CONNECTION` | Fetch full batch; show overlaps and transition exclusions; write no facts/cursor |
| `budget plaid adopt CONNECTION PROVIDER_TRANSACTION --local-txn 123` | Explicitly approve one reviewed overlap under locks and immediate transaction |
| `budget plaid activate CONNECTION PROVIDER_ACCOUNT` | Fetch again, recheck current overlap/generation and require interactive confirmation |
| `budget plaid sync CONNECTION` | Retrieve and atomically commit available updates |
| `budget plaid sync --all --non-interactive --json` | Scheduled safe output; never prompt or open a browser |
| `budget plaid status --json` | Safe read-only health; zero provider calls |
| `budget plaid reconnect CONNECTION` | Existing Item update mode; retain cursor/credential |
| `budget plaid disconnect CONNECTION` | Confirm in interactive CLI; disable locally, revoke remotely, then clean credentials |
| `budget plaid rotate CONNECTION` | Manual token rotation with durable recovery receipt |
| `budget plaid replace OLD NEW` | Interactive fresh candidate review and explicit ownership transfer |

Production link uses an explicit `--environment production`, requires matching secure config and confirms data sharing. Adoption/replacement commands show current candidate facts and reject stale review or unknown provider IDs; there is no blind automatic adoption. Add commands only when their owning tasks are complete. Tests/ops docs use these names and argument contracts.

Tasks 1–3 establish the foundation. Task 4 links disabled Items; Task 5 maps and previews. Task 6 retrieves batches; Task 7 applies them. Task 8 closes existing write-path races before any live activation. Task 9 handles lifecycle; Task 10 exposes status; Tasks 11–12 verify operations and deliver. Execute sequentially; do not enable a partial implementation against Production.

## Task 1: Private schema, DTOs and authorizer boundary

**Files:** Create `src/local_budget/connectors/plaid/__init__.py`, `types.py`, `store.py`; modify `src/local_budget/db.py`; create `tests/plaid/conftest.py`, `tests/plaid/test_store.py`, `tests/plaid/test_access.py`.

**Interfaces:** Produce frozen `TxnFact(transaction_id: str, account_id: str, posted_date: str, amount_cents: int, merchant_norm: str, payee: str, pending: bool, currency: str = 'USD')`, `SyncBatch(original_cursor: str | None, next_cursor: str, events: tuple[tuple[str, TxnFact | str], ...], readiness: str, request_id: str | None)`, and `SyncResult(outcome: str, inserted: int = 0, updated: int = 0, removed: int = 0, review_count: int = 0)`. Tokens never appear in DTOs. Provider IDs/cursors are private internal DTO fields: disable their dataclass repr and never emit DTOs through public JSON or logs. Safe summaries have explicit allowlisted fields. Produce `store.add_item(conn, *, environment: str, provider_item_id: str, credential_ref: str) -> int`, `store.item(conn, item_id: int) -> dict`, `store.public_status(conn) -> list[dict]`.

- [ ] **Write failing database and access tests.** Create fixtures scoped under `tests/plaid/` so existing tests retain their behavior:

```python
import pytest
from local_budget import db

@pytest.fixture
def plaid_db(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    from local_budget import paths
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
```

```python
import sqlite3
import pytest
from local_budget import db

def test_private_cursor_is_not_agent_readable(plaid_db, seed_item):
    seed_item()
    with db.agent_connect(plaid_db) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute('SELECT cursor FROM plaid_items').fetchall()

def test_public_status_is_not_agent_writable(plaid_db):
    with db.agent_connect(plaid_db, write=True) as conn:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM plaid_status_public")
```

Add idempotent migration, private-table joins/views, column added after migration, `COUNT(*)`, FK-restricted deletion, and one-active-owner assertions. `COUNT(*)` invokes a table read with an empty column name: table-wide denial must cover it too.

- [ ] **Run red:** `uv run pytest tests/plaid/test_store.py tests/plaid/test_access.py -q`. Expect missing connector/schema or denied-read assertion failures.
- [ ] **Implement types and additive schema.** Define all design tables; defaults permit disabled Items before mapping. Required constraints:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS plaid_item_identity
ON plaid_items(environment, provider_item_id);
CREATE UNIQUE INDEX IF NOT EXISTS plaid_active_account
ON plaid_accounts(local_account_id) WHERE enabled=1;
CREATE UNIQUE INDEX IF NOT EXISTS plaid_active_txn
ON plaid_transactions(local_txn_id)
WHERE active_owner=1 AND local_txn_id IS NOT NULL;
```

Use composite provider-map primary keys `(item_id, transaction_id)` and `(item_id, provider_account_id)`. Ledger map FKs use `ON DELETE RESTRICT`. Persist ledger namespace in a private `plaid_meta` table included in backup, not in `settings`; this is the concrete home for the design's persisted namespace. Store minimal latest facts, disposition, and review flags on `plaid_transactions`. `plaid_changes` stores redacted normalized before/after facts and archived dependent records, not raw responses. Use UTC ISO timestamps consistently. Add `normalize_changes.invalidated_reason` by migration.

```python
_AGENT_PRIVATE_TABLES = frozenset({
    'plaid_meta', 'plaid_items', 'plaid_link_sessions', 'plaid_accounts',
    'plaid_transactions', 'plaid_sync_runs', 'plaid_changes',
})
# In the existing SQLITE_READ branch, before its column denylist:
if arg1 in _AGENT_PRIVATE_TABLES:
    return sqlite3.SQLITE_DENY
```

Use these public column names consistently: `local_account_id`, `state`, `last_attempt_at`, `last_commit_at`, `provider_updated_at`, `readiness`, `review_count`, `safe_error_code`. Nullable timestamps default to NULL and review_count defaults to zero. Public status contains only local account ID, safe state, attempt/commit/provider timestamps, readiness and counts; it has no provider IDs. Do not add any Plaid table to agent write allowlists.

- [ ] **Run green:** the two new modules plus `uv run pytest tests/test_db.py tests/test_agent_connect.py tests/test_backup.py -q`.
- [ ] **Commit:** explicitly stage only Task 1 files; `git commit -m 'feat(plaid): add private sync schema and agent access boundary'`.

## Task 2: Native Keychain storage and configuration

**Files:** Create `credentials.py`; modify `pyproject.toml`, `uv.lock`; create `tests/plaid/test_credentials.py`.

**Interfaces:** Produce `CredentialError(code: str)` with safe `str`/repr; `KeychainCredentials(namespace: str, environment: str)` exposing `put(handle: str, secret: str) -> None`, `get(handle: str, *, interactive: bool = False) -> str`, `delete(handle: str) -> None`; and `save_config(path: Path, *, client_id: str, environment: str, namespace: str) -> None`, `load_config(path: Path) -> dict`. Native Security import occurs only when a host credential operation is invoked.

- [ ] **Write failing confidentiality and locked-Keychain tests.** Monkeypatch the native adapter boundary, not platform-wide imports. The concrete read query test is:

```python
def test_background_read_disables_keychain_ui(monkeypatch):
    from local_budget.connectors.plaid import credentials
    calls = []
    def read(service, account, *, interactive):
        calls.append(interactive)
        raise credentials.CredentialError('credential_locked')
    monkeypatch.setattr(credentials, '_native_read', read)
    vault = credentials.KeychainCredentials('test-ledger', 'sandbox')
    with pytest.raises(credentials.CredentialError) as exc:
        vault.get('item-1', interactive=False)
    assert calls == [False]
    assert str(exc.value) == 'credential_locked'
```

Import pytest in that test module. Also test native read returning `SECRET_SENTINEL`, namespace/environment label isolation, missing/locked status mapping, safe repr, overwrite/delete, unsupported Linux without fallback, and 0600 atomic config writes without any secret key.

- [ ] **Run red:** `uv run pytest tests/plaid/test_credentials.py -q`.
- [ ] **Implement native adapter and dependencies.** Promote existing `httpx>=0.27` from dev-only to runtime. Add `pyobjc-framework-Security; sys_platform == 'darwin'`; resolve and lock a supported release with `uv lock`, then `uv sync --dev`. Use Security generic-password items and `SecItemAdd`, `SecItemUpdate`, `SecItemCopyMatching`, `SecItemDelete`; prohibit sync-to-cloud attributes. For noninteractive queries use `kSecUseAuthenticationUIFail`; map `errSecItemNotFound` to missing and denied interaction/authentication to locked. Return only enum errors and chain no secret-bearing native exceptions.

```python
def _service(namespace: str, environment: str) -> str:
    if environment not in {'sandbox', 'production'}:
        raise CredentialError('invalid_environment')
    return f'local-budget.plaid.{namespace}.{environment}'
```

Use direct PyObjC calls; no `security` subprocess, argv secrets, generic keyring backend or plaintext alternative. Write nonsecret config via owner-only temporary file and atomic rename under `paths.data_dir()/plaid/`. Reject unexpected config keys or secret fields. Test binding status signatures with a fabricated, temporary Keychain item only when explicitly authorized on the host; unit tests mock the native functions.

- [ ] **Run green:** `uv run pytest tests/plaid/test_credentials.py -q`; verify Linux/container imports remain functional with macOS dependencies excluded.
- [ ] **Commit:** `git commit -m 'feat(plaid): store credentials in native macOS Keychain'` after staging the four scoped files.

## Task 3: Restricted HTTP client and normalization

**Files:** Create `client.py`; create `tests/plaid/test_client.py`, `tests/plaid/test_normalize.py`.

**Interfaces:** Produce `PlaidError(code: str, request_id: str | None = None, http_status: int | None = None)`; `PlaidClient(client_id: str, secret: str, environment: str, *, transport=None)` with `call(endpoint: str, body: dict, *, deadline: float | None = None) -> dict`; `normalize_txn(row: dict) -> TxnFact`. `deadline` is an absolute monotonic-clock value. Caller owns client lifetime through a context manager scoped to one link operation or one Item run. One shared budget of three transient retries is consumed across all calls/pages/health checks in that operation; a pagination restart does not reset it. The all-Items CLI creates a fresh client scope per Item. Expose `bytes_received: int`, a monotonic cumulative count of streamed response bytes, so Task 6 can enforce its whole-run limit even across retries.

- [ ] **Write failing amount and leak tests.**

```python
from decimal import Decimal
from local_budget.connectors.plaid.client import normalize_txn

def test_purchase_uses_ledger_sign():
    fact = normalize_txn({'transaction_id':'p1', 'account_id':'a1',
        'date':'2026-10-01', 'amount':Decimal('12.34'), 'pending':False,
        'iso_currency_code':'USD', 'name':'COFFEE 123456789'})
    assert fact.amount_cents == -1234
    assert '123456789' not in fact.payee
```

Use `httpx.MockTransport` to return a JSON number `0.29`; assert normalized amount is exactly -29. Test refunds/payments, sub-cent and NaN rejection, absent IDs, invalid date/currency, account mismatch later in application, hostile merchant text, unknown error body containing a sentinel, fixed-host URL checks, endpoint allowlist, request timeouts and bounded retries.

- [ ] **Run red:** `uv run pytest tests/plaid/test_client.py tests/plaid/test_normalize.py -q`.
- [ ] **Implement client and conversion.**

```python
HOSTS = {'sandbox':'https://sandbox.plaid.com',
         'production':'https://production.plaid.com'}
ENDPOINTS = frozenset({'/link/token/create', '/link/token/get',
    '/item/public_token/exchange', '/accounts/get', '/item/get',
    '/transactions/sync', '/item/remove', '/item/access_token/invalidate'})
# Stream and bound response bytes before decoding:
payload = json.loads(response_bytes, parse_float=Decimal)
cents = -money.cents_from_amount_str(str(row['amount']))
```

Import json/Decimal/money where used. HTTPX: `verify=True`, `follow_redirects=False`, `trust_env=False`, explicit connect/pool/write timeouts of 10 seconds and read timeout of 30 seconds, capped by remaining deadline. This avoids environment proxies and arbitrary redirected credential destinations. Serialize credential request bodies without logging them; keep the secret out of repr. Read error codes through an allowlist and discard error messages/body. Retry idempotent reads only up to the spec limit; never blindly retry public-token exchange or access-token rotation after ambiguous transport failure. Respect bounded Retry-After with injected clock/sleep seams.

- [ ] **Run green:** new modules plus `uv run pytest tests/test_money.py tests/test_sanitize.py tests/test_parse_hostile.py -q`.
- [ ] **Commit:** `git commit -m 'feat(plaid): add bounded provider client and exact amount conversion'`.

## Task 4: Hosted Link sessions and host command entry point

**Files:** Create `link.py`, `connectors/plaid/cli.py`; modify root `cli.py`; create `tests/plaid/test_link.py`, `tests/plaid/test_cli.py`.

**Interfaces:** Produce `link.start(db_path: Path, client: PlaidClient, vault: KeychainCredentials, *, country: str, now: datetime, days_requested: int = 90, existing_item: int | None = None) -> str` returning a local session handle; `link.resume(db_path, client, vault, session_id: str, *, now: datetime) -> int | None` returning linked Item ID when complete; `link.open_session_url(url: str) -> None`. The URL is never returned through CLI JSON or status DTO. Register the host Click group `plaid`; use the command contract above as subsequent tasks land.

- [ ] **Write failing lifecycle tests.** Fake provider responses via the Task 3 MockTransport. Test user cancellation, expiry, resume after process restart, wrong environment/session, duplicated successful result, Keychain write failure, and access-token exchange uncertainty.

```python
def test_update_mode_never_exchanges_public_token(plaid_db, seed_item):
    from datetime import datetime, timezone
    from local_budget import db
    from local_budget.connectors.plaid import link, store
    item_id = seed_item()
    calls = []
    class Vault:
        def get(self, handle, *, interactive=False):
            assert handle == 'link-ref'
            return 'FABRICATED_LINK_TOKEN'
    class Client:
        def call(self, endpoint, body, *, deadline=None):
            calls.append(endpoint)
            return {'link_sessions':[{'finished_at':'2026-10-10T12:00:00Z',
                'results':{'item_add_results':[]}, 'exit':None}]}
    with db.connect(plaid_db) as conn:
        store.add_link_session(conn, session_id='test-session', mode='update',
            item_id=item_id, credential_ref='link-ref',
            expires_at='2026-10-10T12:15:00+00:00')
    result = link.resume(plaid_db, Client(), Vault(), 'test-session',
        now=datetime(2026, 10, 10, 12, 1, tzinfo=timezone.utc))
    assert result == item_id
    assert calls == ['/link/token/get']
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item_id)['credential_ref'] == 'test-ref-test-item'
```

Produce `store.add_link_session(conn, *, session_id: str, mode: str, item_id: int | None, credential_ref: str, expires_at: str) -> None` in this task. Production tests use the Task 3 MockTransport; the test above isolates the receipt state machine. Full vault fakes implement put/get/delete with a dict. Update mode must retain the existing credential and never exchange any returned public token. A finished receipt returns the existing Item handle but is not proof of restored access; keep reconnect-required state until the Task 10 Item/accounts health checks confirm recovery. Test an exited/cancelled update session separately.

- [ ] **Run red:** `uv run pytest tests/plaid/test_link.py tests/plaid/test_cli.py -q`.
- [ ] **Implement session flow.** Persist Link token in Keychain and nonsecret receipt before browser launch; validate returned host `secure.plaid.com`, HTTPS and absent URL credentials. Use `hosted_link={'url_lifetime_seconds':900, 'is_mobile_app':False}`, `transactions={'days_requested':days_requested}`, `client_name='Local Budget'`, `language='en'`, random persisted user ID and `country_codes=[country]`. New links request `products=['transactions']`; update-mode creation sends the existing access token and omits products and history configuration. Validate requested history against the documented supported range before creating a token. No webhook, delivery method or completion redirect. Process results only once; bind environment/mode/Item from the stored receipt. Launch the validated hosted URL through `open_session_url` inside `start` after its receipt commits, returning only the local handle. Save exchanged credentials before disabled Item metadata; on ambiguity record recovery-required without recreating an Item.

```python
@click.group()
def plaid():
    """Manage host bank synchronization."""

# In root cli.py; connector modules must not eagerly import native Security:
from .connectors.plaid.cli import plaid
main.add_command(plaid)
```

CLI setup uses `click.prompt(..., hide_input=True)` and no secret options/env fallback. Production link presents data-sharing consent and requires explicit environment. Scheduled paths never launch the browser. Link polling uses a bounded five-second cadence and resumes stored sessions instead of issuing a second create request.

- [ ] **Run green:** new modules plus `uv run pytest tests/test_web.py tests/test_mcp_server.py -q`; prove no Plaid management route exists on the dashboard.
- [ ] **Commit:** `git commit -m 'feat(plaid): link bank connections through host-only Hosted Link'`.

## Task 5: Mapping, review preview and Item locking

**Files:** Create `locks.py`; extend `store.py`, Plaid `cli.py`; create `tests/plaid/test_mapping.py`, `tests/plaid/test_locks.py`.

**Interfaces:** Produce `locks.item_locks(db_path: Path, item_ids: list[int]) -> ContextManager[bool]`; `store.map_account(conn, *, item_id: int, provider_account_id: str, local_account_id: int, currency: str, start_date: str) -> None`; `store.enable_account(conn, *, item_id: int, provider_account_id: str, reviewed_generation: int, batch: SyncBatch) -> None`; `store.preview(conn, item_id: int, batch: SyncBatch) -> dict`. Preview consumes the fetch interface added in Task 6; mapping unit tests pass a constructed batch now. `start_date` is inclusive and an account stays disabled until reviewed activation.

- [ ] **Write failing mapping and overlap tests.**

```python
def test_mapping_does_not_enable_ingestion(plaid_db, seed_item):
    from local_budget import db
    from local_budget.connectors.plaid import store
    item = seed_item()
    with db.connect(plaid_db) as conn:
        store.map_account(conn, item_id=item, provider_account_id='a1',
            local_account_id=1, currency='USD', start_date='2026-10-01')
        row = conn.execute('SELECT enabled FROM plaid_accounts').fetchone()
        assert row['enabled'] == 0
```

Test same-last-four distinct accounts, non-USD/type mismatch, duplicate enabled owner, new account discovery disabled, mapping generation change, two real identical charges, preview producing no cursor/ledger writes, and sorted lock contention with two processes on a temp ledger. Seed fabricated account rows through existing SQLite fixtures.

- [ ] **Run red:** `uv run pytest tests/plaid/test_mapping.py tests/plaid/test_locks.py -q`.
- [ ] **Implement local state and locking.** Store flock files in an owner-only ledger-local Plaid directory; sort/deduplicate Item IDs before nonblocking acquisition and release in reverse order. Mapping writes use Item lock then BEGIN IMMEDIATE, increment mapping generation, and reject active-source duplication. Provider-only local accounts use a namespaced HMAC key and a provider mask, never a fabricated full number. Preview gives candidate matches and unresolved overlap counts without silently adopting any row. The transition date must be user supplied after a complete statement period, never the latest transaction date.

```python
def eligible(fact: TxnFact, account: dict) -> bool:
    return (account['enabled'] == 1 and not fact.pending
            and fact.currency == 'USD'
            and fact.posted_date >= account['start_date'])
```

Put `eligible` in store.py for Task 7. Activation must run a fresh preview against the current ledger under the protected transaction; it records the reviewed generation and refuses unknown overlap. CLI financial previews remain interactive; they are not scheduled logs.

- [ ] **Run green:** mapping/locks tests and `uv run pytest tests/test_import.py tests/test_csv.py -q`.
- [ ] **Commit:** `git commit -m 'feat(plaid): require account mapping and history review before ingestion'`.

## Task 6: Complete, bounded cursor retrieval

**Files:** Create `sync.py`; create `tests/plaid/test_fetch.py`.

**Interfaces:** Consume Task 3 `PlaidClient.call` and `bytes_received`; produce `fetch_batch(client: PlaidClient, access_token: str, *, cursor: str | None, deadline: float) -> SyncBatch`. It performs no SQLite or Keychain writes. Events retain page order; each event is `('added'|'modified', TxnFact)` or `('removed', transaction_id: str)`.

- [ ] **Write the pagination-restart test.**

```python
import time
from local_budget.connectors.plaid.client import PlaidError
from local_budget.connectors.plaid.sync import fetch_batch

def test_pagination_restart_discards_intermediate_cursor():
    seen = []
    class Client:
        bytes_received = 0
        def call(self, endpoint, body, *, deadline=None):
            seen.append(body.get('cursor'))
            self.bytes_received += 100
            if len(seen) == 2:
                raise PlaidError('TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION')
            return {'added':[], 'modified':[], 'removed':[],
                'has_more':len(seen) == 1, 'next_cursor':'final' if len(seen) == 3 else 'partial',
                'transactions_update_status':'HISTORICAL_UPDATE_COMPLETE'}
    result = fetch_batch(Client(), 'FABRICATED_ACCESS_TOKEN',
                         cursor='original', deadline=time.monotonic() + 300)
    assert seen == ['original', 'partial', 'original']
    assert result.original_cursor == 'original'
    assert result.next_cursor == 'final'
    assert result.events == ()
```

- [ ] **Add failing limit and ordering tests.** Cover empty-not-ready history, repeated/nonadvancing cursors while `has_more=True` (an empty completed batch may retain its cursor), deadline expiry, exactly-on-limit versus over-limit bytes/records, retries consuming the same budget, missing required keys, and inconsistent same-ID events within a page. Reject ambiguous conflicting facts instead of guessing chronology. Across pages, preserve event order and collapse each provider ID to its final observation during application. Do not interpret a missing account in transactions as revoked access.
- [ ] **Run red:** `uv run pytest tests/plaid/test_fetch.py -q`.
- [ ] **Implement the bounded fetch loop.** Request `count=500`; omit cursor on the first fetch when None. Retain the original cursor across restarts, discard accumulated events after mutation, and keep byte/update counters cumulative across the entire run, including discarded pages. Only return after `has_more=False`.

```python
def check_fetch_limits(*, updates: int, response_bytes: int,
                       deadline: float, now: float) -> None:
    if now >= deadline:
        raise PlaidError('sync_deadline')
    if updates > 50_000 or response_bytes > 64 * 1024 * 1024:
        raise PlaidError('sync_limit')
```

Put this helper in `sync.py`; call it before each request and after each page. At most three restarts follow the initial attempt. Transaction readiness comes from the response enum; retain unknown readiness as safe `unknown`, never ready. Provider timestamps may be absent; represent them as unknown rather than setting them to the local clock.

- [ ] **Run green:** fetch tests plus Task 3 client/normalization tests.
- [ ] **Commit:** `git commit -m 'feat(plaid): fetch bounded complete cursor batches'`.

## Task 7: Atomic ledger application and correction handling

**Files:** Extend `sync.py`, `store.py`; modify `src/local_budget/connectors/amazon/match.py`, `amazon/split.py`, `walmart/match.py`, `walmart/split.py` only where their derived-record interfaces require changes; create `tests/plaid/test_apply.py`, `tests/plaid/test_corrections.py`.

**Interfaces:** Produce `apply_batch(conn, item_id: int, batch: SyncBatch, *, expected_generation: int) -> SyncResult`, called only in a caller-owned immediate transaction with intake lock held; `sync_item(db_path: Path, item_id: int, client: PlaidClient, vault: KeychainCredentials, *, now: datetime) -> SyncResult`; `store.adopt_transaction(conn, *, item_id: int, provider_transaction_id: str, local_txn_id: int, fact: TxnFact) -> None` for explicitly reviewed historical matches. Define `SyncStateError(code: str)` with safe messages. Extend the Item store row with integer `mapping_generation`, nullable `cursor` and safe lifecycle state. CLI exposes one Item or all eligible Items without revealing identifiers or financial facts in scheduled output.

- [ ] **Write the all-or-nothing test.**

```python
import pytest
from local_budget import db
from local_budget.connectors.plaid import store
from local_budget.connectors.plaid.sync import apply_batch, SyncStateError
from local_budget.connectors.plaid.types import SyncBatch

def test_generation_race_never_advances_cursor(plaid_db, seed_item):
    item_id = seed_item()
    batch = SyncBatch(None, 'new-cursor', (), 'HISTORICAL_UPDATE_COMPLETE', None)
    with db.connect(plaid_db) as conn:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(SyncStateError, match='mapping_changed'):
            apply_batch(conn, item_id, batch,
                        expected_generation=store.item(conn, item_id)['mapping_generation'] + 1)
        assert store.item(conn, item_id)['cursor'] is None
```

- [ ] **Add failing ledger behavior tests.** Seed an enabled reviewed mapping and a posted fact; assert duplicate batch replay does not duplicate rows. Assert provider modification keeps local `txn_id`, preserves manual category/subcategory, recomputes merchant-derived fields and invalidates affected normalization snapshots. Assert removal becomes a tombstone excluded from monthly totals; removed-before-ever-added creates a minimal provider map and no ledger expense. Pending records never count; pending-to-posted replacement yields one charge. Two genuine equal purchases remain two charges. A fact for another provider account fails the batch before commit.
- [ ] **Add failing derived-data tests.** An amount correction archives and removes splits whose sum no longer matches; reports immediately count the corrected unsplit amount and set review-needed. A merchant/date/amount change invalidates dependent Amazon/Walmart matches and generated splits according to their actual dependencies. Preserve valid manual splits when parent amount is unchanged. Removed charges have no active merchant match/splits. Mark any affected `normalize_changes` entry invalidated. Inject failure after ledger/map/audit writes but before cursor write and assert outer rollback restores every table.
- [ ] **Run red:** `uv run pytest tests/plaid/test_apply.py tests/plaid/test_corrections.py -q`.
- [ ] **Implement the commit boundary.** Fetch while holding only the Item lock; acquire `inbox_adapter.intake_lock()` afterward. Then start `BEGIN IMMEDIATE` before reading cursor, generation, enabled state, source ownership or parent amounts. Recheck all expected state; apply final per-ID events, derived invalidations and audit; update public health and cursor last; caller commits once. Do not acquire network responses while holding SQLite's write lock.

```python
def require_generation(item: dict, expected: int) -> None:
    if item['mapping_generation'] != expected:
        raise SyncStateError('mapping_changed')

def require_cursor(item: dict, batch: SyncBatch) -> None:
    if item['cursor'] != batch.original_cursor:
        raise SyncStateError('cursor_changed')
```

Put both helpers in `sync.py`; check generation/cursor before lifecycle eligibility so a stale request reports the correct race. Before any mutation, validate all final facts and mapped account relationships. Use synthetic local FITIDs derived from environment/Item/provider transaction identity, never match providers to bank OFX FITIDs. Store no raw provider JSON in `raw_ofx`. Retain inactive ownership maps and audit so replays cannot seize a transaction currently owned by a replacement Item. Pre-boundary facts remain excluded unless explicitly adopted after review. Record safe failed-attempt health in a separate short transaction after a rollback, without changing cursor or last successful commit time.

- [ ] **Run green:** new tests plus `uv run pytest tests/test_reports.py tests/test_splits.py tests/test_normalize.py tests/test_amazon_connector.py tests/test_walmart_connector.py -q`.
- [ ] **Commit:** `git commit -m 'feat(plaid): apply corrections and cursor state atomically'`.

## Task 8: Protect existing imports, undo, reset and split writes

**Files:** Modify `src/local_budget/db.py`, `splits.py`, `intake.py`, `ingest/importer.py`, `reconcile.py`, `normalize.py`, root `cli.py`; update affected Amazon/Walmart split callers; create `tests/plaid/test_existing_writes.py`, `tests/plaid/test_concurrency.py`.

**Interfaces:** Produce `store.is_provider_owned(conn, txn_id: int) -> bool`, `store.assert_file_import_allowed(conn, rows: list[tuple[int, str]]) -> None`, and `ProviderOwnershipError(code: str)`. Each row is `(resolved_local_account_id, posted_date)` for every parsed record in the file. Preserve existing import/undo/reconcile signatures and return contracts, adding their established safe error shape. Produce `db.begin_immediate(conn) -> None`: start immediate before dependent reads if no transaction is active; callers already in a transaction must have obtained the immediate write reservation before their first dependent read. Do not claim a deferred transaction was made safe by checking only `conn.in_transaction`.

- [ ] **Write the reset-refusal test.**

```python
from click.testing import CliRunner
from local_budget import db
from local_budget.cli import main

def test_reset_refuses_existing_provider_mapping(plaid_db, seed_item):
    from local_budget.connectors.plaid import store
    item_id = seed_item()
    with db.connect(plaid_db) as conn:
        store.map_account(conn, item_id=item_id, provider_account_id='a1',
            local_account_id=1, currency='USD', start_date='2026-10-01')
    result = CliRunner().invoke(main, ['reset', '--yes'])
    assert result.exit_code != 0
    assert 'Plaid' in result.output
    with db.connect(plaid_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM plaid_accounts').fetchone()[0] == 1
```

Task 1 uses `paths.budget_db_path()`, so this root CLI test and the fixture exercise the same ledger. Keep the caller's data-dir fixture in scope.

- [ ] **Add failing source-protection tests.** A mixed-account OFX/CSV containing any row in an enabled account's Plaid-owned date range rejects the whole file with zero transaction/conflict writes and no successful file-import record. Historical-only imports and unmapped accounts remain usable. Import collision, reconciliation replacement/delete and generic undo cannot alter provider-owned facts or remove mappings; refuse an entire undo batch if any affected row is protected. Normalization undo refuses the entire stale batch, leaving aliases and transactions unchanged. Reset refuses while any provider account/transaction mapping exists, including inactive maps needed for recovery; `--yes` bypasses only the prompt.
- [ ] **Add a real SQLite race test.** With two connections/processes and synchronization events, hold an immediate split-validation transaction while the other process attempts a parent correction. Assert correction cannot interleave between parent validation and split insertion. In the opposite order, the split writer reads the new amount and refuses an invalid total. Use bounded joins and release events in `finally`, not sleep-based timing assertions.
- [ ] **Run red:** `uv run pytest tests/plaid/test_existing_writes.py tests/plaid/test_concurrency.py -q`.
- [ ] **Implement early guard and locking changes.** Parse the entire file before its write transaction; under intake lock and immediate transaction, inspect every account/row before inserting file-level transaction/conflict metadata. Intake may already have opened its shared batch run; retain that bookkeeping and record a safe source-overlap rejection instead of claiming the whole intake batch never existed. Reuse `inbox_adapter.intake_lock()` without recursively reacquiring it: direct CLI imports acquire it at their entry point, while existing intake passes its already-held lock through a narrow internal call path. Existing importer-to-account resolution stays authoritative. Apply source guards before reconcile/undo destructive DML. Mark stale normalization batches during Task 7; refuse stale undo before restoring any snapshot or deleting alias rows.

```python
def begin_immediate(conn) -> None:
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')

# Root reset: inside an immediate transaction, before its first DELETE.
if conn.execute('SELECT 1 FROM plaid_accounts LIMIT 1').fetchone():
    raise click.ClickException('Plaid mappings exist; reset is unavailable. Use documented restore recovery.')
```

The helper only handles the no-transaction case. Audit every split caller and move its write reservation before dependent reads; retain the existing `splits.apply` API. Existing write transactions that already executed DML hold a reservation; transactions that performed only SELECT must be reorganized. Keep supported category/manual split edits available. Do not add blanket read denial to ordinary ledger facts.

- [ ] **Run green:** new tests plus `uv run pytest tests/test_import.py tests/test_csv.py tests/test_intake_dedup.py tests/test_inbox_adapter.py tests/test_normalize.py tests/test_splits.py tests/test_write_tools.py tests/test_amazon_connector.py tests/test_walmart_connector.py -q`.
- [ ] **Commit:** `git commit -m 'fix(plaid): guard source ownership and concurrent ledger edits'`.

## Task 9: Disconnect, update mode, replacement and recovery

**Files:** Create `lifecycle.py`; extend `store.py`, `link.py`, Plaid `cli.py`; create `tests/plaid/test_lifecycle.py`, `tests/plaid/test_recovery.py`.

**Interfaces:** Produce `disconnect(db_path: Path, item_id: int, client: PlaidClient, vault: KeychainCredentials) -> str`, `rotate_token(db_path: Path, item_id: int, client: PlaidClient, vault: KeychainCredentials) -> str`, `transfer_ownership(conn, *, old_item_id: int, new_item_id: int, matches: dict[str, int], reviewed_generation: int) -> None`. Lifecycle results are safe enum strings. `matches` maps reviewed new provider transaction IDs to existing local transaction IDs; the new provider facts must already exist as excluded review candidates. Replacement takes both Item locks in sorted order and one immediate transaction; it is an explicit reviewed operation, never an automatic relink behavior.

- [ ] **Write the uncertain-removal test.**

```python
from local_budget import db
from local_budget.connectors.plaid import store
from local_budget.connectors.plaid.client import PlaidError
from local_budget.connectors.plaid.lifecycle import disconnect

def test_uncertain_removal_retains_recovery_credential(plaid_db, seed_item):
    item_id = seed_item()
    deleted = []
    class Vault:
        def get(self, handle, *, interactive=False):
            return 'FABRICATED_ACCESS_TOKEN'
        def delete(self, handle):
            deleted.append(handle)
    class Client:
        def call(self, endpoint, body, *, deadline=None):
            raise PlaidError('transport_uncertain')
    assert disconnect(plaid_db, item_id, Client(), Vault()) == 'disconnect_pending'
    assert deleted == []
    with db.connect(plaid_db) as conn:
        assert store.item(conn, item_id)['state'] == 'disconnect_pending'
```

- [ ] **Add failing lifecycle and restore tests.** Confirm remote remove before deleting credential, retry pending disconnect safely, missing credential, update-mode reconnect preserving Item/cursor, explicit replacement uniqueness transfer, old Item replay unable to mutate transferred facts, and crash before/after Keychain rotation. Restore a whole DB backup at an older cursor into a test path; replay deterministic fixture pages and verify stable totals, transaction IDs and namespace. Invalid cursor requires a reviewed preview rebuild; a missing credential requires reconnection guidance rather than repeated silent link creation.
- [ ] **Run red:** `uv run pytest tests/plaid/test_lifecycle.py tests/plaid/test_recovery.py -q`.
- [ ] **Implement persisted state transitions.**

```python
LIFECYCLE_STATES = frozenset({
    'disabled', 'active', 'reconnect_required', 'recovery_required',
    'disconnect_pending', 'disconnected', 'replaced', 'cleanup_needed',
})
```

Define the enum set in `types.py` and validate stores against it. Disable locally and commit pending removal before remote calls. On confirmed removal, mark disconnected and delete credentials; if deletion fails, retain cleanup-needed state without reenabling. Never erase ledger history on disconnect. Rotation writes the new token to a new opaque Keychain handle, then atomically switches DB reference, then deletes old handle. Persist a rotation receipt before the API request; ambiguous exchange/rotation remains recovery-required, since the provider and local Keychain are not one atomic system. No blind retry of an uncertain rotation. Confirm provider error meanings against current official docs before treating any error as proof of revocation.

- [ ] **Run green:** lifecycle/recovery tests plus `uv run pytest tests/test_backup.py -q`.
- [ ] **Commit:** `git commit -m 'feat(plaid): add recoverable connection lifecycle operations'`.

## Task 10: Safe read-only health and truthful freshness

**Files:** Extend `store.py`, `sync.py`, Plaid `cli.py`; modify `src/local_budget/reports.py`, `agent/tools.py`, `agent/render.py` only if rendering needs a helper; create `tests/plaid/test_status.py`.

**Interfaces:** Produce `reports.bank_sync_status(conn=None, *, now: datetime | None = None) -> list[dict]`, reading only `plaid_status_public`; add registry tool `budget_bank_sync_status` through existing `TOOL_SPECS`, using its read-only connection wrapper. Produce `store.refresh_item_health(conn, *, item_id: int, item_response: dict, accounts_response: dict, now: datetime) -> None` fed by sanitized client responses after `/item/get` and `/accounts/get`. No access token or provider IDs enter tool metadata/results.

- [ ] **Write the stale-status test.**

```python
from datetime import datetime, timezone
from local_budget import db, reports

def test_recent_attempt_does_not_hide_stale_commit(plaid_db):
    with db.connect(plaid_db) as conn:
        conn.execute('INSERT INTO plaid_status_public '
            '(local_account_id,state,last_attempt_at,last_commit_at,readiness,review_count) '
            'VALUES(1,?,?,?,?,0)', ('active','2026-10-10T11:59:00+00:00',
            '2026-10-07T12:00:00+00:00','HISTORICAL_UPDATE_COMPLETE'))
        rows = reports.bank_sync_status(conn,
            now=datetime(2026,10,10,12,tzinfo=timezone.utc))
    assert rows[0]['stale'] is True
    assert rows[0]['provider_updated_at'] is None
```

- [ ] **Add failing confidentiality/readiness tests.** A commit with empty data and incomplete history remains initial-sync; historical completion is explicit. Assert 48-hour boundary, old provider timestamp despite a recent commit, unavailable provider timestamp, locked credentials/reconnect/pending disconnect/partial failures, and removed versus newly discovered accounts. The registry tool runs through agent_connect and cannot read private tables. Sentinel credentials/provider IDs never appear in CLI JSON, MCP output, safe health, logs or errors.
- [ ] **Run red:** `uv run pytest tests/plaid/test_status.py -q`.
- [ ] **Implement safe status projection.**

```python
def is_stale(last_commit_at: datetime | None, now: datetime,
             provider_updated_at: datetime | None = None) -> bool:
    observed = provider_updated_at or last_commit_at
    return observed is None or (now - observed).total_seconds() >= 48 * 3600
```

Put the helper in `reports.py`. Show local attempt, committed sync and provider timestamps with distinct labels; stale uses available provider update time, falling back to last commit. Do not turn a recent polling attempt into bank freshness. Normal sync calls the bounded Item/accounts health reads; successful discovery adds unmapped accounts disabled. Explicit revoked-account access disables that mapping and increments generation. Failed discovery never enables an account or implies revocation from absent transaction rows. Readiness and access health are separate fields. Expose counts and masked/local labels only; no management tools or new dashboard UI is required for v1.

- [ ] **Run green:** status tests plus `uv run pytest tests/test_agent_tools.py tests/test_read_tools_phase4.py tests/test_mcp_server.py tests/test_reports.py -q`.
- [ ] **Commit:** `git commit -m 'feat(plaid): expose safe connection health and freshness'`.

## Task 11: Opt-in host scheduler and operational commands

**Files:** Create `ops/budget-plaid-sync.sh`, `ops/com.local-budget.plaid-sync.plist.template`, `ops/install-plaid-sync.sh`; modify `ops/README.md`; create `tests/plaid/test_schedule.py`. Do not install a LaunchAgent while implementing or testing.

**Interfaces:** `budget plaid sync --all --non-interactive --json` uses only safe summaries. Exit 0 means completed/no eligible Items; nonzero indicates credential/configuration/fetch failure. Per-Item failures do not prevent other eligible Items from being attempted. JSON includes local counts/state/code, never financial transaction details. Installer takes absolute repository/venv/data paths and renders escaped plist values; it uses the venv's pinned installed CLI entry point.

- [ ] **Write the scheduling contract test.**

```python
import plistlib
from pathlib import Path

def test_schedule_template_is_opt_in_and_six_hourly():
    root = Path(__file__).resolve().parents[2]
    payload = plistlib.loads((root / 'ops/com.local-budget.plaid-sync.plist.template').read_bytes())
    assert payload['StartInterval'] == 21600
    assert payload['RunAtLoad'] is True
    assert 'KeepAlive' not in payload
    assert 'EnvironmentVariables' not in payload
```

- [ ] **Add failing automation tests.** Rendering paths with spaces produces valid plist XML and literal arguments. Locked Keychain returns safe failure without browser/prompts; lock contention skips safely; one bad Item does not stop the next; deadline/limit errors never advance cursor. Scheduler tests mock launchctl and filesystem targets under tmp_path. Test the wrapper never calls `uv`, activates a shell profile or embeds secret values.
- [ ] **Run red:** `uv run pytest tests/plaid/test_schedule.py -q`.
- [ ] **Implement wrapper and explicit installer.** Use a concrete Bash wrapper with owner-only output and a single pinned command:

```bash
#!/bin/bash
set -euo pipefail
umask 077
if [[ $# -ne 2 ]]; then
  printf '%s\n' 'usage: budget-plaid-sync.sh ABSOLUTE_BUDGET_BINARY ABSOLUTE_DATA_DIR' >&2
  exit 64
fi
export LOCAL_BUDGET_DATA_DIR="$2"
exec "$1" plaid sync --all --non-interactive --json
```

The plist passes those two absolute paths in ProgramArguments, sets StartInterval=21600 and RunAtLoad=true, and directs safe stdout/stderr to owner-only paths. Render plist via Python `plistlib`, not sed substitution, then `plutil -lint`. Installation is an explicit operator action after pilot success; document launchctl bootstrap/bootout and removal. No database secrets in launchd environment. Explain Mac awake/login requirements and that six-hour polling retrieves Plaid's available data rather than forcing banks to refresh.

- [ ] **Run green:** schedule tests; `bash -n ops/budget-plaid-sync.sh ops/install-plaid-sync.sh`; lint a rendered temporary plist with `plutil -lint`. Do not bootstrap it.
- [ ] **Commit:** `git commit -m 'feat(plaid): add opt-in six-hour host synchronization schedule'`.

## Task 12: Integrated acceptance, cost controls and reviewable delivery

**Files:** Create `tests/plaid/test_acceptance.py`, `docs/plaid-operations.md`; update repository `README.md` with a short link. Keep detailed procedures in the operations document.

**Interfaces:** No new runtime abstraction. Consume the completed CLI, store, mocked HTTP transport, fake vault, actual SQLite and current report interfaces.

- [ ] **Write the end-to-end fixture test.** Start with a fabricated linked disabled Item; map one local account; fetch history; assert preview leaves database/cursor unchanged; explicitly review and activate; sync two posted transactions; assign a manual category and valid split; sync one amount/merchant correction and one removal; assert reports show only corrected posted spending, manual category retained, invalid splits archived, review flag visible and exactly one cursor commit per successful batch. Inject a final-page failure and assert all ledger and cursor values remain from the prior successful batch. Restore the previous full database backup and replay the same pages; assert identical final facts and totals.

```python
from local_budget import db

def test_backup_has_namespace_but_no_credentials(plaid_db, seed_item):
    seed_item()
    with db.connect(plaid_db) as conn:
        dump = '\n'.join(conn.iterdump())
    assert 'plaid_meta' in dump
    assert 'FABRICATED_ACCESS_TOKEN' not in dump
    assert 'FABRICATED_CLIENT_SECRET' not in dump
```

Use sentinel tokens in the acceptance fake vault so the negative assertions exercise an actual source of secrets. Add output/log capture, backup byte scan and agent-read tests, including joins and views; do not inspect only filenames. Scan all changed code/test/docs fixtures for real credentials and accidentally captured bank data.

- [ ] **Run red:** `uv run pytest tests/plaid/test_acceptance.py -q` before adding the integration fixture/procedure wiring; lower-level unit tests are already green. If integration already passes, add the missing behavioral assertion before claiming a red cycle.
- [ ] **Write concrete operator procedures.** Include the exact implemented commands for Sandbox setup, linking, mapping, boundary preview, explicit historical adoption, activation, sync, status, reconnect, disconnect, rotation/recovery, scheduler installation/removal and restore. Document Keychain unlock behavior, missing credentials after moving hosts, whole-DB restore, invalid cursor recovery, uncertain exchange, lost Keychain reference and database backup confidentiality. Database backup excludes Keychain and is not encrypted by this feature; document encryption of storage/backups as an operator decision.

Record pricing as **contract-specific, not a fixed dollar estimate**. Obtain a Production quote for Transactions billing per connected Item, minimums/commitments, additional products, Item removal and any on-demand refresh charges. Request only Transactions; do not add Auth/Identity/Balance or `/transactions/refresh` to v1. Polling more often does not guarantee fresher bank data. Document that costs, Production eligibility, supported institutions/countries and redirect/Hosted Link settings must be confirmed in the developer account before live pilot. Cite the official pricing and product documentation already linked in the design; recheck them at implementation time.

- [ ] **Run the required repository checks.**

```bash
uv run ruff check .
uv run pytest --cov=local_budget --cov-report=term-missing --cov-fail-under=80
uv run python scripts/eval.py --mock
bash scripts/secret-scan.sh
npx -y @natjswenson/press@0.7.2 check --repo .
```

The existing secret-scan script checks filenames, so also review staged contents for secrets. Inspect the final diff, unexpected files/dependencies, all management surfaces, and native imports under Linux. A mocked CI pass cannot prove real Keychain behavior or institution data quality; report those limits accurately.

- [ ] **Commit and deliver review.** Stage scoped files only, create the final scoped commit, push the implementation feature branch and open a draft PR. Include actual observed check results and remaining host/Production validation. Do not merge or install the scheduler as part of delivery.
- [ ] **Prepare the live pilot checklist for a separately authorized run.** One institution and one account, encrypted local backup, agreed transition date from a complete statement, explicit overlap adoption, first full history complete, statement totals compared, second incremental run/replay checked, then correction/reconnect behavior assessed where safe. Enable the six-hour schedule only after the user authorizes installation. Keep a manual import path for unmapped accounts and historical periods.

## Review and release gates

1. **Foundation gate (Tasks 1–6):** Private access boundary, Keychain secrecy, fixed-host HTTP and bounded fetch tested. Linking remains disabled for ingestion.
2. **Ledger safety gate (Tasks 7–9):** Atomic corrections/cursor, source protection, race tests, ownership transfer and restore tests pass. No live activation until this gate is complete.
3. **Delivery gate (Tasks 10–12):** Safe status, scheduler dry run, full required CI checks and draft PR are reviewable.
4. **Live gate:** Production quote/access confirmed and user authorizes the one-account pilot. Schedule install remains a distinct authorized action after successful pilot.

## Plan self-review record

Before execution, verify the actual worktree against these paths and interfaces. This plan was reviewed against the accepted design for privacy boundaries, lifecycle uncertainty, duplicate prevention, correction/split invariants, locking, restore and pricing. The concrete additions are the private namespace table and refusal of legacy reset while mappings exist. Neither changes the host-only architecture. The implementation must resolve native binding behavior with host tests and current provider response fixtures; mocks alone are insufficient evidence for those two integrations.

## Official references rechecked for this plan

- [Plaid Link API](https://plaid.com/docs/api/link/): Hosted Link configuration, API result retrieval and update-mode request differences.
- [Hosted Link](https://plaid.com/docs/link/hosted-link/): browser flow and integration requirements.
- [Transactions API](https://plaid.com/docs/api/products/transactions/): cursor pagination, mutation recovery and historical-readiness status.

Rechecked on 2026-10-10. Native macOS and live institution verification are execution-time checks; no real credentials or bank sessions were used to prepare this plan.
