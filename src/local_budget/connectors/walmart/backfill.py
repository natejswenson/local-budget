"""Full history backfill.

`sync` pulls a rolling window, which explains recent months and almost nothing
else. This walks the whole range the ledger covers.

**The asymmetry that shapes everything here** is the opposite of the Amazon
connector's. There, order history is year-scoped and the expensive part is
per-order detail. Here the order LIST is cheap — one paginated page carries
many orders — and the per-order detail page is one request each. So the two are
separated: page the list to the end in one pass, then fetch detail only for the
orders that still lack it.

**Resumability falls out of the data, with no new bookkeeping.** Amazon's
backfill resumes on `amazon_sync_runs.scope` recording a finished year. This
resumes on `walmart_orders.detail_fetched`, which is simply true or false for
each order. A backfill that dies at 80% keeps everything it stored and the next
run picks up the remaining 20% — and because the list pass runs first, every
order and its total are already stored, which is all the matcher needs. So even
an interrupted backfill improves coverage rather than leaving nothing behind.
"""
from __future__ import annotations

import time

from ... import db
from . import match, store, sync
from .match import MERCHANT_LIKE
from .session import WalmartAuthError

#: Between detail fetches. Not a throttle we are forced into — a long scrape
#: that hammers a server gets blocked harder, and a backfill is not urgent.
POLITE_DELAY_SECONDS = 1.5

#: Transient-failure retry schedule, in seconds. Deliberately short and few.
BACKOFF_SECONDS = (5, 20, 60)


def earliest_charge_date(conn) -> str | None:
    """The first Walmart charge in the ledger.

    Derived from data rather than guessed, so the default scope is exactly the
    period there is something to reconcile — fetching further back would be
    requests spent on orders no bank row will ever match.
    """
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    row = conn.execute(
        f"""SELECT MIN(posted_date) AS d FROM transactions
             WHERE status='posted' AND amount_cents < 0 AND ({like})""",
        MERCHANT_LIKE).fetchone()
    return row["d"] if row and row["d"] else None


def pending_detail(conn, limit: int | None = None) -> list[str]:
    """Orders whose detail page has not been read, newest first.

    Newest first because that is where the ledger's unexplained charges
    concentrate and where an interrupted run leaves the most value behind.
    Cancelled orders are skipped: they have no items to collect and no charge to
    reconcile, so a request spent on one buys nothing.
    """
    rows = conn.execute(
        "SELECT order_number FROM walmart_orders "
        " WHERE detail_fetched = 0 AND cancelled = 0 "
        " ORDER BY order_placed_date DESC"
        + (" LIMIT ?" if limit else ""),
        (limit,) if limit else ()).fetchall()
    return [r["order_number"] for r in rows]


def plan(conn, since: str | None = None) -> dict:
    """What a backfill would do, without doing it."""
    since = since or earliest_charge_date(conn)
    if since is None:
        return {"since": None, "pending": 0, "stored": 0,
                "reason": "no Walmart charges in the ledger"}
    stored = conn.execute(
        "SELECT COUNT(*) n FROM walmart_orders").fetchone()["n"]
    return {"since": since, "pending": len(pending_detail(conn)),
            "stored": int(stored), "reason": None}


def _with_retry(fn, *, what: str, on_progress=None):
    """Retry on transient failure only.

    An auth or bot-challenge error is re-raised immediately and never retried:
    the session is not going to become valid on the third attempt, and
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
        except WalmartAuthError:
            raise
        except Exception as e:                                # noqa: BLE001
            if any(w in str(e).lower() for w in ("challenge", "captcha", "robot")):
                raise
            last = e
    raise RuntimeError(f"{what} failed after {len(BACKOFF_SECONDS)} retries: {last}")


def run_backfill(*, since: str | None = None, limit: int | None = None,
                 headless: bool = True, on_progress=None) -> dict:
    """List every order back to `since`, then fill in the detail, then match.

    The list pass comes first because it is what makes the run useful early: it
    stores every order and its total, and matching needs only the total — so
    coverage improves before a single detail page has been read. Matching runs once at the end rather than
    per order — it is global and cheap, and per-order passes would redo the same
    work N times for no benefit.
    """
    from . import fetch                       # lazy: pulls in Playwright

    def say(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    with db.connect() as conn:
        p = plan(conn, since)
    if p["reason"]:
        return {"orders": 0, "detailed": 0, "reason": p["reason"]}
    since = p["since"]

    detailed = 0
    stopped_early: str | None = None
    with fetch.browser_session(headless=headless) as f:
        # ── pass 1: the whole list, cheap ───────────────────────────────────
        say(f"  listing orders back to {since}")
        orders = _with_retry(lambda: f.order_list(since=since, on_progress=on_progress),
                             what="order list", on_progress=on_progress)
        summary = sync.store_and_match(orders, scope=f"backfill-list since={since}",
                                       since=since)
        say(f"    stored {summary['orders']} orders · "
            f"{summary['matched']} already reconcile")

        # ── pass 2: detail, one request each, resumable ─────────────────────
        with db.connect() as conn:
            todo = pending_detail(conn, limit)
        say(f"  {len(todo)} orders need detail")
        for i, num in enumerate(todo, 1):
            try:
                detail = _with_retry(lambda num=num: f.order_detail(num),
                                     what=f"order {num}", on_progress=on_progress)
            except WalmartAuthError as e:
                # Stop, keep everything already stored, and say how to resume.
                stopped_early = str(e)
                say(f"    ! session lost after {detailed} orders — "
                    f"`budget walmart login`, then re-run to resume")
                break
            with db.connect() as conn:
                run_id = store.start_run(conn, f"backfill-detail {num}")
                store.store_orders(conn, [detail], run_id)
                store.finish_run(conn, run_id, status="success",
                                 orders_seen=1, orders_upserted=1)
            detailed += 1
            if i % 10 == 0 or i == len(todo):
                say(f"    {i}/{len(todo)} detail pages")
            time.sleep(POLITE_DELAY_SECONDS)

    # ── one match pass over everything now present ──────────────────────────
    say("  matching")
    with db.connect() as conn:
        result = match.run(conn)
        cov = match.coverage(conn)
        hz = match.horizon(conn)
        remaining = len(pending_detail(conn))

    # `matched` is the TOTAL that now reconciles, not what this last pass added.
    # The list pass already matched most of it, so the final pass's own count is
    # usually near zero — reporting that as "matched N" reads as a failed run.
    return {"since": since, "orders": summary["orders"], "detailed": detailed,
            "remaining": remaining, "matched": cov["matched_txns"],
            "new_matches": result["matched"],
            "ambiguous": len(result["ambiguous"]), "coverage": cov,
            "horizon": hz, "stopped_early": stopped_early, "reason": None}
