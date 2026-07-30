"""Sync orchestration: fetch → store → match, inside one run ledger.

Everything is written through `db.connect()` (the deterministic core's full
read/write handle), never `agent_connect()`. That is deliberate: Amazon rows
are imported facts on the same footing as bank transactions, so the agent can
read them and can never write them — the authorizer denies writes to any table
not in `_AGENT_WRITE_TABLES`, and none of the `amazon_*` tables are listed.
"""
from __future__ import annotations

from datetime import date, timedelta

from ... import db
from . import fetch, match, store
from .match import MERCHANT_LIKE


def _ledger_has_amazon_charges(conn, since: str) -> bool:
    """Does the BANK think there were Amazon charges in this window?

    This is the ground truth the anti-vacuity check leans on. If the ledger
    says yes and the connector returns nothing, the parser is broken — a fact
    only knowable by comparing against data we already trust.
    """
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    row = conn.execute(
        f"""SELECT COUNT(*) AS n FROM transactions
             WHERE status='posted' AND amount_cents < 0
               AND posted_date >= ? AND ({like})""",
        (since, *MERCHANT_LIKE)).fetchone()
    return bool(row["n"])


def run_sync(*, days: int | None = 365, year: int | None = None,
             session=None) -> dict:
    """Pull orders + transactions, store them, then match. Returns a summary.

    Raises `store.SyncAborted` when the result is empty but the ledger says it
    should not be, and writes nothing in that case — a broken parser must never
    look like a quiet month.
    """
    scope = f"year={year}" if year else f"days={days}"
    window_days = days if days is not None else 400
    since = (date.today() - timedelta(days=window_days)).isoformat()

    orders = fetch.fetch_orders(year=year, full_details=True, session=session)
    txns = fetch.fetch_transactions(days=window_days, session=session)

    summary: dict = {}
    try:
        with db.connect() as conn:
            expect = _ledger_has_amazon_charges(conn, since)
            # Checked BEFORE a run is opened, so a broken parse leaves no trace
            # and no half-written state — nothing began, nothing to unwind.
            store.assert_not_vacuous(conn, orders=len(orders), txns=len(txns),
                                     scope_has_known_charges=expect)
            run_id = store.start_run(conn, scope)
            n_orders = store.store_orders(conn, orders, run_id)
            n_txns = store.store_transactions(conn, txns, run_id)
            result = match.run(conn)
            store.finish_run(conn, run_id, status="success",
                             orders_seen=len(orders), orders_upserted=n_orders,
                             txns_seen=len(txns), txns_upserted=n_txns)
            summary = {
                "sync_run_id": run_id, "scope": scope,
                "orders": n_orders, "transactions": n_txns,
                "matched": result["matched"], "exact": result["exact"],
                "windowed": result["windowed"],
                "ambiguous": len(result["ambiguous"]),
                "coverage": match.coverage(conn),
            }
    except store.SyncAborted:
        raise                      # nothing was written; there is no run to log
    except Exception as e:
        # `db.connect()` rolls back on exception, which would take the
        # failure record with it. A second, independent transaction is the only
        # way the run ledger keeps evidence that this sync ran and broke.
        with db.connect() as c2:
            rid = store.start_run(c2, scope)
            store.finish_run(c2, rid, status="failed", error=str(e)[:500])
        raise
    return summary
