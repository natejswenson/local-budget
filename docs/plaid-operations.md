# Plaid host operations

This connector runs on the owner's macOS host. Bank management is interactive CLI only; dashboard and MCP expose safe health, not credentials or management routes. Scheduled sync never opens a browser or prompts. USD posted transactions contribute to spending; pending and removed transactions do not. Same-user shell access is outside the credential-isolation guarantee.

## Before linking

Confirm Production eligibility, institution and country support, OAuth redirects and Hosted Link settings in the Plaid developer account. v1 accepts US connections and requests only `products=[transactions]`; initial history defaults to 90 days. Set the desired history before linking. Do not enable Auth, Identity, Balance or `/transactions/refresh` for this implementation.

Pricing is **contract-specific**, not a fixed dollar estimate. Obtain a Production quote covering Transactions billing per connected Item, minimums/commitments, additional products, when Item removal stops billing, and any on-demand refresh charges. [Plaid pricing](https://plaid.com/pricing/) describes its available plans; [Transactions pricing](https://plaid.com/docs/transactions/#transactions-pricing) identifies subscription billing and optional Refresh per-request billing. Check the actual [billing terms](https://plaid.com/docs/account/billing/) for your account. These official pages were rechecked on 2026-10-10. No paid provider activity was performed during development.

Polling more often does not guarantee fresher bank data: [Transactions updates](https://plaid.com/docs/transactions/#transactions-updates) depend on institutional update cadence. This feature fetches available updates and does not force bank refreshes. Production cost and data quality require the separately authorized pilot below.

Keep the ledger on private storage. Directories/files use 0700/0600. Keychain labels bind environment, persisted ledger namespace and opaque local handles. There is no plaintext credential fallback. Unlock the login Keychain in the owner's macOS session for interactive work. Scheduled requests prohibit authentication UI and report `credential_locked` or `credential_missing` instead of prompting. Resolve the host condition interactively, then retry manual sync.

## Sandbox workflow and transition

Run from the installed environment with the intended `LOCAL_BUDGET_DATA_DIR`. The examples use connection `1`, provider account `a1`, and local account `1` as placeholders; substitute the opaque handles and account choice shown by your own commands. Never put tokens or API secrets in arguments, shell history, general settings or a plist.

```sh
budget plaid setup --environment sandbox
budget plaid link --environment sandbox --country US --days-requested 90
budget plaid link --resume SESSION
budget plaid accounts 1
budget plaid accounts 1 --create-local a1
budget plaid map 1 a1 --local-account 1 --start-date 2026-10-01
budget plaid preview 1
budget plaid adopt 1 PROVIDER_TRANSACTION --local-txn 123
budget plaid activate 1 a1
budget plaid sync 1
budget plaid status --json
```

Setup prompts for client ID and hidden API secret, verifies secure storage and saves nonsecret configuration. Link confirms sharing and opens Hosted Link using the native host browser integration. Receipts expire after 15 minutes; polling is every five seconds within expiry/backoff. Resume the recorded session, rather than creating a duplicate Link token. A completed Link creates a disabled Item. Accounts discovery displays masked choices; `--create-local` is optional for a new ledger account. Mapping stays disabled.

Choose the transition date from a complete statement, not an arbitrary first fetched date. Keep CSV/QFX imports through the day before the boundary and for unmapped accounts or periods outside provider coverage. Preview fetches full history (and saved-cursor delta when present), shows exclusions and overlaps, and writes no ledger facts or cursor. Historical completion requires `HISTORICAL_UPDATE_COMPLETE`; an empty response does not prove completion. Confirm each exact existing historical transaction with `adopt`; the command shows current provider and local candidate facts and rejects stale or ambiguous review. It binds the existing fact without creating another purchase. Do not blindly adopt every overlap.

Activation fetches again, checks current generation/overlaps, requires interactive confirmation, and applies the reviewed batch atomically. Do not import statements into an active source's post-boundary period. Manual categories and valid manual splits remain editable; amount corrections archive invalid splits, mark review, and immediately change posted report totals. Resolve the visible review queue using corrected facts. Removal tombstones preserve history and exclude spending. All pages and the final cursor commit together; a failed final page leaves prior ledger facts and cursor intact.

## Health and connection recovery

```sh
budget plaid sync --all --non-interactive --json
budget plaid status --json
budget plaid reconnect 1
budget plaid rotate 1
budget plaid rebuild 1
budget plaid disconnect 1
budget plaid replace OLD NEW
```

Status is read-only and makes zero provider calls. It shows local connection handles, readiness, safe error codes, timestamps and counts. Last successful data older than 48 hours is stale. A successful API call alone does not establish complete historical coverage. Each Item run has a five-minute deadline, 500 records per page, at most 50,000 updates and 64 MiB response data, three transient HTTP retries and three pagination restarts. Retry a safe failed sync after addressing its reported condition; do not manually advance its cursor.

Reconnect uses the existing Item's update mode and retains cursor/credential. It repairs consent or login problems; it cannot reconstruct a missing access token. After changing hosts, secure config may remain but its Keychain entries will not: setup can restore the API credential, not the Item token. Recover the original environment/namespace/opaque Keychain reference through an authorized secure host procedure or create a fresh connection and review replacement. Never copy a token into SQLite or settings.

Rotation uses a durable receipt. Re-running `rotate` resumes known locally stored stages, including old-token cleanup, without repeating ambiguous invalidation. If invalidation happened but the new token was not durably stored, the operation remains `recovery_required`; retain receipts and pursue authorized provider/host recovery or a reviewed fresh replacement. Do not erase the receipt or repeat raw invalidation. A lost Keychain reference similarly requires secure recovery, not a plaintext workaround.

An uncertain public-token exchange remains `recovery_required`; `link --resume SESSION` must not blindly exchange again. Retain the local receipt and securely resolve possible provider Item/credential state before creating another connection. Do not infer failure means no billable Item was created.

A rejected cursor reported as `INVALID_FIELD` records private `recovery_required`. There is no automatic reset or relink. Interactive `rebuild CONNECTION` fetches full history, previews current overlaps and coverage, and requires completed history and confirmation. It atomically replaces the saved original cursor with the full next cursor while applying that batch. History absence is not a removal event; preserve existing facts unless explicit removal is received. Declining or failing recovery preserves the prior cursor and facts.

Disconnect confirms interactively, disables locally first, calls `/item/remove`, then cleans credentials after confirmed removal. `disconnect_pending` retains the credential when remote removal is uncertain; `cleanup_needed` means cleanup still needs attention. Retry the same command after resolving the condition. An invalid token alone does not prove removal. Confirm billing cessation against your contract; bank OAuth permission displays may require separate bank-side action.

For replacement, link and map NEW disabled first, then `replace OLD NEW`. Review each displayed candidate and enter exact matching local transaction IDs when prompted; confirm the transfer and retirement. Old historical ownership remains guarded. NEW stays disabled after transfer: run preview and normal explicit `activate NEW PROVIDER_ACCOUNT` before sync automation. Replacement is not remote revocation; address the old provider Item and billing through the authorized lifecycle procedure.

## Backup and whole-database restore

```sh
budget backup --out /ABSOLUTE/DATA/budget-before-plaid.db
```

The destination must be under the data directory or an explicitly configured `backup_root`. This is a consistent SQLite backup containing the ledger namespace, account mappings, cursors, recovery receipts and audit alongside financial facts. Scan/review handling accordingly: it is confidential even though Keychain credentials are excluded. The backup is **not encrypted by this feature**. Choose encrypted storage and encrypted backups as an operator decision; the live pilot requires an encrypted local backup. Keychain tokens are not exported.

`budget backup` copies only SQLite. It does **not** include `data/local_key`, the statement account HMAC identity file. Same-host restore must preserve the original local_key. On a new host securely preserve that original file alongside the database, with owner-only permissions, or statement account identity hashes can change and create duplicate accounts/transactions. Recover Keychain entries separately, preserving the persisted namespace and environment. Never regenerate local_key as a migration shortcut.

Restore procedure (there is no `budget restore` command):

1. Stop dashboard/MCP/CLI writers and intake automation; remove the Plaid schedule as below. Confirm no process holds the ledger open.
2. Keep a private recovery copy of the current complete database and the original local_key. Select a trusted consistent SQLite backup. Do not combine financial rows from one backup with newer mappings/cursors from another.
3. With every connection closed, replace the entire `budget.db` from that backup. Remove obsolete `budget.db-wal`/`budget.db-shm` sidecars only after confirming all processes are stopped; these must not be reused with the restored database. Preserve original local_key and owner-only directory/file permissions.
4. Confirm ledger namespace, account mappings, transition dates and safe status on the intended data directory. Securely recover missing Keychain credentials or review a replacement. A backup before token rotation may reference a token that is now invalid; restoring SQLite cannot reverse provider invalidation or removal.
5. Run manual sync to replay from the restored cursor; compare corrected posted totals and splits to the statement. If the cursor is rejected, follow explicit rebuild review above. Re-enable automation only after successful verification and authorization.

## Opt-in six-hour scheduling

Nothing installs automatically. After a successful live pilot and explicit schedule authorization, pin an installed virtual environment and absolute stable repository/data paths:

```sh
bash ops/install-plaid-sync.sh /ABSOLUTE/REPO /ABSOLUTE/VENV /ABSOLUTE/DATA
```

The installer renders/lints an owner-only plist and uses launchctl bootstrap. It runs at load and every 21600 seconds through `ops/budget-plaid-sync.sh`, invoking the pinned `budget plaid sync --all --non-interactive --json`. Logs are private count/error output under `DATA/plaid-schedule/logs`; review these and safe status for failures. No secrets belong in plists or logs. The Mac must be awake with the owner's session/Keychain available. This does not make unavailable bank data fresher.

To remove the schedule, set DATA to the same absolute directory used at installation:

```sh
launchctl bootout "gui/$(id -u)/com.local-budget.plaid-sync"
rm "$DATA/plaid-schedule/com.local-budget.plaid-sync.plist"
```

Keep private logs for review as needed. Removing a local schedule does not disconnect Items or stop subscription billing. No scheduler installation was performed during implementation.

## Separately authorized live pilot

- Obtain explicit authorization for one institution and one account, cost/eligibility confirmation, sharing consent, and an encrypted local backup with original local_key preserved.
- On macOS verify real native Keychain put/get/delete behavior, locked-Keychain noninteractive failure and no plaintext fallback; confirm OAuth/Hosted Link redirect/resume works for the selected institution.
- Configure Production securely, then use `link --environment production --country US --days-requested 90` with explicit sharing consent. Keep automation off.
- Use the complete statement to agree a transition boundary; discover/map one account, preview full history, explicitly adopt reviewed overlaps, and confirm historical completion before activation.
- Compare first posted report totals and date coverage against that full statement. Verify pending/excluded rows are absent from spending and investigate gaps rather than assuming bank history is complete.
- Run a second incremental sync and replay check: stable IDs, no duplicated facts, atomic cursor, same totals for unchanged observations. Assess a safe correction and reconnect where feasible.
- Preserve manual import coverage for unmapped accounts and older periods. Only after this pilot succeeds and the user separately authorizes installation, enable the six-hour schedule.

Mocked CI verifies contracts and isolation; it does not prove actual Keychain bindings, OAuth behavior, Production eligibility, institution coverage or real bank data quality. Linux imports were checked by simulation, not a real Linux container/native-host pilot.
