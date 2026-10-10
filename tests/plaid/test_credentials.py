"""Fabricated Security responses: these tests never access the host Keychain."""
import json
import os
from types import SimpleNamespace

import pytest

from local_budget.connectors.plaid import credentials


@pytest.fixture
def security(monkeypatch):
    names = ('kSecClass', 'kSecClassGenericPassword', 'kSecAttrService',
             'kSecAttrAccount', 'kSecAttrSynchronizable', 'kSecValueData',
             'kSecReturnData', 'kSecMatchLimit', 'kSecMatchLimitOne',
             'kSecUseAuthenticationUI', 'kSecUseAuthenticationUIFail')
    native = SimpleNamespace(**{name: name for name in names},
                             errSecSuccess=0, errSecItemNotFound=-25300,
                             errSecDuplicateItem=-25299,
                             errSecInteractionNotAllowed=-25308,
                             errSecAuthFailed=-25293, errSecInteractionRequired=-25315,
                             errSecUserCanceled=-128)
    monkeypatch.setattr(credentials, '_security', lambda: native)
    return native


def test_background_read_disables_keychain_ui(monkeypatch):
    calls = []
    def read(service, account, *, interactive):
        calls.append(interactive)
        raise credentials.CredentialError('credential_locked')
    monkeypatch.setattr(credentials, '_native_read', read)
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.KeychainCredentials('test-ledger', 'sandbox').get('item-1')
    assert calls == [False]
    assert str(exc.value) == 'credential_locked'


def test_secret_return_and_native_query(security):
    queries = []
    def read(query, result):
        queries.append(query)
        assert result is None
        return 0, b'SECRET_SENTINEL'
    security.SecItemCopyMatching = read
    vault = credentials.KeychainCredentials('ledger-a', 'sandbox')
    assert vault.get('opaque-handle') == 'SECRET_SENTINEL'
    assert queries[0]['kSecUseAuthenticationUI'] == 'kSecUseAuthenticationUIFail'
    assert queries[0]['kSecAttrSynchronizable'] is False
    vault.get('opaque-handle', interactive=True)
    assert 'kSecUseAuthenticationUI' not in queries[1]


def test_namespace_environment_isolation(monkeypatch):
    labels = []
    monkeypatch.setattr(credentials, '_native_read',
                        lambda service, account, **kw: labels.append((service, account)) or 'secret')
    for namespace, environment in [('a', 'sandbox'), ('b', 'sandbox'), ('a', 'production')]:
        credentials.KeychainCredentials(namespace, environment).get('same-handle')
    assert len(set(labels)) == 3
    assert all(account == 'same-handle' for _, account in labels)
    assert credentials.client_credential_handle('a', 'sandbox') == credentials.client_credential_handle('a', 'sandbox')
    assert credentials.client_credential_handle('a', 'sandbox') != credentials.client_credential_handle('a', 'production')


@pytest.mark.parametrize('status,code', [(-25300, 'credential_missing'),
    (-25308, 'credential_locked'), (-25293, 'credential_locked'), (-1, 'credential_unavailable')])
def test_native_status_is_safe(security, status, code):
    security.SecItemCopyMatching = lambda *args: (status, None)
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.KeychainCredentials('a', 'sandbox').get('h')
    assert str(exc.value) == code
    assert repr(exc.value) == f'CredentialError({code!r})'


def test_native_exception_does_not_leak(security):
    def fail(*args):
        raise RuntimeError('SECRET_SENTINEL')
    security.SecItemCopyMatching = fail
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.KeychainCredentials('a', 'sandbox').get('h')
    assert 'SECRET_SENTINEL' not in str(exc.value) + repr(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_put_overwrites_existing_and_delete_missing_is_idempotent(security):
    updates = []
    adds = []
    security.SecItemAdd = lambda query, result: adds.append(query) or (-25299, None)
    security.SecItemUpdate = lambda query, value: updates.append((query, value)) or 0
    security.SecItemDelete = lambda query: -25300
    vault = credentials.KeychainCredentials('a', 'sandbox')
    vault.put('h', 'SECRET_SENTINEL')
    assert adds[0]['kSecValueData'] == b'SECRET_SENTINEL'
    assert adds[0]['kSecAttrSynchronizable'] is False
    assert updates[0][1] == {'kSecValueData': b'SECRET_SENTINEL'}
    assert 'kSecValueData' not in updates[0][0]
    vault.delete('h')


def test_unsupported_platform_has_no_fallback(monkeypatch):
    monkeypatch.setattr(credentials.sys, 'platform', 'linux')
    with pytest.raises(credentials.CredentialError, match='unsupported_platform'):
        credentials.KeychainCredentials('a', 'sandbox').get('h')


@pytest.mark.parametrize('namespace,environment', [('', 'sandbox'), ('a', 'bad'), ('a.b', 'sandbox')])
def test_invalid_scope(namespace, environment):
    with pytest.raises(credentials.CredentialError):
        credentials.KeychainCredentials(namespace, environment)


def test_owner_only_atomic_config(data_dir, monkeypatch):
    path = data_dir / 'plaid' / 'config.json'
    replacements = []
    replace = os.replace
    def check_replace(source, target):
        assert os.stat(source).st_mode & 0o777 == 0o600
        replacements.append((source, target))
        replace(source, target)
    monkeypatch.setattr(credentials.os, 'replace', check_replace)
    credentials.save_config(path, client_id='client-id', environment='sandbox', namespace='ledger-a')
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert credentials.load_config(path) == {'client_id': 'client-id', 'environment': 'sandbox', 'namespace': 'ledger-a'}
    assert len(replacements) == 1
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('extra', [{'secret': 'SECRET_SENTINEL'}, {'access_token': 'SECRET_SENTINEL'}, {'unknown': 'x'}])
def test_config_rejects_extra_keys(data_dir, extra):
    path = data_dir / 'plaid' / 'config.json'
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text(json.dumps(dict(client_id='c', environment='sandbox', namespace='a', **extra)))
    path.chmod(0o600)
    with pytest.raises(credentials.CredentialError, match='invalid_config'):
        credentials.load_config(path)


def test_config_rejects_insecure_mode_and_outside_path(data_dir, tmp_path):
    path = data_dir / 'plaid' / 'config.json'
    credentials.save_config(path, client_id='c', environment='sandbox', namespace='a')
    path.chmod(0o644)
    with pytest.raises(credentials.CredentialError, match='invalid_config'):
        credentials.load_config(path)
    with pytest.raises(credentials.CredentialError, match='invalid_config'):
        credentials.save_config(tmp_path / 'outside.json', client_id='c', environment='sandbox', namespace='a')


def test_authentication_required_and_canceled_are_locked(security):
    security.errSecInteractionRequired = -25315
    security.errSecUserCanceled = -128
    for status in (-25315, -128):
        security.SecItemCopyMatching = lambda *args: (status, None)
        with pytest.raises(credentials.CredentialError, match='credential_locked'):
            credentials.KeychainCredentials('a', 'sandbox').get('h')


def test_unknown_error_code_cannot_expose_secret():
    error = credentials.CredentialError('SECRET_SENTINEL')
    assert str(error) == 'credential_unavailable'
    assert 'SECRET_SENTINEL' not in repr(error)


@pytest.mark.parametrize('operation', ['put', 'delete'])
def test_mutation_native_failures_remain_safe(security, operation):
    def fail(*args):
        raise RuntimeError('SECRET_SENTINEL')
    security.SecItemAdd = fail
    security.SecItemDelete = fail
    vault = credentials.KeychainCredentials('a', 'sandbox')
    with pytest.raises(credentials.CredentialError) as exc:
        if operation == 'put':
            vault.put('h', 'SECRET_SENTINEL')
        else:
            vault.delete('h')
    assert str(exc.value) == 'credential_unavailable'
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_config_failed_replace_preserves_prior_file(data_dir, monkeypatch):
    path = data_dir / 'plaid' / 'config.json'
    credentials.save_config(path, client_id='original', environment='sandbox', namespace='a')
    def fail(*args):
        raise OSError('SECRET_SENTINEL')
    monkeypatch.setattr(credentials.os, 'replace', fail)
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.save_config(path, client_id='new', environment='sandbox', namespace='a')
    assert str(exc.value) == 'invalid_config'
    assert exc.value.__context__ is None
    assert credentials.load_config(path)['client_id'] == 'original'
    assert list(path.parent.iterdir()) == [path]


def test_config_symlink_rejected(data_dir):
    path = data_dir / 'plaid' / 'config.json'
    credentials.save_config(path, client_id='c', environment='sandbox', namespace='a')
    alias = path.parent / 'alias.json'
    alias.symlink_to(path)
    with pytest.raises(credentials.CredentialError, match='invalid_config'):
        credentials.load_config(alias)
    with pytest.raises(credentials.CredentialError, match='invalid_config'):
        credentials.save_config(alias, client_id='new', environment='sandbox', namespace='a')
    assert credentials.load_config(path)['client_id'] == 'c'
