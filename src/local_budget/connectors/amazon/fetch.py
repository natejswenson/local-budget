"""The ONLY module in this package that touches the network.

Keeping the boundary this thin is what makes everything downstream testable:
`store` and `match` take plain entity objects, so the whole test suite runs
offline with hand-built fakes and never needs credentials.
"""
from __future__ import annotations

from .session import AmazonAuthError, build_session, harden


class AmazonFetchError(RuntimeError):
    """The remote shape changed, or the session is no longer good."""


def _wrap(e: Exception) -> AmazonFetchError:
    """Turn an upstream failure into something with a next action in it.

    Any parse error here means one of two things — the cookie jar went stale,
    or Amazon changed a page — and the operator cannot tell which from a
    BeautifulSoup traceback.
    """
    return AmazonFetchError(
        f"{type(e).__name__}: {e}\n"
        "Either the session expired (run `budget amazon login`) or Amazon "
        "changed a page (upgrade with `uv add amazon-orders@latest` — this "
        "connector parses the consumer site, so upstream fixes are the "
        "maintenance story).")


def fetch_orders(*, year: int | None = None, full_details: bool = True,
                 session=None) -> list:
    """Orders, newest first. `full_details=True` is required for item-level
    data — without it Amazon's list page omits per-item prices, which is the
    entire reason this connector exists."""
    try:
        from amazonorders.orders import AmazonOrders
        s = session or build_session()
        return AmazonOrders(s).get_order_history(
            year=year, full_details=full_details, keep_paging=True)
    except AmazonAuthError:
        raise
    except Exception as e:                                    # pragma: no cover
        raise _wrap(e) from e
    finally:
        harden()


def fetch_transactions(*, days: int = 365, session=None) -> list:
    """Amazon's own list of card charges — the reconciliation key. An order
    total is frequently NOT what hit the card (one order, three shipments,
    three charges), so matching goes through these, not through order totals."""
    try:
        from amazonorders.transactions import AmazonTransactions
        s = session or build_session()
        return AmazonTransactions(s).get_transactions(days=days, keep_paging=True)
    except AmazonAuthError:
        raise
    except Exception as e:                                    # pragma: no cover
        raise _wrap(e) from e
    finally:
        harden()
