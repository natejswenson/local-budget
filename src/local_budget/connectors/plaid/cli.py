"""Interactive macOS commands. Native modules are loaded only at use time."""
from datetime import datetime, timezone
import sys
import time

import click

from local_budget import db, paths
from . import link as hosted_link, store
from .client import PlaidClient, PlaidError
from .credentials import (CredentialError, KeychainCredentials, client_credential_handle,
                          load_config, save_config)


def _host():
    if sys.platform != 'darwin':
        raise click.ClickException('unsupported_platform')


def _config_path(environment):
    return paths.data_dir() / 'plaid' / f'{environment}.json'


def _identity():
    db.init_schema()
    with db.connect() as conn:
        return store.namespace(conn)


def _configured(environment, *, interactive=True):
    namespace = _identity()
    config = load_config(_config_path(environment))
    if config['environment'] != environment or config['namespace'] != namespace:
        raise CredentialError('invalid_config')
    vault = KeychainCredentials(namespace, environment)
    secret = vault.get(client_credential_handle(namespace, environment), interactive=interactive)
    return config, vault, secret


def _now():
    return datetime.now(timezone.utc)


def _poll(path, client, vault, session):
    """One transport instance and one receipt expiry bound every poll."""
    click.echo(f'Session: {session}')
    while True:
        result = hosted_link.resume(path, client, vault, session, now=_now())
        if result is not None:
            click.echo(f'Connection: {result}')
            return
        with db.connect(path) as conn:
            row = conn.execute('SELECT state,expires_at FROM plaid_link_sessions WHERE session_id=?',
                               (session,)).fetchone()
        if row['state'] in {'cancelled', 'expired'}:
            click.echo(f"Session {row['state']}: {session}")
            return
        remaining = (datetime.fromisoformat(row['expires_at']) - _now()).total_seconds()
        if remaining > 0:
            time.sleep(min(5, remaining))


def _run(environment, *, country='US', days_requested=90, session=None, item=None):
    _host()
    path = db.get_db_path()
    try:
        config, vault, secret = _configured(environment)
        remaining = 900
        if session is not None:
            with db.connect(path) as conn:
                row = conn.execute('SELECT environment,expires_at FROM plaid_link_sessions WHERE session_id=?',
                                   (session,)).fetchone()
            if row is None or row['environment'] != environment:
                raise hosted_link.LinkError('invalid_session')
            remaining = (datetime.fromisoformat(row['expires_at']) - _now()).total_seconds()
            # Expired receipts still receive their durable local terminal state.
            if remaining <= 0:
                with PlaidClient(config['client_id'], secret, environment) as client:
                    _poll(path, client, vault, session)
                return
        else:
            click.confirm('Share bank account and transaction data with Plaid and this local ledger?',
                          abort=True)
        with PlaidClient(config['client_id'], secret, environment,
                         deadline=time.monotonic() + remaining) as client:
            if session is None:
                session = hosted_link.start(path, client, vault, country=country, now=_now(),
                                            days_requested=days_requested, existing_item=item)
            _poll(path, client, vault, session)
    except (CredentialError, PlaidError, hosted_link.LinkError) as error:
        if isinstance(error, hosted_link.LinkError) and error.session_id is not None:
            click.echo(f'Session: {error.session_id}')
        raise click.ClickException(error.code) from None


@click.group()
def plaid():
    """Manage host bank synchronization."""


@plaid.command()
@click.option('--environment', type=click.Choice(['sandbox', 'production']), required=True)
def setup(environment):
    """Store API credentials in this host's Keychain."""
    _host()
    try:
        namespace = _identity()
        vault = KeychainCredentials(namespace, environment)
        client_id = click.prompt('Plaid client ID').strip()
        secret = click.prompt('Plaid API secret', hide_input=True).strip()
        handle = client_credential_handle(namespace, environment)
        vault.put(handle, secret)
        if vault.get(handle, interactive=True) != secret:
            raise CredentialError('credential_unavailable')
        save_config(_config_path(environment), client_id=client_id,
                    environment=environment, namespace=namespace)
    except CredentialError as error:
        raise click.ClickException(error.code) from None
    click.echo(f'Plaid {environment} configured.')


@plaid.command(name='link')
@click.option('--environment', type=click.Choice(['sandbox', 'production']))
@click.option('--country', type=click.Choice(['US']), default='US')
@click.option('--days-requested', type=click.IntRange(1, 730), default=90)
@click.option('--resume', 'session')
def link_command(environment, country, days_requested, session):
    """Open Hosted Link or resume an existing local session."""
    _host()
    if session is not None:
        with db.connect() as conn:
            row = conn.execute('SELECT environment FROM plaid_link_sessions WHERE session_id=?', (session,)).fetchone()
        if row is None or (environment is not None and row['environment'] != environment):
            raise click.ClickException('invalid_session')
        environment = row['environment']
    if environment is None:
        raise click.UsageError('--environment is required for a new link')
    _run(environment, country=country, days_requested=days_requested, session=session)


@plaid.command()
@click.argument('connection', type=int)
def reconnect(connection):
    """Reconnect an existing Item without changing its credential or cursor."""
    _host()
    with db.connect() as conn:
        try:
            item = store.item(conn, connection)
        except KeyError:
            raise click.ClickException('invalid_session') from None
    _run(item['environment'], item=connection)


@plaid.command(name='map')
@click.argument('connection', type=click.IntRange(1))
@click.argument('provider_account')
@click.option('--local-account', type=click.IntRange(1), required=True)
@click.option('--start-date', required=True)
def map_command(connection, provider_account, local_account, start_date):
    """Map a discovered account with an inclusive date after reviewed statements."""
    from ... import inbox_adapter
    from .locks import item_locks

    _host()
    path = db.get_db_path()
    try:
        with item_locks(path, [connection]) as acquired:
            if not acquired:
                raise click.ClickException('item_busy')
            with inbox_adapter.intake_lock() as intake:
                if not intake:
                    raise click.ClickException('intake_busy')
                with db.connect(path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    store.map_account(conn, item_id=connection, provider_account_id=provider_account,
                                      local_account_id=local_account, currency='USD', start_date=start_date)
    except ValueError as error:
        raise click.ClickException(str(error)) from None
    click.echo('Mapping saved disabled; preview and activate after reviewing history.')


def _interactive():
    if not sys.stdin.isatty():
        raise click.ClickException('interactive_required')


def _connection(connection):
    with db.connect() as conn:
        try:
            return store.item(conn, connection)
        except KeyError:
            raise click.ClickException('unknown_item') from None


def _show_review(review):
    click.echo(f"Readiness: {review['readiness']}; returned coverage: {review['returned_coverage']}")
    for row in review['transactions']:
        click.echo(f"{row['transaction_id']} | {row['posted_date']} | {row['amount_cents']} cents | "
                   f"{row['merchant_norm']} | excluded={row['excluded_reason']} | "
                   f"local candidates={row['candidate_txn_ids']}")
    click.echo(f"Incoming eligible total: {review['incoming_total_cents']} cents")
    click.echo(f"Unresolved overlaps: {review['unresolved_overlap_count']}")


def _review_command(connection, *, provider_account=None, provider_transaction=None, local_txn=None):
    """Prompt under Item lock only; recheck the displayed review under immediate."""
    from ... import inbox_adapter
    from . import sync as syncing
    from .locks import item_locks

    _host()
    _interactive()
    path = db.get_db_path()
    try:
        with item_locks(path, [connection]) as acquired:
            if not acquired:
                raise syncing.SyncStateError('item_busy')
            snapshot = _connection(connection)
            syncing._eligible_item(snapshot)
            config, vault, secret = _configured(snapshot['environment'])
            token = vault.get(snapshot['credential_ref'], interactive=True)
            with PlaidClient(config['client_id'], secret, snapshot['environment']) as client:
                batch = syncing.fetch_review_batch(client, token, cursor=snapshot['cursor'], deadline=time.monotonic()+300)
            with db.connect(path) as conn:
                syncing.require_generation(store.item(conn, connection), snapshot['mapping_generation'])
                review = store.preview(conn, connection, batch)
            _show_review(review)
            if provider_account is None and provider_transaction is None:
                return
            value = None
            if provider_transaction is not None:
                final = syncing.final_events(batch).get(provider_transaction)
                if final is None or final[0] == 'removed':
                    raise click.ClickException('unknown_transaction')
                value = final[1]
                with db.connect(path) as conn:
                    candidate = conn.execute('SELECT account_id,posted_date,amount_cents,merchant_norm,status,payee,category FROM transactions WHERE txn_id=?', (local_txn,)).fetchone()
                if candidate is None:
                    raise click.ClickException('stale_review')
                candidate = dict(candidate)
                click.echo(f"Local candidate {local_txn} | account={candidate['account_id']} | {candidate['posted_date']} | "
                           f"{candidate['amount_cents']} cents | {candidate['merchant_norm']} | {candidate['status']} | "
                           f"payee={candidate['payee'] or ''} | category={candidate['category'] or ''}")
                click.echo(f'Adopt {provider_transaction} as local transaction {local_txn}.')
                click.confirm('Approve this exact reviewed transaction match?', abort=True)
            else:
                with db.connect(path) as conn:
                    account = store._account(conn, connection, provider_account)
                click.echo(f"Inclusive start date: {account['start_date']}")
                click.confirm('Confirm complete imported statements before this boundary, no gap at the boundary, '
                              'and approve the displayed history?', abort=True)
            with inbox_adapter.intake_lock() as intake:
                if not intake:
                    raise syncing.SyncStateError('intake_busy')
                with db.connect(path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    current = store.item(conn, connection)
                    syncing.require_generation(current, snapshot['mapping_generation'])
                    syncing.require_cursor(current, batch)
                    syncing.require_deadline(client._sync_deadline)
                    if store.preview(conn, connection, batch) != review:
                        raise click.ClickException('review_changed')
                    if value is not None:
                        fresh = conn.execute('SELECT account_id,posted_date,amount_cents,merchant_norm,status,payee,category FROM transactions WHERE txn_id=?', (local_txn,)).fetchone()
                        if fresh is None or dict(fresh) != candidate:
                            raise click.ClickException('stale_review')
                        store.adopt_transaction(conn, item_id=connection, provider_transaction_id=provider_transaction,
                                                local_txn_id=local_txn, fact=value)
                        syncing.require_deadline(client._sync_deadline)
                    else:
                        store.enable_account(conn, item_id=connection, provider_account_id=provider_account,
                                             reviewed_generation=snapshot['mapping_generation'], batch=batch)
                        syncing.apply_batch(conn, connection, batch, expected_generation=current['mapping_generation']+1,
                                            deadline=client._sync_deadline)
                        syncing.require_deadline(client._sync_deadline)
        click.echo('Adoption saved.' if value is not None else 'Account activated and history committed.')
    except (CredentialError, PlaidError, syncing.SyncStateError) as error:
        raise click.ClickException(error.code) from None
    except ValueError as error:
        safe = str(error) if str(error) in {'stale_cursor', 'stale_generation', 'unmapped_account', 'history_incomplete', 'unresolved_overlap', 'active_source', 'account_type_mismatch', 'stale_review', 'already_owned', 'unknown_account', 'item_disabled', 'unsupported_account'} else 'invalid_review'
        raise click.ClickException(safe) from None


@plaid.command()
@click.argument('connection', type=click.IntRange(1))
@click.option('--create-local', 'provider_account')
def accounts(connection, provider_account):
    """Discover masked accounts; optionally create an unmapped local account."""
    from ... import inbox_adapter
    from . import sync as syncing
    from .locks import item_locks

    _host()
    _interactive()
    path = db.get_db_path()
    try:
        with item_locks(path, [connection]) as acquired:
            if not acquired:
                raise syncing.SyncStateError('item_busy')
            snapshot = _connection(connection)
            syncing._eligible_item(snapshot)
            config, vault, secret = _configured(snapshot['environment'])
            token = vault.get(snapshot['credential_ref'], interactive=True)
            with PlaidClient(config['client_id'], secret, snapshot['environment']) as client:
                result = client.call('/accounts/get', {'access_token': token})
            data = result.get('accounts')
            if not isinstance(data, list) or not all(isinstance(a, dict) and syncing._identifier(a.get('account_id'))
                                                    and isinstance(a.get('balances'), dict)
                                                    and (a.get('type') is None or isinstance(a.get('type'), str))
                                                    and (a.get('subtype') is None or isinstance(a.get('subtype'), str))
                                                    for a in data):
                raise PlaidError('INVALID_RESPONSE')
            # Strip display text/balances before passing minimal metadata to storage.
            clean = [dict(account_id=a['account_id'], type=a.get('type'), subtype=a.get('subtype'),
                          mask=a.get('mask') if isinstance(a.get('mask'), str) and a['mask'].isdigit() and len(a['mask']) <= 4 else None,
                          balances={'iso_currency_code': 'USD' if a['balances'].get('iso_currency_code') == 'USD' else 'UNKNOWN'}) for a in data]
            with inbox_adapter.intake_lock() as intake:
                if not intake:
                    raise syncing.SyncStateError('intake_busy')
                with db.connect(path) as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    syncing.require_generation(store.item(conn, connection), snapshot['mapping_generation'])
                    store.discover_accounts(conn, item_id=connection, accounts=clean)
                    if provider_account is not None:
                        local_id = store.create_local_account(conn, item_id=connection, provider_account_id=provider_account)
                        click.echo(f'Local account created: {local_id}')
                    rows = conn.execute('SELECT provider_account_id,mask,account_type,currency,local_account_id,enabled FROM plaid_accounts WHERE item_id=? ORDER BY provider_account_id', (connection,)).fetchall()
                    locals_ = conn.execute('SELECT account_id,acct_type FROM accounts ORDER BY account_id').fetchall()
            for row in rows:
                click.echo(f"{row['provider_account_id']} | ****{row['mask'] or '?'} | {row['account_type'] or 'unsupported'} | {row['currency']} | local={row['local_account_id']} | enabled={bool(row['enabled'])}")
            click.echo('Local mapping choices: ' + ', '.join(f"{r['account_id']} ({r['acct_type']})" for r in locals_))
            click.echo('Use map with an explicit local account and inclusive start date; new mappings stay disabled.')
    except (CredentialError, PlaidError, syncing.SyncStateError) as error:
        raise click.ClickException(error.code) from None
    except ValueError:
        raise click.ClickException('unsupported_account') from None


@plaid.command()
@click.argument('connection', type=click.IntRange(1))
def preview(connection):
    """Review complete history without changing facts, ownership or cursor."""
    _review_command(connection)


@plaid.command()
@click.argument('connection', type=click.IntRange(1))
@click.argument('provider_transaction')
@click.option('--local-txn', type=click.IntRange(1), required=True)
def adopt(connection, provider_transaction, local_txn):
    """Approve one current exact overlap; never blindly merge transactions."""
    _review_command(connection, provider_transaction=provider_transaction, local_txn=local_txn)


@plaid.command()
@click.argument('connection', type=click.IntRange(1))
@click.argument('provider_account')
def activate(connection, provider_account):
    """Fetch fresh history, confirm statement boundary and commit atomically."""
    _review_command(connection, provider_account=provider_account)


@plaid.command(name='sync')
@click.argument('connection', type=click.IntRange(1), required=False)
@click.option('--all', 'all_items', is_flag=True)
@click.option('--non-interactive', is_flag=True)
@click.option('--json', 'as_json', is_flag=True)
def sync_command(connection, all_items, non_interactive, as_json):
    """Sync one or every eligible connection, with safe scheduled output."""
    import json
    from dataclasses import asdict
    from . import sync as syncing

    _host()
    if (connection is None) == (not all_items):
        raise click.UsageError('Choose a connection or --all')
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT DISTINCT i.item_id FROM plaid_items i JOIN plaid_accounts a USING(item_id) WHERE a.enabled=1 AND a.history_approved=1 AND i.state IN ('disabled','active') ORDER BY i.item_id")] if all_items else [connection]
    results = []
    for item_id in ids:
        try:
            snapshot = _connection(item_id)
            try:
                config, vault, secret = _configured(snapshot['environment'], interactive=not non_interactive)
            except CredentialError as error:
                _record_config_failure(item_id, error)
                raise
            with PlaidClient(config['client_id'], secret, snapshot['environment']) as client:
                result = syncing.sync_item(db.get_db_path(), item_id, client, vault, now=_now())
            results.append(dict(connection=item_id, **asdict(result)))
        except (CredentialError, PlaidError, syncing.SyncStateError) as error:
            results.append(dict(connection=item_id, outcome='failed', safe_error_code=error.code))
        except click.ClickException:
            results.append(dict(connection=item_id, outcome='failed', safe_error_code='unknown_item'))
    if as_json:
        click.echo(json.dumps(results))
    else:
        for row in results:
            click.echo(f"Connection {row['connection']}: {row['outcome']}")
    if any(row['outcome'] == 'failed' for row in results):
        raise click.exceptions.Exit(1)


def _record_config_failure(item_id, error):
    """Client credential reads precede sync_item; record their safe attempt too."""
    from ... import inbox_adapter
    from . import sync as syncing
    from .locks import item_locks

    try:
        with item_locks(db.get_db_path(), [item_id]) as acquired:
            if acquired:
                with inbox_adapter.intake_lock() as intake:
                    if intake:
                        with db.connect() as conn:
                            conn.execute('BEGIN IMMEDIATE')
                            syncing.record_failure(conn, item_id, error, now=_now())
    except Exception:
        pass  # Keep the original allowlisted credential error safe.
