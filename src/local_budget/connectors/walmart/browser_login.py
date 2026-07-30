"""Interactive sign-in: capture a session instead of storing a password.

Walmart sign-in routinely ends in a one-time code sent to a phone or an inbox,
and often a bot challenge before that. None of it is answerable by a stored
secret, so the only flow that actually works is the honest one: a human signs in
however Walmart asks them to, in a real window, and we keep the result.

The trade is re-authentication. When Walmart expires the session, sync fails and
you run this again.

Requires Playwright:  uv sync  &&  uv run playwright install chromium
"""
from __future__ import annotations

import time

from . import browser
from .session import (AUTH_COOKIE_CANDIDATES, WALMART_DOMAIN, WalmartAuthError,
                      harden, save_storage_state, storage_state_path)

SIGN_IN_URL = "https://www.walmart.com/account/login"
ORDERS_URL = "https://www.walmart.com/orders"

#: Where an unauthenticated request to /orders lands. Reaching /orders WITHOUT
#: passing through one of these is the proof that the session is real — cookie
#: presence alone is not, because Walmart sets plenty of them before sign-in.
LOGIN_PATHS = ("/account/login", "/signin", "/account/signin")

POLL_SECONDS = 3
DEFAULT_TIMEOUT = 300


def signed_in(url: str) -> bool:
    """Does this landing URL mean we got in?"""
    return "/orders" in url and not any(p in url for p in LOGIN_PATHS)


def looks_done(url: str, cookie_names: set[str]) -> bool:
    """Cheap "the human is probably finished" test, run on every poll tick.

    It has to be cheap AND non-disruptive, because the expensive confirmation —
    navigating to the orders page — would yank someone out of a half-typed
    one-time code if it ran on a timer. So: off the login page, and at least one
    account cookie set. Both are true only after Walmart has redirected away
    from sign-in, and neither disturbs the page.
    """
    if any(p in url for p in LOGIN_PATHS):
        return False
    return bool(cookie_names & set(AUTH_COOKIE_CANDIDATES))


def login(*, timeout: int = DEFAULT_TIMEOUT, echo=print) -> dict:
    """Open a real browser, wait for the human, capture the session.

    Returns ``{"cookies": n, "path": str}``. Raises `WalmartAuthError` on
    timeout or if the captured session is not actually authenticated.
    """
    echo("  Opening a browser window — sign in to Walmart however it asks "
         "(code, passkey, challenge).")
    echo("  Nothing is captured until your order history actually loads.")

    with browser.context(headless=False) as (ctx, page):
        page.goto(SIGN_IN_URL)
        deadline = time.monotonic() + timeout
        announced = False
        while time.monotonic() < deadline:
            names = {c["name"] for c in ctx.cookies()
                     if WALMART_DOMAIN in (c.get("domain") or "")}
            if looks_done(page.url, names):
                if not announced:
                    echo("  ✓ signed in — confirming against your orders…")
                    announced = True
                # The real proof. Account cookies alone are not it: Walmart sets
                # some of them before the one-time code is entered, so a
                # cookie-only test would capture a session that cannot read an
                # order and call it success.
                try:
                    page.goto(ORDERS_URL, wait_until="domcontentloaded")
                except Exception:                          # pragma: no cover
                    time.sleep(POLL_SECONDS)   # mid-navigation; try again
                    continue
                if signed_in(page.url):
                    n = save_storage_state(ctx.storage_state())
                    harden()
                    echo(f"  ✓ captured {n} cookies")
                    return {"cookies": n, "path": str(storage_state_path())}
                announced = False                          # bounced — keep waiting
            time.sleep(POLL_SECONDS)

    raise WalmartAuthError(
        f"timed out after {timeout}s without a signed-in Walmart session. "
        "Re-run `budget walmart login` and complete sign-in in the window.")
