"""Deterministic spending reports (design §4.3 / I11 / I11b).

All aggregates run over budget.db and count ONLY `status='posted'` rows —
quarantined near-duplicates (`status='conflict'`) are excluded until resolved.
Every total that excludes conflict rows is accompanied by the excluded-conflict
count/sum so the headline number is NEVER silently understated (I11b).

Spend total = sum of the spend-category totals only; `Transfer` and `Income`
are accounted separately (M5). A spend category that's also floor-marked
(e.g. `Investments` — see `categories.mark_floor_category`) is reported as
SAVINGS instead of spend: `categories.is_savings` excludes it from
`spend_total_cents`/`spend_by_category` and sums it into
`savings_total_cents`/`savings_by_category`, mirroring how `Transfer` is a
sibling bucket to spend rather than part of it.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date

from . import categories, db


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


ALL = "all"


def _months_ago(n: int) -> str:
    """YYYY-MM that is (n-1) months before the current month (inclusive window)."""
    total = date.today().year * 12 + (date.today().month - 1) - (n - 1)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _full_months_window(n: int = 3) -> tuple[str, str]:
    """(start, end) YYYY-MM for the last `n` FULL (completed) calendar months,
    EXCLUDING the current in-progress month — so a partial current month can't drag
    a budget/salary suggestion down. E.g. today 2026-06 -> ('2026-03', '2026-05')."""
    base = date.today().year * 12 + (date.today().month - 1)   # current month index
    start, end = base - n, base - 1
    return (f"{start // 12:04d}-{start % 12 + 1:02d}",
            f"{end // 12:04d}-{end % 12 + 1:02d}")


def _is_timeframe(month: str | None) -> bool:
    return bool(month) and (month == ALL or month.startswith("last"))


# Valid timeframe scope: all-time, a `lastN` window (N=1..999), or a `YYYY-MM`
# month. `re.fullmatch` (not `$`) so a trailing newline can't slip past the gate.
_SCOPE_RE = re.compile(r"all|last[1-9]\d{0,2}|\d{4}-(0[1-9]|1[0-2])")


def validate_scope(month: str | None) -> None:
    """Raise ValueError on a malformed timeframe scope (e.g. 'lastABC', '2025-13',
    'last1\\n'). The scope-following GET endpoints call this and map the ValueError
    to HTTP 400 — reports.py stays framework-free (no fastapi import). `_scope`
    itself stays lenient so non-GET callers (the suggest POST, the CLI) keep their
    coerce-to-empty behavior unchanged."""
    m = month or ALL
    if not _SCOPE_RE.fullmatch(m):
        raise ValueError(f"invalid timeframe scope: {month!r}")


def _scope(month: str) -> tuple[str, tuple]:
    """WHERE fragment + params for a single month ('YYYY-MM'), a trailing window
    ('last1'|'last3'|'last6'|'last12'), or all-time ('all'). Lenient — an
    unrecognized month yields an empty-result `LIKE` fragment (callers that need
    strict 400 validation call `validate_scope` first)."""
    if not month or month == ALL:
        return "", ()
    if month.startswith("last"):
        return " AND substr(posted_date,1,7) >= ?", (_months_ago(int(month[4:])),)
    return " AND posted_date LIKE ?", (f"{month}-%",)


# ── Budgets-tab window (design 2026-06-13: total-for-period) ───────────────────
# Budgets are monthly; the tab reviews COMPLETED months. Unlike `_scope` (open-ended,
# includes the live month), the Budgets tab resolves a CLOSED [start, end] window of
# whole calendar months and a whole-month `factor`, with the in-progress current month
# excluded from ranges (D6). A single concrete `YYYY-MM` is exactly that month (factor 1)
# and may be the live partial month when explicitly chosen.
MAX_BUDGET_SPAN = 600   # defensive clamp so one poisoned `posted_date` MIN can't blow up factor


def _month_index(m: str) -> int:
    """'YYYY-MM' -> absolute month index (year*12 + month-1)."""
    return int(m[:4]) * 12 + (int(m[5:7]) - 1)


def _index_to_month(i: int) -> str:
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def _first_data_month(conn: sqlite3.Connection) -> str | None:
    r = conn.execute(
        "SELECT MIN(substr(posted_date,1,7)) AS m FROM transactions WHERE status='posted'"
    ).fetchone()
    return r["m"] if r and r["m"] else None


def _budget_window(conn: sqlite3.Connection, month: str) -> tuple[str, str, int]:
    """(start, end, factor) of whole calendar months for the Budgets tab.
    - single 'YYYY-MM' -> (m, m, 1) — exact, may be the live partial month.
    - 'lastN'          -> (current-N, current-1, N) — N COMPLETED months, live month excluded.
    - 'all'            -> (max(first_data, current-MAX_SPAN), current-1, count).
    No completed months yet -> (current, current, 1) so the tab still renders."""
    cur = current_month()
    cur_i = _month_index(cur)
    if month and month != ALL and not month.startswith("last"):
        return month, month, 1                      # single concrete month
    last_completed = cur_i - 1                       # exclude the in-progress month (D6)
    if month and month.startswith("last"):
        start_i = cur_i - int(month[4:])
    else:                                            # all
        fdm = _first_data_month(conn)
        start_i = max(_month_index(fdm) if fdm else last_completed, cur_i - MAX_BUDGET_SPAN)
    if last_completed < start_i:                     # no completed month -> show current month
        return cur, cur, 1
    return _index_to_month(start_i), _index_to_month(last_completed), last_completed - start_i + 1


def _budget_scope(conn: sqlite3.Connection, month: str) -> tuple[str, tuple, int]:
    """Closed-window WHERE fragment + params + factor for the Budgets tab."""
    start, end, factor = _budget_window(conn, month)
    return " AND substr(posted_date,1,7) BETWEEN ? AND ?", (start, end), factor


# ── per-active-month income averaging ────────────────────────────────────────
# The Budgets tab's income reality-check is a MONTHLY figure (compared to the monthly
# expected income), so it's averaged over the income months in scope. Budget spend is
# NOT averaged — it's a total-for-period vs `monthly × factor` (see `_budget_window`).
def _active_income_months(conn: sqlite3.Connection, month: str) -> int:
    """Distinct months in scope with income (the income average divisor)."""
    frag, params = _scope(month)
    r = conn.execute(
        f"SELECT COUNT(DISTINCT substr(posted_date,1,7)) AS m FROM transactions "
        f"WHERE status='posted'{frag} AND category=?", (*params, categories.INCOME)).fetchone()
    return max(1, int(r["m"] or 0))


def _avg_per_active_month(total: int, n: int) -> int:
    """Round `total` to a per-active-month average (n months; never divide by 0)."""
    return round(total / max(1, n))


def _posted_by_category(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    """Signed cent totals per category for posted rows in scope."""
    frag, params = _scope(month)
    rows = conn.execute(
        f"SELECT category, SUM(amount_cents) AS total FROM transactions "
        f"WHERE status = 'posted'{frag} GROUP BY category",
        params,
    ).fetchall()
    return {(r["category"] or categories.UNCATEGORIZED): int(r["total"] or 0) for r in rows}


def unresolved_conflicts(conn: sqlite3.Connection, month: str) -> dict:
    """Excluded-conflict count + dollar sum in scope (I11b)."""
    frag, params = _scope(month)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(ABS(amount_cents)),0) AS total "
        f"FROM transactions WHERE status = 'conflict'{frag}",
        params,
    ).fetchone()
    return {"count": int(row["n"]), "total_cents": int(row["total"])}


def uncategorized_spend(conn: sqlite3.Connection, month: str) -> dict:
    """Uncategorized real spend count + dollar sum in scope (S1)."""
    frag, params = _scope(month)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(-amount_cents),0) AS total "
        f"FROM transactions WHERE status = 'posted'{frag} "
        f"AND category = ? AND amount_cents < 0",
        (*params, categories.UNCATEGORIZED),
    ).fetchone()
    return {"count": int(row["n"]), "total_cents": int(row["total"])}


def monthly_trend(conn: sqlite3.Connection, limit: int = 24) -> list[dict]:
    """Spend AND income per month (most recent `limit` months), oldest-first.

    "Spend" excludes the structural categories AND floor-marked savings
    categories (e.g. Investments) — same definition as `_month_summary`'s
    `spend_total_cents`, kept in sync via `categories.NON_SPEND_CATEGORIES`/
    `floor_categories()` rather than a second hardcoded name list."""
    exclude = categories.NON_SPEND_CATEGORIES | categories.floor_categories(conn=conn)
    placeholders = ",".join("?" for _ in exclude)
    rows = conn.execute(
        f"SELECT substr(posted_date,1,7) AS month, "
        f"SUM(CASE WHEN amount_cents < 0 "
        f"    AND category NOT IN ({placeholders}) "
        f"    THEN -amount_cents ELSE 0 END) AS spent, "
        f"SUM(CASE WHEN category = ? THEN amount_cents ELSE 0 END) AS income "
        f"FROM transactions WHERE status = 'posted' "
        f"GROUP BY month ORDER BY month DESC LIMIT ?",
        (*exclude, categories.INCOME, limit),
    ).fetchall()
    return [{"month": r["month"], "spend_cents": int(r["spent"] or 0),
             "income_cents": int(r["income"] or 0)} for r in reversed(rows)]


def month_summary(month: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    """Full month report: per-spend-category totals, spend total, income,
    transfer, top merchants, MoM delta, and the excluded-conflict line (I11b)."""
    month = month or current_month()
    if conn is not None:
        return _month_summary(conn, month)
    with db.connect() as c:
        return _month_summary(c, month)


def _month_summary(conn: sqlite3.Connection, month: str) -> dict:
    by_cat = _posted_by_category(conn, month)
    floor_set = categories.floor_categories(conn=conn)

    spend_by_category = {
        cat: -total  # outflow magnitude as positive dollars
        for cat, total in by_cat.items()
        if categories.is_spend(cat) and not categories.is_floor(cat, floor_set=floor_set)
    }
    savings_by_category = {
        cat: -total
        for cat, total in by_cat.items()
        if categories.is_savings(cat, floor_set=floor_set)
    }
    spend_total = sum(spend_by_category.values())
    savings_total = sum(savings_by_category.values())
    income_cents = by_cat.get(categories.INCOME, 0)
    transfer_cents = by_cat.get(categories.TRANSFER, 0)

    # Month-over-month only meaningful for a single month; skip for windows/all.
    if _is_timeframe(month):
        prev, prev_spend = None, 0
    else:
        prev = prev_month(month)
        prev_spend = sum(
            -t for c, t in _posted_by_category(conn, prev).items()
            if categories.is_spend(c) and not categories.is_floor(c, floor_set=floor_set)
        )

    return {
        "month": month,
        "spend_total_cents": spend_total,
        "spend_by_category": dict(sorted(spend_by_category.items(),
                                         key=lambda kv: kv[1], reverse=True)),
        "savings_total_cents": savings_total,
        "savings_by_category": dict(sorted(savings_by_category.items(),
                                           key=lambda kv: kv[1], reverse=True)),
        "income_cents": income_cents,
        "transfer_cents": transfer_cents,
        "prev_month": prev,
        "prev_spend_total_cents": prev_spend,
        "mom_delta_cents": spend_total - prev_spend,
        "top_merchants": top_merchants(conn, month),
        "unresolved_conflicts": unresolved_conflicts(conn, month),
        "uncategorized_spend": uncategorized_spend(conn, month),
        "budgets": budget_status(conn, month),
        "trend": monthly_trend(conn),
    }


# Categories you can realistically cut (vs fixed: Housing/Utilities/Insurance/etc.).
DISCRETIONARY = frozenset({
    "Dining Out", "Entertainment", "Shopping", "Subscriptions",
    "Personal Care", "Travel", "Gifts & Donations",
})


def _scope_month_count(conn: sqlite3.Connection, month: str) -> int:
    frag, params = _scope(month)
    r = conn.execute(
        f"SELECT COUNT(DISTINCT substr(posted_date,1,7)) AS n "
        f"FROM transactions WHERE status='posted'{frag}", params).fetchone()
    return max(1, int(r["n"] or 1))


def insights(month: str | None = None) -> list[dict]:
    """Actionable 'ways to save', ranked: over-budget alerts first, then the
    biggest discretionary categories, then subscriptions and the top merchant in
    each. Deterministic + fast (no LLM). Each item carries hard numbers."""
    month = month or current_month()
    with db.connect() as conn:
        s = _month_summary(conn, month)
        nmonths = _scope_month_count(conn, month)
        subs = subcategory_breakdown("Subscriptions", month) if "Subscriptions" in s["spend_by_category"] else []
        floor_set = categories.floor_categories(conn=conn)
    spend = s["spend_by_category"]
    out: list[dict] = []

    # 1) Over-budget / under-target — you set the limit and missed it (most urgent).
    for b in s["budgets"]:
        if b["over_cents"] > 0:
            label = f"{b['category']} / {b['subcategory']}" if b["subcategory"] else b["category"]
            kind = categories.off_track_label(b["category"], floor_set=floor_set,
                                              under="under_target", over="over_budget")
            out.append({"kind": kind, "label": label, "amount_cents": b["over_cents"],
                        "actual_cents": b["actual_cents"], "limit_cents": b["limit_cents"]})

    # 2) Biggest discretionary categories — where you can actually cut.
    disc = sorted(((c, v) for c, v in spend.items() if c in DISCRETIONARY), key=lambda x: -x[1])
    for c, v in disc[:3]:
        out.append({"kind": "reduce", "label": c, "amount_cents": v,
                    "monthly_cents": v // nmonths})

    # 3) Subscriptions — easy cancels.
    if subs:
        monthly = sum(x["monthly_avg_cents"] for x in subs)
        out.append({"kind": "subscriptions", "label": f"{len(subs)} subscriptions",
                    "amount_cents": monthly, "count": len(subs)})

    return out


def top_merchants(conn: sqlite3.Connection, month: str, limit: int = 8) -> list[dict]:
    # Group by the canonical vendor (falls back to merchant_norm when unset) so a
    # vendor's spelling-variants collapse into one row (merchant normalization).
    frag, params = _scope(month)
    rows = conn.execute(
        f"SELECT COALESCE(canonical_merchant, merchant_norm) AS merchant, "
        f"SUM(-amount_cents) AS spent, COUNT(*) AS n "
        f"FROM transactions WHERE status = 'posted'{frag} "
        f"AND amount_cents < 0 GROUP BY merchant ORDER BY spent DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [{"merchant": r["merchant"], "spent_cents": int(r["spent"]), "count": int(r["n"])}
            for r in rows]


def budget_status(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Active monthly limit (× period factor) vs un-averaged actual for each
    (category, subcategory|None), over the Budgets-tab COMPLETED-months window
    (design 2026-06-13). Shares `_budget_scope`/`_period_factor` with `budget_overview`
    so the Overview "over budget" insight and the Budgets tab cannot diverge in a scope."""
    from . import budgets as budgets_mod
    frag, params, factor = _budget_scope(conn, month)
    floor_set = categories.floor_categories(conn=conn)
    out = []
    for (category, subcategory), limit_cents in budgets_mod.active_limits(conn).items():   # current limit, all scopes
        if subcategory is None:
            row = conn.execute(
                f"SELECT COALESCE(SUM(-amount_cents),0) AS s FROM transactions "
                f"WHERE status='posted'{frag} AND category=?",
                (*params, category)).fetchone()
        else:
            row = conn.execute(
                f"SELECT COALESCE(SUM(-amount_cents),0) AS s FROM transactions "
                f"WHERE status='posted'{frag} AND category=? AND subcategory=?",
                (*params, category, subcategory)).fetchone()
        actual = int(row["s"])                          # un-averaged NET window total (matches Overview)
        period_limit = int(limit_cents) * factor        # monthly × whole-month factor
        out.append({
            "category": category,
            "subcategory": subcategory,
            "limit_cents": period_limit,
            "actual_cents": actual,
            # Direction-aware, sign-normalized: positive always means "bad" —
            # over the limit for a ceiling category, under it for a floor one
            # (e.g. Investments). Direction is a property of `category`, not
            # `subcategory` (it cascades to all subcategories underneath).
            "over_cents": categories.off_track_delta(category, actual, period_limit, floor_set=floor_set),
        })
    return sorted(out, key=lambda d: (d["category"], d["subcategory"] or ""))


def transactions_in_category(category: str, month: str | None = None) -> list[dict]:
    """All posted rows in `category` for the scope, most-recent first.

    Returns the same sanitized shape the browser already sees
    (`merchant_norm, amount_cents, posted_date`) — never raw payee/PII. Rows are
    signed (charges negative, refunds positive), with NO amount-sign filter, so
    the rows' signed `SUM(amount_cents)` equals `-spend_by_category[cat]` — the
    drill-down nets to the same magnitude as the "Where your money goes" bar it
    opened from (an outflow-only filter would over-state any category with a
    refund). Caller (server) restricts `category` to spend categories.
    """
    month = month or current_month()
    with db.connect() as conn:
        frag, params = _scope(month)
        rows = conn.execute(
            f"SELECT merchant_norm, amount_cents, posted_date FROM transactions "
            f"WHERE status='posted' AND category = ?{frag} "
            f"ORDER BY posted_date DESC",
            (category, *params),
        ).fetchall()
    return [dict(r) for r in rows]


# ── income sources ("Where your money comes from") ───────────────────────────
# Income payees fragment the same employer across several merchant_norm strings
# (GoodLeap appears as 4, Optum as 2). income_source_key collapses them with no
# user config. It operates on the STORED merchant_norm (brackets already stripped
# by sanitize._NONWORD before storage), never the raw payee.
#
# Only "REDACTED" is a junk member — never "[REDACTED]": a split token (step 2)
# can never contain '['/']' since merchant_norm is already bracket-free.
_INCOME_JUNK = frozenset({"REDACTED"})
_INCOME_SUFFIX = frozenset({
    "PAYROLL", "DIR", "DIRECT", "DEP", "DEPOSIT", "DD",
    "ACH", "LLC", "PAYMENT", "PAYMENTS", "TREAS",
})
INCOME_TOP_N = 6                 # top-6 real sources render as bars; the rest fold
INCOME_OTHER_LABEL = "Other sources"  # synthetic fold row's display label (cosmetic; clients key off `other`)


def income_source_key(merchant_norm: str) -> str:
    """Canonical income-source key derived from the stored `merchant_norm`.

    Output is **alphanumerics + spaces only** (step 2 splits on `[^A-Za-z0-9]+`,
    dropping `& ' . < > "` etc.), so it can never carry an HTML metacharacter.
    Reserved key `"Unknown"` for the literal `"UNKNOWN"` merchant_norm or any input
    that leaves no tokens.
    """
    s = (merchant_norm or "").strip()
    if s.upper() == "UNKNOWN":                              # step 1: literal-UNKNOWN guard
        return "Unknown"
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", s) if t]  # step 2: split on non-alphanumerics
    keep: list[str] = []
    for t in tokens:                                        # steps 3-4: drop junk + suffix tokens
        u = t.upper()
        if u.isdigit() or len(u) == 1 or u in _INCOME_JUNK or u in _INCOME_SUFFIX:
            continue
        keep.append(u)
    collapsed: list[str] = []                               # step 5: collapse consecutive dupes
    for t in keep:
        if not collapsed or collapsed[-1] != t:
            collapsed.append(t)
    if not collapsed:                                       # step 6 fallback: nothing survived
        return "Unknown"
    return " ".join(collapsed[:2]).title()                  # step 6: first 2 tokens, Title-Cased


def _income_rows(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Posted income rows in scope (sanitized columns only)."""
    frag, params = _scope(month)
    rows = conn.execute(
        f"SELECT merchant_norm, amount_cents, posted_date FROM transactions "
        f"WHERE status='posted' AND category=?{frag} ORDER BY posted_date DESC",
        (categories.INCOME, *params),
    ).fetchall()
    return [dict(r) for r in rows]


def income_by_source(month: str | None = None,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    """Income grouped by normalized source for the scope, biggest first.

    Returns the top `INCOME_TOP_N` real sources as `{source, total_cents, count,
    other: False}`; the long tail PLUS any non-positive-net source (defensive
    guard, no live trigger) fold into a single synthetic
    `{source: "Other sources", total_cents, count, other: True}` row appended
    last. The group totals (incl. the fold) sum to `income_cents` — folding (not
    dropping) the tail preserves that invariant.
    """
    if conn is None:
        with db.connect() as c:
            return income_by_source(month, c)
    month = month or current_month()
    agg: dict[str, list[int]] = {}                          # source -> [total_cents, count]
    for r in _income_rows(conn, month):
        a = agg.setdefault(income_source_key(r["merchant_norm"]), [0, 0])
        a[0] += int(r["amount_cents"])
        a[1] += 1
    ranked = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)
    top = [(s, tc, n) for s, (tc, n) in ranked if tc > 0][:INCOME_TOP_N]
    top_keys = {s for s, _, _ in top}
    out = [{"source": s, "total_cents": tc, "count": n, "other": False} for s, tc, n in top]
    fold_total = sum(tc for s, (tc, n) in ranked if s not in top_keys)
    fold_count = sum(n for s, (tc, n) in ranked if s not in top_keys)
    if fold_count:                                          # synthetic fold row (display-only flag)
        out.append({"source": INCOME_OTHER_LABEL, "total_cents": fold_total,
                    "count": fold_count, "other": True})
    return out


def income_transactions(source: str, month: str | None = None,
                        conn: sqlite3.Connection | None = None) -> list[dict]:
    """All posted income rows whose normalized source key == `source`, recent first.

    No allow-list: any `source` is safe — a match returns sanitized income rows,
    an unmatched/bogus source returns `[]`. Sanitized columns only.
    """
    if conn is None:
        with db.connect() as c:
            return income_transactions(source, month, c)
    month = month or current_month()
    return [r for r in _income_rows(conn, month)
            if income_source_key(r["merchant_norm"]) == source]


def _avg3_by_category(conn: sqlite3.Connection) -> dict[str, int]:
    """Average monthly spend per category over the last 3 FULL months (cents), for
    budget suggestions. The current in-progress month is excluded so a partial month
    can't understate the recommendation; the average divides by the number of full
    months that have data (so a newer category isn't diluted by empty months)."""
    start, end = _full_months_window(3)
    rows = conn.execute(
        "SELECT category, SUM(-amount_cents) AS spent, "
        "COUNT(DISTINCT substr(posted_date,1,7)) AS m FROM transactions "
        "WHERE status='posted' AND amount_cents<0 AND substr(posted_date,1,7) BETWEEN ? AND ? "
        "GROUP BY category", (start, end)).fetchall()
    return {r["category"]: round(int(r["spent"]) / max(1, int(r["m"]))) for r in rows}


def _avg3_by_subcategory(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    start, end = _full_months_window(3)
    rows = conn.execute(
        "SELECT category, subcategory, SUM(-amount_cents) AS spent, "
        "COUNT(DISTINCT substr(posted_date,1,7)) AS m FROM transactions "
        "WHERE status='posted' AND amount_cents<0 AND subcategory IS NOT NULL "
        "AND substr(posted_date,1,7) BETWEEN ? AND ? GROUP BY category, subcategory",
        (start, end)).fetchall()
    return {(r["category"], r["subcategory"]): round(int(r["spent"]) / max(1, int(r["m"]))) for r in rows}


def _avg3_income(conn: sqlite3.Connection) -> int:
    """Average monthly income (salary) over the last 3 FULL months (cents) — the
    suggested 'expected monthly income' for zero-based allocation. Excludes the
    current partial month; divides by the number of full months that had income."""
    start, end = _full_months_window(3)
    r = conn.execute(
        "SELECT COALESCE(SUM(amount_cents),0) AS total, "
        "COUNT(DISTINCT substr(posted_date,1,7)) AS m FROM transactions "
        "WHERE status='posted' AND category = ? AND amount_cents > 0 "
        "AND substr(posted_date,1,7) BETWEEN ? AND ?",
        (categories.INCOME, start, end)).fetchone()
    return round(int(r["total"]) / max(1, int(r["m"])))


def _pct(spent: int, budget: int | None) -> int | None:
    if not budget or budget <= 0:
        return None
    return round(spent / budget * 100)


def budget_overview(month: str | None = None) -> dict:
    """Consolidated Budgets-tab payload (design 2026-06-12): zero-based rollup +
    per-category envelopes with optional subcategory sub-envelopes. Only TOP-LEVEL
    category budgets count toward `to_allocate` (subcategories live inside them — no
    double-count). No PII — categories, subcategory names, integer cents only."""
    from . import budgets as budgets_mod
    month = month or current_month()
    with db.connect() as conn:
        # Always the CURRENT monthly limit (no point-in-time `_as_of`): the user reviews
        # any past month against the budget they have SET NOW ("how did I do in May vs my
        # budget?"), so selecting an old month never shows an empty budget (design 2026-06-13).
        active = budgets_mod.active_limits(conn)
        floor_set = categories.floor_categories(conn=conn)
        # Budget spend: un-averaged NET total over the closed completed-months window
        # (excludes the in-progress month for ranges; design 2026-06-13). NET (no
        # amount_cents<0 filter) so a refund/credit reduces the category exactly as it
        # does on the Overview tab and its drill-down — the two screens must agree.
        frag, params, factor = _budget_scope(conn, month)
        cat_rows = conn.execute(
            f"SELECT category, COALESCE(SUM(-amount_cents),0) AS s FROM transactions "
            f"WHERE status='posted'{frag} GROUP BY category", params).fetchall()
        cat_spend = {r["category"]: int(r["s"]) for r in cat_rows}
        sub_rows = conn.execute(
            f"SELECT category, subcategory, COALESCE(SUM(-amount_cents),0) AS s "
            f"FROM transactions WHERE status='posted'{frag} "
            f"AND subcategory IS NOT NULL GROUP BY category, subcategory", params).fetchall()
        sub_spend = {(r["category"], r["subcategory"]): int(r["s"]) for r in sub_rows}
        # Income reality-check stays MONTHLY (per-active-month avg over the literal
        # scope) so it compares to the monthly expected income; limits stay monthly.
        income_total = _posted_by_category(conn, month).get(categories.INCOME, 0)
        income_actual = _avg_per_active_month(int(income_total), _active_income_months(conn, month))
        avg_cat = _avg3_by_category(conn)
        avg_sub = _avg3_by_subcategory(conn)
        suggested_income = _avg3_income(conn)
        expected = budgets_mod.get_expected_income()

    # Categories to show: every spend category, plus any category that has spend or a
    # budget in scope (custom categories included; structural ones excluded).
    cats = set(categories.spend_categories())
    cats |= {c for c in cat_spend if categories.is_spend(c)}
    cats |= {c for (c, s) in active if c in categories.all_categories() and categories.is_spend(c)}

    out_cats = []
    total_budgeted = 0   # Σ MONTHLY top-level limits — feeds the wizard's to_allocate (D5)
    for cat in sorted(cats):
        monthly = active.get((cat, None))
        if monthly is not None:
            total_budgeted += monthly
        budget = monthly * factor if monthly is not None else None   # PERIOD budget (exact)
        spent = int(cat_spend.get(cat, 0))                           # un-averaged window total
        # subcategories: any sub with spend OR a sub-budget in this category
        sub_keys = {s for (c, s) in active if c == cat and s is not None}
        sub_keys |= {s for (c, s) in sub_spend if c == cat}
        subs = []
        sub_total = 0   # MONTHLY (subs_exceed compares monthly — factor cancels)
        for sub in sorted(sub_keys):
            sb_m = active.get((cat, sub))
            if sb_m is not None:
                sub_total += sb_m
            sb = sb_m * factor if sb_m is not None else None
            ss = int(sub_spend.get((cat, sub), 0))
            subs.append({
                "subcategory": sub, "budget_cents": sb, "monthly_budget_cents": sb_m,
                "spent_cents": ss, "suggested_cents": int(avg_sub.get((cat, sub), 0)),
                # Direction-aware (floor categories flip the comparison). `floor_set`
                # was fetched once above (while the connection was still open) so
                # this per-row check never re-reads the DB.
                "over": sb is not None and categories.is_off_track(cat, ss, sb, floor_set=floor_set),
                "over_cents": categories.off_track_delta(cat, ss, sb, floor_set=floor_set) if sb is not None else None,
                "pct": _pct(ss, sb),
                # Lets a downstream chart-builder key off this field directly instead
                # of relying on out-of-band memory of which categories were marked
                # floor-type via mark_floor_category (design determinism).
                "floor": categories.is_floor(cat, floor_set=floor_set),
            })
        out_cats.append({
            "category": cat, "budget_cents": budget, "monthly_budget_cents": monthly,
            "spent_cents": spent, "suggested_cents": int(avg_cat.get(cat, 0)),
            "over": budget is not None and categories.is_off_track(cat, spent, budget, floor_set=floor_set),
            "over_cents": categories.off_track_delta(cat, spent, budget, floor_set=floor_set) if budget is not None else None,
            "pct": _pct(spent, budget),
            "sub_total_cents": sub_total,
            "subs_exceed": monthly is not None and sub_total > monthly,
            "subcategories": subs,
            # See subcategory "floor" comment above — same rationale at the
            # category level.
            "floor": categories.is_floor(cat, floor_set=floor_set),
        })

    return {
        "month": month,
        "factor": factor,                                  # whole-month period multiplier (≥1)
        "expected_income_cents": expected,
        "actual_income_cents": int(income_actual),
        "suggested_income_cents": int(suggested_income),   # avg of last 3 FULL months
        "total_budgeted_cents": total_budgeted,            # MONTHLY (D5 — wizard basis)
        "to_allocate_cents": expected - total_budgeted,    # monthly income − monthly budgeted
        # First-run signal for the setup wizard: the client auto-opens it when the
        # budget is empty AND the user hasn't been through (or dismissed) setup.
        "onboarded": db.get_setting("budget_onboarded") == "1",
        "categories": out_cats,
    }


def apply_suggestions(month: str | None = None) -> int:
    """Fill every category/subcategory that has NO active budget with its 3-month
    average (rounded cents, >0). Never overwrites a user-set value. Returns the
    count set."""
    from . import budgets as budgets_mod
    month = month or current_month()
    with db.connect() as conn:
        active = budgets_mod.active_limits(conn)
        avg_cat = _avg3_by_category(conn)
        avg_sub = _avg3_by_subcategory(conn)

    n = 0
    for cat, cents in avg_cat.items():
        if cents > 0 and categories.is_spend(cat) and cat in categories.all_categories() \
                and (cat, None) not in active:
            budgets_mod.set_limit(cat, cents)
            n += 1
    for (cat, sub), cents in avg_sub.items():
        if cents > 0 and categories.is_spend(cat) and cat in categories.all_categories() \
                and (cat, sub) not in active:
            budgets_mod.set_limit(cat, cents, subcategory=sub)
            n += 1
    return n


def subcategory_breakdown(category: str, month: str | None = None) -> list[dict]:
    """Per-subcategory spend within a category, with each subcategory's budget."""
    from . import budgets as budgets_mod
    month = month or current_month()
    with db.connect() as conn:
        frag, params = _scope(month)
        rows = conn.execute(
            f"SELECT subcategory, SUM(-amount_cents) AS spent, COUNT(*) AS n, "
            f"COUNT(DISTINCT substr(posted_date,1,7)) AS months "
            f"FROM transactions WHERE status='posted'{frag} AND category=? AND amount_cents<0 "
            f"GROUP BY subcategory ORDER BY spent DESC",
            (*params, category)).fetchall()
        # Authoritative active sub-limits (one row per key, same-day-edit safe) —
        # matches the Budgets-tab path instead of an ad-hoc MAX(effective_from) query.
        limits = {sub: cents for (cat, sub), cents in budgets_mod.active_limits(conn).items()
                  if cat == category and sub is not None}
    out = []
    for r in rows:
        sub = r["subcategory"]
        spent = int(r["spent"])
        months = max(1, int(r["months"]))
        out.append({
            "subcategory": sub or "(unsplit)",
            "spent_cents": spent,
            "monthly_avg_cents": spent // months,
            "count": int(r["n"]),
            "months": int(r["months"]),
            "limit_cents": limits.get(sub),
        })
    return out
