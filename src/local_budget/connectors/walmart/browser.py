"""One place that knows how to open a browser. Used by login AND by fetch.

The Amazon connector needs a browser only to sign in; afterwards it replays a
cookie into an HTTP client. Walmart's edge rejects that, so the browser is the
transport for the whole connector — which makes "how do we open a browser"
shared plumbing rather than a detail of the login flow, and worth exactly one
implementation.

Nothing here tries to hide what it is. The settings below make an automated
Chromium behave like the ordinary desktop Chrome the account already signs in
from — a real locale, a real timezone, a real window size, a UA string without
`HeadlessChrome` in it — so a legitimate session is not challenged for looking
malformed. That is the goal: don't get spuriously blocked, not evade a block.
"""
from __future__ import annotations

from contextlib import contextmanager

from .session import WalmartAuthError

#: A current desktop Chrome UA. Playwright's headless default announces
#: `HeadlessChrome`, which is enough on its own to get a session challenged.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")

VIEWPORT = {"width": 1280, "height": 900}
LOCALE = "en-US"
TIMEZONE = "America/Chicago"

#: Chromium's own automation banner. Left on, it is advertised in
#: `navigator.webdriver` and in the CDP-flavoured feature set.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def playwright_or_raise():
    """`sync_playwright`, or an error that says how to get it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:                                  # pragma: no cover
        raise WalmartAuthError(
            "Playwright is not installed. Run `uv sync` then "
            "`uv run playwright install chromium`.") from e
    return sync_playwright


@contextmanager
def context(*, headless: bool = True, storage_state: dict | None = None):
    """Yield ``(context, page)`` for a browser that looks like a normal one.

    Always a fresh context seeded from `storage_state`, never a persistent
    profile on disk: the captured session lives in one hardened file that we
    control, and a second logged-in Walmart profile lying around would be a
    credential nobody is managing.
    """
    sync_playwright = playwright_or_raise()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        ctx = browser.new_context(
            storage_state=storage_state, user_agent=USER_AGENT,
            viewport=VIEWPORT, locale=LOCALE, timezone_id=TIMEZONE)
        try:
            yield ctx, ctx.new_page()
        finally:
            ctx.close()
            browser.close()
