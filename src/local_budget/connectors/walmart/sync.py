"""Sync orchestration: fetch → store → match, inside one run ledger.

Everything is written through `db.connect()` (the deterministic core's full
read/write handle), never `agent_connect()`. That is deliberate: Walmart rows
are imported facts on the same footing as bank transactions, so the agent can
read them and can never write them — the authorizer denies writes to any table
not in `_AGENT_WRITE_TABLES`, and none of the `walmart_*` tables are listed.

Simpler than the Amazon equivalent in one way: there is only one thing to
fetch. Amazon pulls orders and a separate transaction list; Walmart's charges
come out of the orders themselves (see `store._store_charges`).
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
                         charges_seen=n["charges"], charges_upserted=n["charges"])
        return {
            "sync_run_id": run_id, "scope": scope,
            "orders": n["orders"], "charges": n["charges"],
            "matched": result["matched"], "exact": result["exact"],
            "windowed": result["windowed"],
            "ambiguous": len(result["ambiguous"]),
            "coverage": match.coverage(conn),
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


def run_sync(*, days: int = 90, headless: bool = True, on_progress=None) -> dict:
    """Pull recent orders with item detail, store them, then match."""
    from . import fetch                       # lazy: pulls in Playwright

    scope = f"days={days}"
    since = (date.today() - timedelta(days=days)).isoformat()
    try:
        orders = fetch.fetch_orders(since=since, detail=True, headless=headless,
                                    on_progress=on_progress)
    except Exception as e:
        record_failure(scope, e)
        raise
    try:
        return store_and_match(orders, scope=scope, since=since)
    except store.SyncAborted:
        raise                      # nothing was written; there is no run to log
    except Exception as e:
        record_failure(scope, e)
        raise
