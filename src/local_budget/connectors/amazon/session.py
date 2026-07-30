"""Amazon session + credential handling.

Amazon publishes no consumer order API and no OAuth, so the only way to reach
your own order history programmatically is an authenticated browser-style
session against the consumer site. Consequences worth being explicit about,
because they are permanent properties of this connector and not bugs:

* Amazon's Conditions of Use prohibit automated extraction. This is your own
  account and your own data, but it is a real term.
* The upstream parser can break whenever Amazon redesigns a page. `sync` must
  fail loudly when that happens — see `store.SyncAborted`.
* **The cookie jar is a credential.** A live Amazon session is worth as much as
  the password, so it is kept beside `budget.db` under the same at-rest posture
  (0700 dir / 0600 file) rather than in the library's default
  `~/.config/amazonorders/`, which is neither hardened nor gitignored.

Credentials come from the environment (`.env` is auto-loaded by `cli.py` and is
gitignored):

    AMAZON_USERNAME         account email
    AMAZON_PASSWORD         account password
    AMAZON_OTP_SECRET_KEY   TOTP secret — OPTIONAL, but it is what makes sync
                            unattended. Without it a 2FA challenge needs a
                            human at the terminal.

Nothing here is imported at module scope by the rest of the app: a broken or
missing `amazon-orders` install must degrade to "the Amazon commands don't
work", never "the budget CLI won't start".
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ... import paths


class AmazonAuthError(RuntimeError):
    """Credentials missing, rejected, or a 2FA challenge we cannot answer."""


def amazon_dir() -> Path:
    """`data/amazon/`, created 0700. Sibling of budget.db on purpose."""
    d = paths.data_dir() / "amazon"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(paths.DIR_MODE)
    return d


def cookie_path() -> Path:
    return amazon_dir() / "cookies.json"


def config_path() -> Path:
    return amazon_dir() / "config.yml"


def harden() -> None:
    """0600 whatever the library wrote. Called after every session operation —
    the library creates these files itself, so we cannot set the mode up front."""
    for p in (cookie_path(), config_path()):
        if p.exists():
            p.chmod(paths.FILE_MODE)


def credentials(*, required: bool = True) -> tuple[str | None, str | None, str | None]:
    """(username, password, otp_secret) from the environment.

    `required=False` for the captured-session path, where there is no password
    at all — a passkey account has no replayable secret, so the cookie jar IS
    the credential.
    """
    user = (os.environ.get("AMAZON_USERNAME") or "").strip() or None
    pw = os.environ.get("AMAZON_PASSWORD") or None
    otp = (os.environ.get("AMAZON_OTP_SECRET_KEY") or "").strip() or None
    if required and not (user and pw):
        raise AmazonAuthError(
            "No saved Amazon session, and AMAZON_USERNAME / AMAZON_PASSWORD "
            "are not set.\nRun `budget amazon login` to sign in through a "
            "browser window (works with a passkey — nothing is stored but the "
            "session cookie).")
    return user, pw, otp


def stored_session_looks_valid() -> bool:
    """Does the jar hold the one cookie the library treats as authentication?

    Cheap and offline. Whether Amazon still HONOURS the session is only
    knowable by making a request, which `fetch` reports on.
    """
    p = cookie_path()
    if not p.exists():
        return False
    try:
        return "x-main" in json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def build_session(*, force_login: bool = False):
    """An authenticated `AmazonSession` with storage pointed at `data/amazon/`.

    Imported lazily so the CLI still starts if `amazon-orders` is absent.

    Auth is gated on `auth_cookies_stored()`, NOT on `session.is_authenticated`:
    the latter is initialised to False on every fresh object, so branching on it
    would run the whole sign-in flow on every sync even with a perfectly good
    cookie jar — more round-trips, and more chances to trip a bot challenge.
    A stale jar surfaces later as an auth error, which `fetch` turns into
    "run `budget amazon login`".
    """
    try:
        from amazonorders.conf import AmazonOrdersConfig
        from amazonorders.session import AmazonSession
    except ImportError as e:                                  # pragma: no cover
        raise AmazonAuthError(
            "the `amazon-orders` package is not installed — run `uv sync`") from e

    # A captured browser session means no password is needed — and for a
    # passkey account, none exists to need.
    have_session = stored_session_looks_valid() and not force_login
    user, pw, otp = credentials(required=not have_session)
    config = AmazonOrdersConfig(data={
        "cookie_jar_path": str(cookie_path()),
        "output_dir": str(amazon_dir() / "output"),
    }, config_path=str(config_path()))

    session = AmazonSession(user, pw, otp_secret_key=otp, config=config)
    if force_login or not session.auth_cookies_stored():
        if not (user and pw):
            # The captured session is gone or unreadable and there is no
            # password to fall back on. Say that, rather than letting the
            # library fail somewhere inside a sign-in form with None.
            raise AmazonAuthError(
                "the saved Amazon session is missing or no longer valid, and "
                "there is no password configured to sign in with.\n"
                "Run `budget amazon login` to capture a fresh session.")
        session.login()
    else:
        # A restored jar loads the cookies but leaves `is_authenticated` False,
        # and every fetch is gated on that flag — so the connector would say
        # "Call AmazonSession.login() to authenticate first" while holding a
        # perfectly good session.
        #
        # This is not a bypass: login() sets the same flag on exactly this
        # condition (`if self.auth_cookies_stored(): self.is_authenticated =
        # True`). We are applying the library's own test to a session it
        # restored but never evaluated. If the cookies ARE stale, the first
        # request comes back as a sign-in page and fetch._wrap reports it.
        session.is_authenticated = True
    harden()
    return session
