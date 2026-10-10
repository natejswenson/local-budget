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


def _configured(environment):
    namespace = _identity()
    config = load_config(_config_path(environment))
    if config['environment'] != environment or config['namespace'] != namespace:
        raise CredentialError('invalid_config')
    vault = KeychainCredentials(namespace, environment)
    secret = vault.get(client_credential_handle(namespace, environment), interactive=True)
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
