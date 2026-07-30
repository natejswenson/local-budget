"""Interactive sign-in: capture a session instead of storing a password.

A passkey is a WebAuthn credential bound to a device's secure element. There is
no secret a script can replay, so the username+password flow simply cannot work
for an account that signs in that way — and phone-approval 2FA rules out a
stored TOTP secret too.

What makes this tractable is how little the library needs to consider itself
authenticated. Its cookie jar is a flat ``json.dumps({name: value})`` map, and
``auth_cookies_stored()`` checks for exactly one cookie, ``x-main``. So a human
can sign in however they like — phone number, passkey, phone approval, CAPTCHA
— and we keep only the resulting session.

This is a security improvement, not a workaround. The original design put an
Amazon password and a TOTP seed on disk; this stores one session cookie that
Amazon can revoke, and nothing else.

The trade is re-authentication: when Amazon expires the session, sync fails and
you run this again. With "keep me signed in" that is months, not days.

Requires Playwright:  uv sync  &&  uv run playwright install chromium
"""
from __future__ import annotations

import json
import time

from ... import paths
from .session import AmazonAuthError, cookie_path

SIGN_IN_URL = "https://www.amazon.com/ap/signin"
ORDER_HISTORY_URL = "https://www.amazon.com/your-orders/orders"
#: The single cookie the library treats as proof of authentication.
AUTH_COOKIE = "x-main"

POLL_SECONDS = 2
DEFAULT_TIMEOUT = 300


def _write_jar(cookies: list[dict]) -> int:
    """Playwright cookie dicts → the library's flat {name: value} jar.

    Only amazon.com cookies are kept. The jar is a name→value map with no
    domain scoping, so letting a third-party cookie in would silently shadow a
    real one of the same name.
    """
    flat = {c["name"]: c["value"] for c in cookies
            if "amazon.com" in (c.get("domain") or "")}
    if AUTH_COOKIE not in flat:
        raise AmazonAuthError(
            f"signed-in session has no {AUTH_COOKIE!r} cookie — not authenticated. "
            "Make sure you reached Your Orders before the window closed.")
    path = cookie_path()
    path.write_text(json.dumps(flat), encoding="utf-8")
    path.chmod(paths.FILE_MODE)
    return len(flat)


def login(*, timeout: int = DEFAULT_TIMEOUT, echo=print) -> dict:
    """Open a real browser, wait for the human, capture the session.

    Returns ``{"cookies": n, "path": str}``. Raises `AmazonAuthError` on
    timeout or if the captured session is not actually authenticated.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:                                  # pragma: no cover
        raise AmazonAuthError(
            "Playwright is not installed. Run `uv sync` then "
            "`uv run playwright install chromium`.") from e

    echo("  Opening a browser window — sign in with your phone number and passkey.")
    echo("  Nothing is captured until you reach Your Orders.")

    with sync_playwright() as p:
        # Headed, and a persistent context is deliberately NOT used: the point
        # is to capture a session into our own hardened jar, not to leave a
        # second logged-in Amazon profile lying around on disk.
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(SIGN_IN_URL)

        deadline = time.monotonic() + timeout
        seen = False
        try:
            while time.monotonic() < deadline:
                names = {c["name"] for c in context.cookies()
                         if "amazon.com" in (c.get("domain") or "")}
                if AUTH_COOKIE in names:
                    if not seen:
                        echo("  ✓ signed in — confirming against Your Orders…")
                        seen = True
                    # x-main alone is what the library trusts, but it can be
                    # set mid-flow. Loading the orders page is the real proof:
                    # an unauthenticated request bounces back to /ap/signin.
                    page.goto(ORDER_HISTORY_URL, wait_until="domcontentloaded")
                    if "/ap/signin" not in page.url:
                        n = _write_jar(context.cookies())
                        echo(f"  ✓ captured {n} cookies")
                        return {"cookies": n, "path": str(cookie_path())}
                    seen = False        # bounced — keep waiting
                time.sleep(POLL_SECONDS)
        finally:
            context.close()
            browser.close()

    raise AmazonAuthError(
        f"timed out after {timeout}s without a signed-in Amazon session. "
        "Re-run `budget amazon login` and complete sign-in in the window.")
