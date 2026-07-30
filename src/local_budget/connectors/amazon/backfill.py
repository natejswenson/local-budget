"""Multi-year history backfill.

`sync` pulls a rolling window, which explains recent months and almost nothing
else. This walks the whole range the ledger covers.

**The asymmetry that shapes everything here:** `get_order_history(year=Y)` is
year-scoped, while `get_transactions(days=N)` is days-back only. So orders are
fetched year by year and transactions in one long call — and the two can reach
different distances into the past. Matching goes through transactions, so
whatever the transactions page can reach IS the reconcilable window, regardless
of how many orders were stored. `match.horizon()` reports that boundary rather
than letting it show up as an unexplained low coverage number.

**Resumability uses the run ledger already there.** `amazon_sync_runs.scope`
records `year=YYYY`, so a finished year is simply a row that exists; a resumed
run skips it. A backfill that dies at 80% keeps everything it stored, and there
is no new state to get out of sync.
"""
from __future__ import annotations

import time
from datetime import date

from ... import db
from . import fetch, match, store, sync
from .match import MERCHANT_LIKE
from .session import AmazonAuthError

#: Transient-failure retry schedule, in seconds. Deliberately short and few: a
#: long scrape that keeps hammering a throttling server gets blocked harder,
#: and a human can always resume.
BACKOFF_SECONDS = (5, 20, 60)


def year_range(conn) -> tuple[int, int] | None:
    """First and last year with Amazon charges in the ledger.

    Derived from data rather than guessed, so the default scope is exactly the
    period there is something to reconcile.
    """
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    row = conn.execute(
        f"""SELECT MIN(substr(posted_date,1,4)) AS lo,
                   MAX(substr(posted_date,1,4)) AS hi
              FROM transactions
             WHERE status='posted' AND amount_cents < 0 AND ({like})""",
        MERCHANT_LIKE).fetchone()
    if not row or not row["lo"]:
        return None
    return int(row["lo"]), int(row["hi"])


def completed_years(conn) -> set[int]:
    """Years already fetched successfully, from the existing run ledger."""
    out: set[int] = set()
    for r in conn.execute(
            "SELECT scope FROM amazon_sync_runs WHERE status='success' "
            "AND scope LIKE 'year=%'"):
        try:
            out.add(int(str(r["scope"]).split("=", 1)[1]))
        except (ValueError, IndexError):
            continue
    return out


def _with_retry(fn, *, what: str, on_progress=None):
    """Retry a fetch on transient failure only.

    An auth or bot-challenge error is re-raised immediately and never retried:
    the credential is not going to become valid on the third attempt, and
    repeatedly re-hitting a challenge is how a soft block becomes a hard one.
    """
    last: Exception | None = None
    for i, wait in enumerate((0, *BACKOFF_SECONDS)):
        if wait:
            if on_progress:
                on_progress(f"    retrying {what} in {wait}s "
                            f"({i}/{len(BACKOFF_SECONDS)}) — {last}")
            time.sleep(wait)
        try:
            return fn()
        except AmazonAuthError:
            raise
        except Exception as e:                                # noqa: BLE001
            if "challenge" in str(e).lower() or "captcha" in str(e).lower():
                raise
            last = e
    raise RuntimeError(f"{what} failed after {len(BACKOFF_SECONDS)} retries: {last}")


def plan(conn, from_year: int | None = None, to_year: int | None = None,
         resume: bool = True) -> dict:
    """What a backfill would do, without doing it."""
    rng = year_range(conn)
    if rng is None:
        return {"years": [], "skipped": [], "days": 0, "reason": "no Amazon charges in the ledger"}
    lo, hi = rng
    lo = from_year or lo
    hi = to_year or hi
    done = completed_years(conn) if resume else set()
    years = [y for y in range(lo, hi + 1) if y not in done]
    skipped = [y for y in range(lo, hi + 1) if y in done]
    # Transactions are days-back, so size the single call to reach the start of
    # the range (+31 days of slack for a charge that settled after year end).
    days = (date.today() - date(lo, 1, 1)).days + 31
    return {"years": years, "skipped": skipped, "days": days, "reason": None}


def run_backfill(*, from_year: int | None = None, to_year: int | None = None,
                 resume: bool = True, on_progress=None, session=None) -> dict:
    """Fetch transactions once, then orders year by year, then match once.

    Transactions come first because they are the reconciliation key — orders
    without them cannot be matched to anything. Matching runs once at the end
    rather than per year: it is global and cheap, and per-year passes would
    redo the same work N times for no benefit.
    """
    def say(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    with db.connect() as conn:
        p = plan(conn, from_year, to_year, resume)
    if p["reason"]:
        return {"years": [], "skipped": [], "orders": 0, "transactions": 0,
                "reason": p["reason"]}

    # ── transactions: one long call sized to the whole range ────────────────
    say(f"  transactions — one call reaching back {p['days']} days")
    txns = _with_retry(lambda: fetch.fetch_transactions(days=p["days"], session=session),
                       what="transactions", on_progress=on_progress)
    with db.connect() as conn:
        run_id = store.start_run(conn, f"backfill-txns days={p['days']}")
        n_txns = store.store_transactions(conn, txns, run_id)
        store.finish_run(conn, run_id, status="success",
                         txns_seen=len(txns), txns_upserted=n_txns)
    say(f"    stored {n_txns} charges")

    # ── orders: year by year, each its own run row and its own gate ─────────
    total_orders = 0
    done_years: list[int] = []
    for y in p["years"]:
        say(f"  {y} — fetching orders")
        try:
            orders = _with_retry(
                lambda y=y: fetch.fetch_orders(year=y, full_details=True, session=session),
                what=f"orders {y}", on_progress=on_progress)
        except AmazonAuthError:
            say(f"    ! auth/challenge on {y} — stopping. "
                f"Re-run `budget amazon backfill` to resume from here.")
            break

        with db.connect() as conn:
            expect = sync._ledger_has_amazon_charges(
                conn, f"{y}-01-01", f"{y}-12-31")
            try:
                # Per-year gate: a year that returns nothing while the ledger
                # shows charges IN THAT YEAR is a broken parse, not a quiet year.
                store.assert_not_vacuous(conn, orders=len(orders), txns=0,
                                         scope_has_known_charges=expect)
            except store.SyncAborted as e:
                say(f"    ! {y} aborted: {e}")
                continue
            rid = store.start_run(conn, f"year={y}")
            n = store.store_orders(conn, orders, rid)
            store.finish_run(conn, rid, status="success",
                             orders_seen=len(orders), orders_upserted=n)
        total_orders += n
        done_years.append(y)
        say(f"    stored {n} orders")

    # ── one match pass over everything now present ──────────────────────────
    say("  matching")
    with db.connect() as conn:
        result = match.run(conn)
        cov = match.coverage(conn)
        hz = match.horizon(conn)

    return {"years": done_years, "skipped": p["skipped"], "orders": total_orders,
            "transactions": n_txns, "matched": result["matched"],
            "ambiguous": len(result["ambiguous"]), "coverage": cov,
            "horizon": hz, "reason": None}
