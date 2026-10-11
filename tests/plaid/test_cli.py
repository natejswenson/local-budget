from click.testing import CliRunner
from local_budget.cli import main


def test_host_commands_registered_without_secret_options():
    result = CliRunner().invoke(main, ['plaid', '--help'])
    assert result.exit_code == 0
    assert all(command in result.output for command in ['setup', 'link', 'reconnect'])
    for command in ['setup', 'link', 'reconnect']:
        result = CliRunner().invoke(main, ['plaid', command, '--help'])
        assert result.exit_code == 0
        assert '--secret' not in result.output and '--access-token' not in result.output


def test_linux_commands_fail_before_native_import(monkeypatch):
    from local_budget.connectors.plaid import cli
    monkeypatch.setattr(cli.sys, 'platform', 'linux')
    result = CliRunner().invoke(main, ['plaid', 'setup', '--environment', 'sandbox'])
    assert result.exit_code != 0
    assert 'unsupported_platform' in result.output


def test_setup_saves_verified_secret_separately(plaid_db, monkeypatch):
    from local_budget.connectors.plaid import cli, credentials, store
    from local_budget import db, paths
    from .test_link import Vault
    vault = Vault()
    monkeypatch.setattr(cli.sys, 'platform', 'darwin')
    monkeypatch.setattr(cli, 'KeychainCredentials', lambda namespace, environment: vault)
    result = CliRunner().invoke(main, ['plaid', 'setup', '--environment', 'sandbox'],
                                input='FABRICATED_CLIENT\nFABRICATED_SECRET\n')
    assert result.exit_code == 0, result.output
    config = credentials.load_config(paths.data_dir() / 'plaid' / 'sandbox.json')
    with db.connect(plaid_db) as conn:
        namespace = store.namespace(conn)
    assert config == {'client_id': 'FABRICATED_CLIENT', 'environment': 'sandbox', 'namespace': namespace}
    assert vault.get(credentials.client_credential_handle(namespace, 'sandbox')) == 'FABRICATED_SECRET'
    assert 'FABRICATED_SECRET' not in result.output


def test_link_poll_uses_one_client_and_resume_never_creates(plaid_db, monkeypatch):
    from datetime import timedelta
    import json
    import httpx
    from local_budget.connectors.plaid import cli, link
    from local_budget import db
    from .test_link import Vault, NOW
    vault = Vault()
    requests = []
    clock = [NOW]
    clients = []
    polls = [0]
    def transport(request):
        requests.append(request.url.path)
        if request.url.path == '/link/token/create':
            return httpx.Response(200, json={'link_token': 'FABRICATED_LINK',
                'hosted_link_url': 'https://secure.plaid.com/x', 'expiration': (NOW + timedelta(minutes=15)).isoformat()})
        if request.url.path == '/link/token/get':
            polls[0] += 1
            return httpx.Response(200, json={'link_sessions': [] if polls[0] == 1 else [
                {'finished_at': NOW.isoformat(), 'exit': None,
                 'results': {'item_add_results': [{'public_token': 'FABRICATED_PUBLIC'}]}}]})
        assert json.loads(request.content)['public_token'] == 'FABRICATED_PUBLIC'
        return httpx.Response(200, json={'access_token': 'FABRICATED_ACCESS', 'item_id': 'FABRICATED_ITEM'})
    real_client = cli.PlaidClient
    def make_client(*args, **kwargs):
        result = real_client(*args, transport=httpx.MockTransport(transport), **kwargs)
        clients.append(result)
        return result
    monkeypatch.setattr(cli.sys, 'platform', 'darwin')
    monkeypatch.setattr(cli, '_configured', lambda env: ({'client_id': 'FABRICATED_ID'}, vault, 'FABRICATED_SECRET'))
    monkeypatch.setattr(cli, 'PlaidClient', make_client)
    monkeypatch.setattr(cli, '_now', lambda: clock[0])
    monkeypatch.setattr(cli.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)))
    monkeypatch.setattr(link, 'open_session_url', lambda url: None)
    result = CliRunner().invoke(main, ['plaid', 'link', '--environment', 'sandbox'], input='y\n')
    assert result.exit_code == 0, result.output
    assert len(clients) == 1 and requests.count('/link/token/get') == 2
    assert clock[0] == NOW + timedelta(seconds=5)
    with db.connect(plaid_db) as conn:
        session = conn.execute('SELECT session_id FROM plaid_link_sessions').fetchone()[0]
    result = CliRunner().invoke(main, ['plaid', 'link', '--resume', session])
    assert result.exit_code == 0, result.output
    assert requests.count('/link/token/create') == 1
    assert 'FABRICATED_' not in result.output


def test_dashboard_does_not_expose_plaid_management():
    from local_budget.web.server import create_app
    assert all('plaid' not in path.lower() for path in create_app().openapi()['paths'])
