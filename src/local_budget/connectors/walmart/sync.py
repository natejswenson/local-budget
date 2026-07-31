"""Sync orchestration: fetch → store → match, inside one run ledger.

Everything is written through `db.connect()` (the deterministic core's full
read/write handle), never `agent_connect()`. That is deliberate: Walmart rows
are imported facts on the same footing as bank transactions, so the agent can
read them and can never write them — the authorizer denies writes to any table
not in `_AGENT_WRITE_TABLES`, and none of the `walmart_*` tables are listed.

Simpler than the Amazon equivalent in one way: there is only one thing to fetch.
Amazon pulls orders and a separate transaction list; Walmart publishes no charge
list at all, so `match.py` recovers each order's settlement by summing bank rows
against the order total.
"""
from __future__ import annotations

from datetime import date, timedelta

from ... import db
from . import match, store
from .match import MERCHANT_LIKE


def ledger_has_walmart_charges(conn, since: str, until: str | None = None) -> bool:
    """Does the BANK think there were Walmart charges in this window?

    This is the ground truth the anti-vacuity check leans on. If the ledger says
    yes and the connector returns nothing, the parser is broken — a fact only
    knowable by comparing against data we already trust.

    `until` bounds the window at the top, which a scoped backfill needs. With
    only a lower bound, checking an old range would also count every recent
    charge, so a fetch that silently returned nothing would still look
    "expected" and pass — the check would be vacuous exactly where a long job
    most needs it.
    """
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    upper = " AND posted_date <= ?" if until else ""
    row = conn.execute(
        f"""SELECT COUNT(*) AS n FROM transactions
             WHERE status='posted' AND amount_cents < 0
               AND posted_date >= ?{upper} AND ({like})""",
        (since, *((until,) if until else ()), *MERCHANT_LIKE)).fetchone()
    return bool(row["n"])


def store_and_match(orders: list, *, scope: str, since: str) -> dict:
    """The offline half of a sync: gate, store, match, record the run.

    Split out from `run_sync` so it can be tested without a browser, and reused
    by `backfill` — which fetches on a different schedule but needs exactly this
    afterwards.

    `since` is the start of the window that was ASKED for, and it is required.
    An empty result can only be judged against the window it came from: without
    one, an account that genuinely has no Walmart orders is indistinguishable
    from a parser that returned none, and the gate would either abort on every
    legitimate empty sync or never abort at all.

    Raises `store.SyncAborted` when the result is empty but the ledger says it
    should not be, and writes nothing in that case.
    """
    with db.connect() as conn:
        expect = ledger_has_walmart_charges(conn, since)
        # Checked BEFORE a run is opened, so a broken parse leaves no trace and
        # no half-written state — nothing began, nothing to unwind.
        store.assert_not_vacuous(conn, orders=len(orders),
                                 scope_has_known_charges=expect)
        run_id = store.start_run(conn, scope)
        n = store.store_orders(conn, orders, run_id)
        result = match.run(conn)
        store.finish_run(conn, run_id, status="success",
                         orders_seen=len(orders), orders_upserted=n["orders"],
                         items_seen=n["items"], items_upserted=n["items"])
        cov = match.coverage(conn)
        # `matched` is the TOTAL that now reconciles, not what this run added.
        # A re-sync over an already-matched window adds nothing and printing
        # "matched 0" reads as a failed run — the same trap backfill fell into.
        return {
            "sync_run_id": run_id, "scope": scope,
            "orders": n["orders"], "items": n["items"],
            "matched": cov["split_settlements"]["orders"],
            "new_matches": result["matched"],
            "exact": result["exact"], "split": result["split"],
            "ambiguous": len(result["ambiguous"]),
            "coverage": cov,
            "horizon": match.horizon(conn),
        }


def record_failure(scope: str, error: Exception) -> None:
    """Log a failed run in its own transaction.

    `db.connect()` rolls back on exception, which would take the failure record
    with it. A second, independent transaction is the only way the run ledger
    keeps evidence that this sync ran and broke.
    """
    with db.connect() as conn:
        rid = store.start_run(conn, scope)
        store.finish_run(conn, rid, status="failed", error=str(error)[:500])


def run_sync(*, days: int = 90, detail: bool = False, headless: bool = True,
             on_progress=None) -> dict:
    """Pull recent orders, store them, match, then optionally fill in detail.

    **The list is stored BEFORE any detail page is fetched**, and that ordering
    is the whole point. Matching needs only the order total, which the list page
    carries — so a sync that reaches the list has already done the reconciling
    work even if every subsequent request fails. The first version fetched all
    detail before storing anything, and a bot challenge on the first detail page
    threw away ten successfully-fetched orders.

    **`detail` is off by default**, for the same reason plus one more: item
    detail is one page load per order, which is both the slow part and the part
    that draws a challenge. A plain `sync` should be cheap enough to run often;
    `budget walmart backfill` is where item lines get collected, at its own pace
    and resumably.
    """
    from . import fetch                       # lazy: pulls in Playwright

    def say(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    scope = f"days={days}"
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        with fetch.browser_session(headless=headless) as f:
            orders = f.order_list(since=since, on_progress=on_progress)
            summary = store_and_match(orders, scope=scope, since=since)

            if detail:
                # Best-effort from here. Everything above is already committed,
                # so a challenge now costs item lines, not the reconciliation.
                with db.connect() as conn:
                    todo = [o["order_number"] for o in orders
                            if not conn.execute(
                                "SELECT detail_fetched FROM walmart_orders "
                                "WHERE order_number = ?",
                                (o["order_number"],)).fetchone()["detail_fetched"]]
                got = 0
                for num in todo:
                    try:
                        d = f.order_detail(num)
                    except Exception as e:                    # noqa: BLE001
                        say(f"    detail stopped after {got} orders — {e}")
                        break
                    with db.connect() as conn:
                        rid = store.start_run(conn, f"sync-detail {num}")
                        store.store_orders(conn, [d], rid)
                        store.finish_run(conn, rid, status="success",
                                         orders_seen=1, orders_upserted=1)
                    got += 1
                summary["detailed"] = got
    except store.SyncAborted:
        raise                      # nothing was written; there is no run to log
    except Exception as e:
        record_failure(scope, e)
        raise
    return summary
