"""Host-only Keychain credentials and owner-only, nonsecret Plaid configuration.

Security is imported lazily. No subprocess, cloud synchronization, or plaintext
credential fallback is used. Native exceptions never leave this boundary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from uuid import NAMESPACE_URL, uuid5

from local_budget import paths

_CODES = frozenset({'credential_missing', 'credential_locked', 'credential_unavailable',
                    'unsupported_platform', 'invalid_environment', 'invalid_handle',
                    'invalid_namespace', 'invalid_config'})
_IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


class CredentialError(Exception):
    """An allowlisted error code only, safe for host status and logs."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else 'credential_unavailable'
        super().__init__(self.code)


def _service(namespace: str, environment: str) -> str:
    if environment not in {'sandbox', 'production'}:
        raise CredentialError('invalid_environment')
    if not isinstance(namespace, str) or not _IDENTIFIER.fullmatch(namespace):
        raise CredentialError('invalid_namespace')
    return f'local-budget.plaid.{namespace}.{environment}'


def client_credential_handle(namespace: str, environment: str) -> str:
    """Stable opaque handle for the API secret, separate from Item handles."""
    return uuid5(NAMESPACE_URL, _service(namespace, environment) + '/client').hex


def _security():
    if sys.platform != 'darwin':
        raise CredentialError('unsupported_platform')
    try:
        import Security
        return Security
    except Exception:
        pass
    raise CredentialError('credential_unavailable')


def _query(native, service: str, account: str, *, interactive: bool = False) -> dict:
    query = {native.kSecClass: native.kSecClassGenericPassword,
             native.kSecAttrService: service, native.kSecAttrAccount: account,
             native.kSecAttrSynchronizable: False}
    if not interactive:
        query[native.kSecUseAuthenticationUI] = native.kSecUseAuthenticationUIFail
    return query


def _status_code(native, status: int) -> str:
    if status == native.errSecItemNotFound:
        return 'credential_missing'
    if status in (native.errSecInteractionNotAllowed, native.errSecAuthFailed,
                  native.errSecInteractionRequired, native.errSecUserCanceled):
        return 'credential_locked'
    return 'credential_unavailable'


def _native_read(service: str, account: str, *, interactive: bool) -> str:
    native = _security()
    code = 'credential_unavailable'
    try:
        query = _query(native, service, account, interactive=interactive)
        query.update({native.kSecReturnData: True, native.kSecMatchLimit: native.kSecMatchLimitOne})
        status, data = native.SecItemCopyMatching(query, None)
        if status == native.errSecSuccess:
            return bytes(data).decode('utf-8')
        code = _status_code(native, status)
    except Exception:
        pass
    # Raise after leaving the handler: no secret-bearing __context__ either.
    raise CredentialError(code)


def _native_put(service: str, account: str, secret: str) -> None:
    native = _security()
    code = 'credential_unavailable'
    try:
        query = _query(native, service, account)
        value = {native.kSecValueData: secret.encode('utf-8')}
        status, _ = native.SecItemAdd(query | value, None)
        if status == native.errSecDuplicateItem:
            status = native.SecItemUpdate(query, value)
        if status == native.errSecSuccess:
            return
        code = _status_code(native, status)
    except Exception:
        pass
    raise CredentialError(code)


def _native_delete(service: str, account: str) -> None:
    native = _security()
    code = 'credential_unavailable'
    try:
        status = native.SecItemDelete(_query(native, service, account))
        if status in (native.errSecSuccess, native.errSecItemNotFound):
            return
        code = _status_code(native, status)
    except Exception:
        pass
    raise CredentialError(code)


class KeychainCredentials:
    def __init__(self, namespace: str, environment: str):
        self._service = _service(namespace, environment)

    @staticmethod
    def _account(handle: str) -> str:
        if not isinstance(handle, str) or not _IDENTIFIER.fullmatch(handle):
            raise CredentialError('invalid_handle')
        return handle

    def put(self, handle: str, secret: str) -> None:
        _native_put(self._service, self._account(handle), secret)

    def get(self, handle: str, *, interactive: bool = False) -> str:
        return _native_read(self._service, self._account(handle), interactive=interactive)

    def delete(self, handle: str) -> None:
        _native_delete(self._service, self._account(handle))


def _config_path(path: Path) -> Path:
    """Restrict config to the dedicated data directory; reject symlinks."""
    root = paths.data_dir() / 'plaid'
    path = Path(path)
    if path.parent != root or path.is_symlink() or root.is_symlink():
        raise CredentialError('invalid_config')
    return path


def _config_values(values: dict) -> dict:
    if (not isinstance(values, dict)
            or set(values) != {'client_id', 'environment', 'namespace'}
            or not all(isinstance(value, str) and value for value in values.values())):
        raise CredentialError('invalid_config')
    try:
        _service(values['namespace'], values['environment'])
    except CredentialError:
        pass
    else:
        return values
    raise CredentialError('invalid_config')


def save_config(path: Path, *, client_id: str, environment: str, namespace: str) -> None:
    """Atomically replace nonsecret config with exact 0700/0600 permissions."""
    values = _config_values(dict(client_id=client_id, environment=environment, namespace=namespace))
    temporary = None
    try:
        path = _config_path(path)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(values, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return
    except Exception:
        pass
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
    raise CredentialError('invalid_config')


def load_config(path: Path) -> dict:
    """Load only the three allowlisted values from an owner-only regular file."""
    try:
        path = _config_path(path)
        parent = path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise CredentialError('invalid_config')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise CredentialError('invalid_config')
            return _config_values(json.load(stream))
    except Exception:
        pass
    raise CredentialError('invalid_config')
