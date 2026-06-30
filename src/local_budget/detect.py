"""Recurring-charge and anomaly detection (design §4.5).

Pure functions over row lists (dicts with posted_date, amount_cents,
merchant_norm, category) so both the CLI (budget.db posted rows) and the agent
tools (agent.db txn rows) can reuse them. Computed on read — no stored state.
"""
from __future__ import annotations


from . import categories, db

# Recurring tuning (OQ2).
RECUR_MIN_MONTHS = 3        # appears in at least this many distinct months
RECUR_MIN_COVERAGE = 0.5   # in ≥ this fraction of the months in its active span
RECUR_MAX_FREQ = 1.6       # ≤ this many charges/active-month → clearly periodic
RECUR_STABLE_FREQ = 2.5    # up to this freq IF the amount is very stable
RECUR_STABLE_AMOUNTS = 4   # "very stable" = ≤ this many distinct amounts

ANOMALY_DEFAULT_SD = 2.0
ANOMALY_MIN_SAMPLES = 3


def _spend_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if categories.is_spend(r.get("category")) and r["amount_cents"] < 0]


def _month_index(iso: str) -> int:
    y, m = int(iso[:4]), int(iso[5:7])
    return y * 12 + (m - 1)


def find_recurring(rows: list[dict]) -> list[dict]:
    """Recurring bills/subscriptions: a merchant that recurs across most months of
    its active span at a roughly-periodic cadence.

    Uses month-COVERAGE (appears in ≥half the months between first and last seen)
    + cadence (not high-frequency retail) + amount stability — robust to bills
    whose amount varies (utilities) and to 2-charges-in-a-month (e.g. two 529
    contributions), which the old strict-interval rule missed.
    """
    by_merchant: dict[str, list[dict]] = {}
    for r in _spend_rows(rows):
        by_merchant.setdefault(r["merchant_norm"], []).append(r)

    out: list[dict] = []
    for merchant, items in by_merchant.items():
        months = {_month_index(r["posted_date"]) for r in items}
        if len(months) < RECUR_MIN_MONTHS:
            continue
        span = max(months) - min(months) + 1
        coverage = len(months) / span
        freq = len(items) / len(months)
        distinct_amounts = len({r["amount_cents"] for r in items})
        is_periodic = (
            coverage >= RECUR_MIN_COVERAGE
            and (freq <= RECUR_MAX_FREQ
                 or (freq <= RECUR_STABLE_FREQ and distinct_amounts <= RECUR_STABLE_AMOUNTS))
        )
        if not is_periodic:
            continue
        amounts = [abs(r["amount_cents"]) for r in items]
        items_sorted = sorted(items, key=lambda r: r["posted_date"])
        out.append({
            "merchant": merchant,
            "occurrences": len(items),
            "months": len(months),
            "avg_amount_cents": int(round(sum(amounts) / len(amounts))),
            "stable": distinct_amounts <= RECUR_STABLE_AMOUNTS,
            "last_date": items_sorted[-1]["posted_date"],
        })
    return sorted(out, key=lambda d: d["months"], reverse=True)


def find_anomalies(rows: list[dict], sd_threshold: float = ANOMALY_DEFAULT_SD) -> list[dict]:
    """Transactions far above their merchant's baseline.

    Uses a LEAVE-ONE-OUT baseline (mean + N·sd of the OTHER charges for that
    merchant) so a single large spike is measured against normal history rather
    than inflating its own threshold. Needs ≥3 other samples (≥4 total).
    """
    by_merchant: dict[str, list[dict]] = {}
    for r in _spend_rows(rows):
        by_merchant.setdefault(r["merchant_norm"], []).append(r)

    out: list[dict] = []
    for merchant, items in by_merchant.items():
        if len(items) <= ANOMALY_MIN_SAMPLES:   # need ≥3 others -> ≥4 total
            continue
        for r in items:
            others = [abs(o["amount_cents"]) for o in items if o is not r]
            mean = sum(others) / len(others)
            sd = (sum((a - mean) ** 2 for a in others) / len(others)) ** 0.5
            cutoff = mean + sd_threshold * sd
            if abs(r["amount_cents"]) > cutoff:
                out.append({
                    "merchant": merchant,
                    "posted_date": r["posted_date"],
                    "amount_cents": r["amount_cents"],
                    "merchant_mean_cents": int(round(mean)),
                    "sd_threshold": sd_threshold,
                })
    return sorted(out, key=lambda d: abs(d["amount_cents"]), reverse=True)


# ── CLI helpers: load posted rows from budget.db ─────────────────────────────
def posted_rows() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT posted_date, amount_cents, merchant_norm, category "
            "FROM transactions WHERE status = 'posted'"
        ).fetchall()
    return [dict(r) for r in rows]


def recurring() -> list[dict]:
    return find_recurring(posted_rows())


def anomalies(sd_threshold: float = ANOMALY_DEFAULT_SD) -> list[dict]:
    return find_anomalies(posted_rows(), sd_threshold)
