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


def credentials() -> tuple[str, str, str | None]:
    """(username, password, otp_secret). Raises if the first two are unset."""
    user = (os.environ.get("AMAZON_USERNAME") or "").strip()
    pw = os.environ.get("AMAZON_PASSWORD") or ""
    otp = (os.environ.get("AMAZON_OTP_SECRET_KEY") or "").strip() or None
    if not user or not pw:
        raise AmazonAuthError(
            "AMAZON_USERNAME / AMAZON_PASSWORD are not set. Add them to .env "
            "(gitignored), and AMAZON_OTP_SECRET_KEY too if you use 2FA — "
            "without it every sync needs someone at the terminal.")
    return user, pw, otp


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

    user, pw, otp = credentials()
    config = AmazonOrdersConfig(data={
        "cookie_jar_path": str(cookie_path()),
        "output_dir": str(amazon_dir() / "output"),
    }, config_path=str(config_path()))

    session = AmazonSession(user, pw, otp_secret_key=otp, config=config)
    if force_login or not session.auth_cookies_stored():
        session.login()
    harden()
    return session
