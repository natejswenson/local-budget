"""The ONLY module in this package that touches the network.

Keeping the boundary this thin is what makes everything downstream testable:
`parse`, `store` and `match` take plain dicts, so the whole test suite runs
offline with literals and never needs a session.

**The standing rule: navigate pages, never call the API.** Walmart's order list
is served by a persisted GraphQL query (`PurchaseHistoryV3`), and calling it
directly is the obvious way to page faster than five orders at a time. It was
tried. It returns 412, and repeating it escalates to a full PerimeterX
interstitial that locks the whole account out of order history for a while. The
app signs its own requests with headers we have no business reconstructing.

So this drives the page as a person would: load `/orders`, read the payload the
server rendered into it, click "Next page", and read the response the app itself
fetched. Slower, and correct. `WalmartBlocked` exists so that when a block does
happen it is never misreported as an expired session — the remedies are
opposites, and signing in again from a throttled address makes it worse.
"""
from __future__ import annotations

import time
from contextlib import contextmanager

from . import browser, parse
from .browser_login import ORDERS_URL, blocked, signed_in
from .session import WalmartAuthError, WalmartBlocked, require_session

ORDER_URL = "https://www.walmart.com/orders/{}"

#: The pagination control, by the label Walmart gives it.
NEXT_PAGE = '[aria-label="Next page"]'

#: Let the app settle after a navigation. The first list page is server-rendered
#: so it needs little; a detail page hydrates its order in afterwards.
LIST_SETTLE_MS = 4000
DETAIL_SETTLE_MS = 6000

#: Between navigations. Deliberate courtesy, and cheap insurance: the block that
#: taught this module its rule came from going fast. Raised to 5s after a real
#: sync was challenged on its FIRST order-detail navigation, having just paged
#: the list twice — the per-order pages are the sensitive ones.
POLITE_DELAY_SECONDS = 5.0

#: Hard stop on paging, so a cursor that stops advancing cannot loop forever.
MAX_PAGES = 80


class WalmartFetchError(RuntimeError):
    """The remote shape changed, or the page never produced what we asked for."""


def _guard(page) -> str:
    """Return the page HTML, or raise with the reason it is not usable.

    Every navigation goes through here. The three outcomes are genuinely
    different and each needs its own next action: a block means wait, a guest
    wall means sign in, and anything else is ours to parse.
    """
    html = page.content()
    if blocked(page.url, html):
        raise WalmartBlocked(
            "Walmart served its bot challenge instead of the page.\n"
            "  Wait before retrying. Do NOT sign in again — that sends more "
            "traffic from an address already being throttled.")
    if not signed_in(page.url, html):
        raise WalmartAuthError(
            f"the orders page served the guest sign-in wall (at {page.url}) — "
            f"run `budget walmart login` to capture a fresh session")
    return html


class Fetcher:
    """A live browser context, scoped to one sync.

    One context for a whole run rather than one per request: each launch is a
    fresh fingerprint arriving with a restored session, which is the pattern
    bot defence is built to notice.
    """

    def __init__(self, page, *, delay: float = POLITE_DELAY_SECONDS):
        self._page = page
        self._delay = delay
        self._responses: list[dict] = []
        page.on("response", self._on_response)

    def _on_response(self, resp) -> None:
        """Keep the purchase-history payloads the APP fetches for itself."""
        if "PurchaseHistoryV3" not in resp.url:
            return
        try:
            body = resp.json()
        except Exception:                                      # pragma: no cover
            return
        data = body.get("data") or {}
        ph = data.get("orders") or data.get("purchaseHistory")
        if ph:
            self._responses.append(ph)

    def _goto(self, url: str, settle_ms: int) -> str:
        self._page.goto(url, wait_until="domcontentloaded")
        self._page.wait_for_timeout(settle_ms)
        return _guard(self._page)

    def order_list(self, *, since: str | None = None, on_progress=None) -> list[dict]:
        """Every order back to `since`, newest first.

        Stops at the first page whose orders are ALL older than `since` rather
        than at the first old order: a page is a mixed batch, and stopping mid-page
        would drop the newer orders sharing it.
        """
        def say(msg: str) -> None:
            if on_progress:
                on_progress(msg)

        html = self._goto(ORDERS_URL, LIST_SETTLE_MS)
        first = parse.purchase_history(parse.next_data(html))
        if first is None:
            raise WalmartFetchError(
                "the orders page carried no purchase-history payload — Walmart "
                "changed the page. Run `budget walmart capture` to see what it "
                "serves now.")

        seen: dict[str, dict] = {}
        pages = [first]
        self._responses.clear()

        for page_no in range(MAX_PAGES):
            batch = parse.orders_from_list_payload(pages[-1])
            for o in batch:
                seen.setdefault(o["order_number"], o)
            say(f"    page {page_no + 1}: {len(batch)} orders "
                f"({batch[-1]['order_placed_date'] if batch else '—'})")
            # A whole page older than the window means everything after it is
            # older still.
            if since and batch and all(
                    (o["order_placed_date"] or "") < since for o in batch):
                break
            if not batch:
                break

            got = self._next_page()
            if got is None:
                break
            pages.append(got)
            time.sleep(self._delay)

        return list(seen.values())

    def _next_page(self) -> dict | None:
        """Click through to the next page and return what the app fetched.

        None when there is no next page. The button sits at the foot of a long
        list, so it is scrolled to first — left alone it stays attached but not
        visible, and every wait on it times out.
        """
        page = self._page
        page.mouse.wheel(0, 30000)
        page.wait_for_timeout(600)
        btn = page.locator(NEXT_PAGE)
        if not btn.count():
            return None
        try:
            btn.first.scroll_into_view_if_needed()
            if btn.first.is_disabled():
                return None
            before = len(self._responses)
            btn.first.click()
        except Exception:
            return None

        for _ in range(20):
            page.wait_for_timeout(500)
            if len(self._responses) > before:
                return self._responses[-1]
        # The click landed but nothing came back. Stopping here loses the tail
        # of history, which `backfill` reports; inventing a result would lose it
        # silently.
        return None

    def order_detail(self, order_number: str) -> dict:
        """One order, with its item prices."""
        html = self._goto(ORDER_URL.format(order_number), DETAIL_SETTLE_MS)
        payload = parse.order_detail_payload(parse.next_data(html))
        if payload is None:
            raise WalmartFetchError(
                f"order {order_number} carried no order payload — Walmart "
                f"changed the detail page. Run `budget walmart capture`.")
        return parse.order_from_detail(payload)


@contextmanager
def browser_session(*, headless: bool = True):
    """Yield a `Fetcher` over an authenticated browser context."""
    state = require_session()
    with browser.context(headless=headless, storage_state=state) as (_ctx, page):
        yield Fetcher(page)


def fetch_orders(*, since: str | None = None, detail: bool = True,
                 headless: bool = True, on_progress=None) -> list[dict]:
    """Orders back to `since`, item detail included when asked for.

    The convenience wrapper `sync` uses. `backfill` drives `browser_session`
    itself, because it needs to interleave storing with fetching so an
    interrupted run keeps what it collected.
    """
    with browser_session(headless=headless) as f:
        orders = f.order_list(since=since, on_progress=on_progress)
        if not detail:
            return orders
        out = []
        for o in orders:
            if on_progress:
                on_progress(f"    detail for {o['order_number']}")
            out.append(f.order_detail(o["order_number"]))
            time.sleep(f._delay)
        return out
