"""Interactive sign-in: capture a session instead of storing a password.

Walmart sign-in routinely ends in a one-time code sent to a phone or an inbox,
and often a bot challenge before that. None of it is answerable by a stored
secret, so the only flow that actually works is the honest one: a human signs in
however Walmart asks them to, in a real window, and we keep the result.

**Walmart does not redirect an unauthenticated visitor away from /orders.** It
serves the same URL with a guest "Track your order" form instead of the order
list. That is checked, not assumed: a browser that has never signed in lands on
`https://www.walmart.com/orders` with a 200, the identical page title, and even
`ACID`/`hasACID` cookies. So neither the URL nor any cookie name distinguishes
signed-in from signed-out — only the page content does, and an earlier version
of this module that tested the URL captured an anonymous session in about two
seconds and reported success.

The trade is re-authentication. When Walmart expires the session, sync fails and
you run this again.

Requires Playwright:  uv sync  &&  uv run playwright install chromium
"""
from __future__ import annotations

import time

from . import browser
from .session import (WalmartAuthError, harden, save_storage_state,
                      storage_state_path)

SIGN_IN_URL = "https://www.walmart.com/account/login"
ORDERS_URL = "https://www.walmart.com/orders"

#: URL paths that are unambiguously still mid-sign-in.
LOGIN_PATHS = ("/account/login", "/signin", "/account/signin")

#: Copy that appears on the GUEST version of the orders page and cannot appear
#: on the signed-in one. Verified against a real anonymous fetch rather than
#: guessed. This is a NEGATIVE test — it proves we are logged out, and its
#: absence is taken as proof we are not — which is the honest way round: the
#: signed-in page's own markup is not knowable until someone signs in, while
#: the guest wall is knowable today and is what we must never mistake for data.
GUEST_MARKERS = (
    "Sign in to do more with your account",
    "you can still track your order status",
)

#: Walmart's bot interstitial. Detected separately from the guest wall because
#: the two call for opposite responses — see `session.WalmartBlocked`.
BLOCK_MARKERS = ("Robot or human?", "px-captcha", "/blocked?url=")

#: Every few seconds, in a background tab. Cheap because it disturbs nothing.
POLL_SECONDS = 5
DEFAULT_TIMEOUT = 600


def blocked(url: str, html: str) -> bool:
    """Did Walmart's bot defence answer instead of Walmart?

    Checked before anything else, because a block page satisfies every negative
    test we have — it is not the guest wall and not an order list — and would
    otherwise be reported as "your session expired", sending someone to sign in
    again from the address currently being throttled.
    """
    return "/blocked" in url or any(m in html for m in BLOCK_MARKERS)


def signed_in(url: str, html: str) -> bool:
    """Did that page load actually show us an account's order history?

    Both halves matter. The URL rules out being mid-flow on a sign-in screen;
    the content rules out the guest wall, which serves a 200 at the very URL we
    asked for.
    """
    if blocked(url, html):
        return False
    if "/orders" not in url or any(p in url for p in LOGIN_PATHS):
        return False
    return not any(m in html for m in GUEST_MARKERS)


def login(*, timeout: int = DEFAULT_TIMEOUT, echo=print) -> dict:
    """Open a real browser, wait for the human, capture the session.

    Returns ``{"cookies": n, "path": str}``. Raises `WalmartAuthError` on
    timeout or if the captured session is not actually authenticated.
    """
    echo("  Opening a browser window — sign in to Walmart however it asks "
         "(code, passkey, challenge).")
    echo(f"  Take your time: nothing is captured until your ORDER HISTORY "
         f"actually loads. You have {timeout // 60} minutes.")

    with browser.context(headless=False) as (ctx, page):
        page.goto(SIGN_IN_URL)
        # The probe runs in a SECOND TAB, never in the tab the human is using.
        # Confirming means loading the orders page, and doing that in their tab
        # would destroy a half-typed one-time code.
        probe = ctx.new_page()
        deadline = time.monotonic() + timeout
        announced = False
        seen: list[str] = []
        last_url = ""

        try:
            while time.monotonic() < deadline:
                try:
                    probe.goto(ORDERS_URL, wait_until="domcontentloaded")
                    last_url, html = probe.url, probe.content()
                except Exception:                          # pragma: no cover
                    time.sleep(POLL_SECONDS)   # mid-navigation; try again
                    continue
                if signed_in(last_url, html):
                    n = save_storage_state(ctx.storage_state(), verified=True)
                    harden()
                    echo(f"  ✓ order history loaded — captured {n} cookies")
                    return {"cookies": n, "path": str(storage_state_path())}
                if not announced:
                    echo("  … waiting — the orders page is still showing the "
                         "guest sign-in wall")
                    announced = True
                time.sleep(POLL_SECONDS)
        finally:
            try:
                seen = sorted({c["name"] for c in ctx.cookies()
                               if "walmart.com" in (c.get("domain") or "")})
                probe.close()
            except Exception:                              # pragma: no cover
                pass          # a diagnostic must never mask the real failure

    raise WalmartAuthError(
        f"timed out after {timeout}s — the orders page never showed an order "
        f"history, only the guest sign-in wall.\n"
        f"  last URL probed: {last_url or '(none)'}\n"
        f"  walmart.com cookies present: {len(seen)}\n"
        "Nothing was saved. Re-run `budget walmart login` and complete sign-in "
        "in the window before it closes.")
