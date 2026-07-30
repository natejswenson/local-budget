"""Walmart's page payloads → the plain entity dicts `store.py` writes.

The layer the Amazon connector does not need, because it has a library. Every
field name below was read off a real captured payload, not guessed — see the
module's tests, whose fixtures reproduce those shapes exactly.

**Where the data lives.** Both pages are Next.js and embed their state in a
`__NEXT_DATA__` script tag, but not in the same place:

    /orders       props.pageProps.phRedesignInitialData.data.purchaseHistory
    /orders/<id>  props.pageProps.initialData.data.order

The list page carries totals, dates, channel and item NAMES; only the detail
page carries item PRICES. That asymmetry is why `backfill` is two passes.

**Money is passed on as the string the page displayed.** `displayValue`
("$241.30") is preferred over `value` (241.30, a JSON float) so the decimal
literal a human would have read is the one that reaches `money.py` — which is
the whole reason `store.py` can use the strict conversion path.
"""
from __future__ import annotations

import json
import re

#: Where each page keeps its payload. Tried in order; the first hit wins, so a
#: detail payload found on a list page (or vice versa) still parses.
LIST_PATH = ("props", "pageProps", "phRedesignInitialData", "data", "purchaseHistory")
DETAIL_PATH = ("props", "pageProps", "initialData", "data", "order")

#: Payment types that can appear on a bank statement. Walmart Cash and gift
#: cards settle inside Walmart and never reach the ledger, so they are not what
#: `payment_method` should describe.
BANKABLE_PAYMENTS = ("CREDITCARD", "DEBITCARD", "CREDIT_CARD", "DEBIT_CARD")

_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


class WalmartParseError(RuntimeError):
    """The page did not contain the shape this parser knows.

    Raised rather than returning empty: a silent zero here is exactly the
    failure `store.SyncAborted` exists to catch, and catching it one layer
    earlier says which page changed.
    """


def next_data(html: str) -> dict:
    """The `__NEXT_DATA__` blob from a page's HTML."""
    m = _NEXT_DATA.search(html)
    if not m:
        raise WalmartParseError(
            "no __NEXT_DATA__ script on the page — Walmart may have moved off "
            "this rendering, or the page is a challenge/sign-in interstitial")
    try:
        return json.loads(m.group(1))
    except ValueError as e:
        raise WalmartParseError(f"__NEXT_DATA__ is not valid JSON: {e}") from e


def _dig(blob: dict, path: tuple[str, ...]):
    cur = blob
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def purchase_history(blob: dict) -> dict | None:
    """The order-list payload, or None if this is not a list page."""
    return _dig(blob, LIST_PATH)


def order_detail_payload(blob: dict) -> dict | None:
    """The single-order payload, or None if this is not a detail page."""
    return _dig(blob, DETAIL_PATH)


def _money(node) -> str | None:
    """A Walmart price node → the decimal string the page displayed.

    `displayValue` first: it is what a human read, and passing the literal along
    is what lets `store.to_cents` use the strict, non-rounding conversion. The
    float is a fallback for a node that carries only `value`.
    """
    if node is None:
        return None
    if isinstance(node, (str, int, float)):
        return str(node)
    if not isinstance(node, dict):
        return None
    shown = node.get("displayValue")
    if isinstance(shown, str) and shown.strip():
        return shown
    v = node.get("value")
    return None if v is None else str(v)


def _payment_method(order: dict) -> str | None:
    """A short label for the card that paid, if one did.

    Only bankable payment types are considered: an order settled entirely in
    Walmart Cash never reaches the statement, and labelling it "Walmart Cash"
    would suggest the ledger should hold a matching charge.
    """
    for pm in order.get("paymentMethods") or []:
        if (pm.get("paymentType") or "").upper() not in BANKABLE_PAYMENTS:
            continue
        parts = [pm.get("cardType"), pm.get("description")]
        label = " ".join(p for p in parts if p)
        if label:
            return label
    return None


def _channel(order: dict) -> str | None:
    """'in-store' | 'online' | None.

    Only the LIST page carries `isInStore`; the detail page has no equivalent
    field. Returning None there rather than defaulting to 'online' is
    deliberate — `store.store_orders` coalesces, so a detail pass must not
    overwrite what the list pass established.
    """
    v = order.get("isInStore")
    if v is None:
        return None
    return "in-store" if v else "online"


def _items_from_groups(groups: list | None) -> list[dict]:
    """Item lines out of a page's fulfilment groups.

    The seller lives on the GROUP, not the item — a marketplace order splits
    into one group per seller — so it is pushed down onto each line, which is
    where every reader of this data expects it.

    Lines with no price survive with `line_price=None`: on a list page NO line
    has one, and dropping them would turn "we have not fetched detail yet" into
    "this order had no items".
    """
    out: list[dict] = []
    for g in groups or []:
        seller = ((g.get("seller") or {}).get("name")) or None
        for it in g.get("items") or []:
            info = it.get("productInfo") or {}
            price = (it.get("priceInfo") or {}).get("linePrice")
            out.append({
                # The list page puts the title at `name`; the detail page nests
                # it under `productInfo`. Same field, two homes.
                "title": info.get("name") or it.get("name"),
                "product_id": info.get("usItemId") or it.get("offerId"),
                "quantity": it.get("quantity") or 1,
                "line_price": _money(price),
                "seller": seller,
                # Walmart publishes no product taxonomy on these pages — the
                # `categories` node on a group is a fulfilment flag, not a shelf
                # category. The column stays NULL and the report falls back to
                # its keyword heuristic, which is what it is written for.
                "category": None,
                "status": "unavailable" if it.get("isUnavailable") else None,
            })
    return out


def order_from_list(o: dict) -> dict:
    """One entry of `purchaseHistory.orders` → an entity dict.

    `detail_fetched` is False: this page has item names but no prices, so the
    order is not fully known yet and `backfill` must still queue it.
    """
    price = o.get("priceDetails") or {}
    return {
        "order_number": str(o.get("id") or "").strip(),
        "order_placed_date": (o.get("orderDate") or "")[:10] or None,
        "grand_total": _money(price.get("orderTotal") or price.get("grandTotal")),
        "subtotal": _money(price.get("subTotal")),
        "tax": _money(price.get("taxTotal")),
        "item_count": o.get("itemCount"),
        "channel": _channel(o),
        "payment_method": _payment_method(o),
        "cancelled": False,
        "detail_fetched": False,
        "items": _items_from_groups(o.get("groups")),
    }


def order_from_detail(o: dict) -> dict:
    """`initialData.data.order` → an entity dict, items priced.

    The groups key carries a version suffix (`groups_2101`), so it is found by
    prefix rather than named: a bumped suffix would otherwise silently yield an
    order with no items, which reads as a genuinely empty order.
    """
    price = o.get("priceDetails") or {}
    groups = next((v for k, v in o.items()
                   if k.startswith("groups") and isinstance(v, list)), None)
    if groups is None:
        raise WalmartParseError(
            f"order {o.get('id')} has no groups_* array — the detail page shape "
            f"changed; run `budget walmart capture` to see what it serves now")
    return {
        "order_number": str(o.get("id") or "").strip(),
        "order_placed_date": (o.get("orderDate") or "")[:10] or None,
        "grand_total": _money(price.get("grandTotal") or price.get("orderTotal")),
        "subtotal": _money(price.get("subTotal")),
        "tax": _money(price.get("taxTotal")),
        "shipping": _money(price.get("shippingTotal")),
        "savings": _money(price.get("savings") or price.get("totalSavings")),
        "item_count": o.get("itemCount"),
        "channel": _channel(o),
        "payment_method": _payment_method(o),
        "cancelled": False,
        "detail_fetched": True,
        "items": _items_from_groups(groups),
    }


def orders_from_list_payload(ph: dict) -> list[dict]:
    """Every order in a purchase-history payload."""
    return [order_from_list(o) for o in (ph.get("orders") or [])
            if (o or {}).get("id")]


def next_cursor(ph: dict) -> str | None:
    return ((ph.get("pageInfo") or {}).get("nextPageCursor")) or None
