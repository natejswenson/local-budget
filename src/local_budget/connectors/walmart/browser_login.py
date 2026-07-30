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

POLL_SECONDS = 2
DEFAULT_TIMEOUT = 600

#: How long to go without probing when the cookie heuristic says nothing. The
#: heuristic is a GUESS about cookie names (see `session.AUTH_COOKIE_CANDIDATES`)
#: and the first version of this treated it as a requirement — so an account
#: whose cookies are named anything else could sign in perfectly and still time
#: out, never having been asked. Now it only ACCELERATES: miss it and the probe
#: still runs, just less often.
FORCED_PROBE_SECONDS = 30


def signed_in(url: str) -> bool:
    """Does this landing URL mean we got in?"""
    return "/orders" in url and not any(p in url for p in LOGIN_PATHS)


def looks_done(url: str, cookie_names: set[str]) -> bool:
    """Cheap "the human is probably finished" hint. Never a precondition.

    Off the login page, and at least one cookie we associate with an account.
    When it fires we probe immediately; when it does not, `FORCED_PROBE_SECONDS`
    probes anyway.
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
    echo(f"  Nothing is captured until your order history actually loads. "
         f"You have {timeout // 60} minutes.")

    with browser.context(headless=False) as (ctx, page):
        page.goto(SIGN_IN_URL)
        # The probe runs in a SECOND TAB, never in the tab the human is using.
        # Confirming means loading the orders page, and doing that in their tab
        # would yank them out of a half-typed one-time code — which is why the
        # first version only dared probe on a cookie signal, and why it hung
        # when that signal never came. A background tab can be probed freely.
        probe = ctx.new_page()
        deadline = time.monotonic() + timeout
        last_probe = 0.0
        last_url = ""
        seen: list[str] = []

        try:
            while time.monotonic() < deadline:
                names = {c["name"] for c in ctx.cookies()
                         if WALMART_DOMAIN in (c.get("domain") or "")}
                try:
                    last_url = page.url
                except Exception:                          # pragma: no cover
                    pass                                   # mid-navigation
                due = (time.monotonic() - last_probe) >= FORCED_PROBE_SECONDS
                if looks_done(last_url, names) or due:
                    last_probe = time.monotonic()
                    try:
                        probe.goto(ORDERS_URL, wait_until="domcontentloaded")
                    except Exception:                      # pragma: no cover
                        time.sleep(POLL_SECONDS)
                        continue
                    if signed_in(probe.url):
                        n = save_storage_state(ctx.storage_state())
                        harden()
                        echo(f"  ✓ captured {n} cookies")
                        return {"cookies": n, "path": str(storage_state_path())}
                time.sleep(POLL_SECONDS)
        finally:
            # Cookie NAMES only — never values. They are what tells us whether
            # AUTH_COOKIE_CANDIDATES is right, and a timeout with no diagnostic
            # leaves the next attempt guessing exactly as blindly as this one.
            try:
                seen = sorted({c["name"] for c in ctx.cookies()
                               if WALMART_DOMAIN in (c.get("domain") or "")})
                probe.close()
            except Exception:                              # pragma: no cover
                pass          # a diagnostic must never mask the real failure

    raise WalmartAuthError(
        f"timed out after {timeout}s without a signed-in Walmart session.\n"
        f"  last page in the window: {last_url or '(none)'}\n"
        f"  walmart.com cookies present: {', '.join(seen) if seen else '(none)'}\n"
        "If you did finish signing in, that cookie list is the diagnostic — the "
        "orders page was still redirecting to sign-in when asked.")
