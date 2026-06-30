"""HTTP API routes (FastAPI APIRouter) — extracted from server.create_app()'s
closure (prospector F-4) so each handler is a module-level, individually-readable
function. server.create_app() mounts these via include_router(api); the auth
middleware, /health, the static mount, and serve() stay in server.py.

The browser receives NO raw PII — only sanitized merchant_norm, category totals,
amounts and dates.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import budgets as budgets_mod
from .. import db, detect, reconcile, reports
from ..money import cents_from_amount_str


def _checked(month: str | None) -> str | None:
    """Validate a timeframe scope at the web boundary → HTTP 400 on a malformed
    value (parity with the POST handlers' 400-on-bad-input). reports.validate_scope
    raises a plain ValueError; we map it here so reports.py stays framework-free."""
    try:
        reports.validate_scope(month)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return month


api = APIRouter()


@api.get("/api/categories")
def category_list() -> list[str]:
    from .. import categories as cats
    return sorted(cats.spend_categories())

@api.get("/api/months")
def months() -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(posted_date,1,7) AS m FROM transactions "
            "WHERE status='posted' ORDER BY m DESC"
        ).fetchall()
    return [r["m"] for r in rows]

@api.get("/api/report")
def report(month: str | None = None) -> dict:
    return reports.month_summary(_checked(month))

@api.get("/api/insights")
def insights(month: str | None = None) -> list[dict]:
    return reports.insights(_checked(month))

# ── intake (drop-folder) ─────────────────────────────────────────────────
@api.get("/api/inbox")
def inbox() -> dict:
    from .. import intake
    return intake.pending()   # counts only — no filenames/rows (PII)

@api.post("/api/upload")
async def upload(request: Request, name: str = "") -> dict:
    """Receive an export file from the dashboard and stage it into the inbox.
    Raw body (no multipart dep); `name` is sanitized to a basename. Size is
    capped before AND after the read; type/path validated. The browser never
    gets the stored filename back (PII boundary) — only a queued count. The
    caller then POSTs /api/intake/run to import it."""
    from .. import inbox_adapter
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > inbox_adapter.MAX_FILE_BYTES:
        raise HTTPException(413, "file too large")
    # Stream with a running cap so a chunked / Content-Length-absent body can't
    # buffer unbounded in RAM before the size check (red-team Minor).
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > inbox_adapter.MAX_FILE_BYTES:
            raise HTTPException(413, "file too large")
    data = bytes(buf)
    try:
        inbox_adapter.stage_upload(name, data)
    except inbox_adapter.UploadError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "queued": 1}

@api.post("/api/intake/run")
def intake_run() -> dict:
    from .. import intake
    return intake.run_intake()   # zero-network: import + offline categorize

@api.post("/api/intake/undo")
def intake_undo() -> dict:
    from .. import intake
    return intake.undo_last_import()

@api.get("/api/conflicts")
def conflicts() -> list[dict]:
    """Open conflicts — sanitized (merchant_norm joined from the txn, no raw payee)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.conflict_id, c.kind, c.existing_amount_cents, c.existing_posted_date, "
            "c.incoming_amount_cents, c.incoming_posted_date, "
            "COALESCE(t.merchant_norm, e.merchant_norm) AS merchant, "
            "e.merchant_norm AS existing_merchant, t.merchant_norm AS incoming_merchant "
            "FROM import_conflicts c "
            "LEFT JOIN transactions t ON t.txn_id = c.incoming_txn_id "
            "LEFT JOIN transactions e ON e.txn_id = c.existing_txn_id "
            "WHERE c.resolved = 0 ORDER BY c.conflict_id"
        ).fetchall()
    return [dict(r) for r in rows]

@api.post("/api/reconcile")
async def do_reconcile(request: Request) -> dict:
    body = await request.json()
    try:
        return reconcile.resolve(int(body["conflict_id"]), str(body["action"]))
    except (reconcile.ReconcileError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e

@api.get("/api/recurring")
def recurring() -> list[dict]:
    return detect.recurring()

@api.get("/api/anomalies")
def anomalies(sd: float = detect.ANOMALY_DEFAULT_SD) -> list[dict]:
    return detect.anomalies(sd)

@api.get("/api/limits")
def limits() -> list[dict]:
    return budgets_mod.list_limits()

@api.post("/api/limits")
async def set_limit(request: Request) -> dict:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        cat = str(body["category"])
        amt = str(body.get("amount", "")).strip()
        if amt:                       # set/replace the category envelope
            budgets_mod.set_limit(cat, cents_from_amount_str(amt))
        else:                         # empty amount clears the envelope
            budgets_mod.clear_limit(cat)
    except (budgets_mod.BudgetError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}

# ── Budgets tab (zero-based envelopes; design 2026-06-12) ─────────────────
@api.get("/api/budgets")
def budgets_overview(month: str | None = None) -> dict:
    return reports.budget_overview(_checked(month))   # categories + amounts only — no PII

@api.post("/api/budgets/income")
async def budgets_income(request: Request) -> dict:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        amt = str(body.get("amount", "")).strip()
        if amt:
            budgets_mod.set_expected_income(cents_from_amount_str(amt))
        else:
            budgets_mod.clear_expected_income()
    except (budgets_mod.BudgetError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}

@api.post("/api/budgets/suggest")
async def budgets_suggest(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as e:           # malformed JSON body → 400, not 500
        raise HTTPException(400, "invalid JSON body") from e
    m = body.get("month") if isinstance(body, dict) else None
    month = str(m) if m else None   # coerce to str (or None) — non-str month must not 500
    n = reports.apply_suggestions(month)
    return {"set": n, "overview": reports.budget_overview(month)}

@api.post("/api/budgets/setup")
async def budgets_setup(request: Request) -> dict:
    """First-run wizard save: set monthly income + every category limit in one
    request and mark setup complete. Body: {income: str, limits: {category: amount}}.

    ALL inputs are validated (amount parse + positivity + spend-category rule —
    the same contract as set_limit) BEFORE any write, so a hostile or malformed
    body returns 400 with nothing persisted (no half-applied budget, never 500)."""
    from .. import categories as cats
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(400, "invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    limits = body.get("limits", {})
    if not isinstance(limits, dict):
        raise HTTPException(400, "limits must be an object")
    income_raw = str(body.get("income", "") or "").strip()
    try:
        # A zero-based SETUP requires a positive monthly income (it is the
        # denominator for "give every dollar a job"). Both checks run HERE,
        # before any write — set_expected_income also rejects <=0, but it runs
        # in the apply phase outside this try, so an unchecked value would
        # surface as a 500 instead of a 400. (Clearing income is a separate
        # operation handled by POST /api/budgets/income, not by setup.)
        if not income_raw:
            raise ValueError("monthly income is required")
        income_cents = cents_from_amount_str(income_raw)
        if income_cents <= 0:
            raise ValueError("monthly income must be positive")
        parsed: list[tuple[str, int | None]] = []
        for cat, amt in limits.items():
            cat = str(cat)
            a = str(amt).strip()
            if not a:
                parsed.append((cat, None))      # blank = clear this envelope
                continue
            cents = cents_from_amount_str(a)
            if cents <= 0:
                raise ValueError("limit must be positive")
            if not cats.is_spend(cat) or cat not in cats.all_categories():
                raise ValueError(f"not a spend category: {cat!r}")
            parsed.append((cat, cents))
    except (budgets_mod.BudgetError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    # All-valid → apply (income first, then each envelope).
    budgets_mod.set_expected_income(income_cents)
    for cat, cents in parsed:
        budgets_mod.clear_limit(cat) if cents is None else budgets_mod.set_limit(cat, cents)
    db.set_setting("budget_onboarded", "1")
    return {"ok": True, "overview": reports.budget_overview(None)}

@api.post("/api/budgets/onboarded")
def budgets_onboarded() -> dict:
    """Mark budget setup as seen (the wizard's 'Skip for now' path) so it won't
    auto-open again. The user can still re-launch it from the Budgets header."""
    db.set_setting("budget_onboarded", "1")
    return {"ok": True}

# ── merchant normalization (canonical vendor identity; design 2026-06-12) ──
@api.post("/api/normalize")
def normalize_run() -> dict:
    """Apply built-in/cached brand aliases deterministically — collapse a vendor's
    many bank-statement spellings into one canonical merchant and merge orphaned
    Subscriptions sub-budgets. Counts only — no PII. Returns
    {batch_id, txns_updated, budgets_merged}."""
    from .. import normalize
    return normalize.apply_aliases()

@api.post("/api/normalize/confirm")
async def normalize_confirm(request: Request) -> dict:
    from .. import normalize
    try:
        body = await request.json()
    except ValueError as e:           # malformed/empty JSON body → 400, not 500
        raise HTTPException(400, "invalid JSON body") from e
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    # `canonical` must be a real, non-blank string — never coerce a non-string
    # (null/dict/list/int) into a truthy garbage str that would persist as a
    # bogus canonical_merchant. Reject before confirm()'s empty-guard runs.
    canonical = body.get("canonical")
    if not isinstance(canonical, str) or not canonical.strip():
        raise HTTPException(400, "canonical must be a non-empty string")
    members = body.get("members", [])
    # `members` must be a list — guard the silent `list("ABC")` -> ['A','B','C'] trap.
    if not isinstance(members, list):
        raise HTTPException(400, "members must be a list")
    # ...and a list of strings only — make the endpoint contract explicit and
    # uniform (belt-and-suspenders with confirm()'s own element filter).
    if any(not isinstance(m, str) for m in members):
        raise HTTPException(400, "members must be a list of strings")
    try:
        return normalize.confirm(canonical, members)
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e

@api.post("/api/normalize/undo")
def normalize_undo() -> dict:
    from .. import normalize
    return normalize.undo_last()

@api.get("/api/merchants")
def merchants(limit: int = 60, only_uncertain: bool = False) -> list[dict]:
    from ..categorize import manual
    return manual.top_merchants(limit=limit, only_uncertain=only_uncertain)

@api.post("/api/merchant-category")
async def set_merchant_cat(request: Request) -> dict:
    from ..categorize import manual
    body = await request.json()
    try:
        n = manual.set_merchant_category(str(body["merchant"]), str(body["category"]))
    except (manual.CategorizeError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {"updated": n}

@api.post("/api/add-category")
async def add_category(request: Request) -> dict:
    from .. import categories as cats
    body = await request.json()
    try:
        name = cats.add_custom_category(str(body["name"]))
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {"name": name, "categories": sorted(cats.spend_categories())}

@api.post("/api/remove-category")
async def remove_category(request: Request) -> dict:
    """Remove a spend category by merging it into another (txns + rules + budgets
    re-pointed, then hidden). Body: {name, merge_into}."""
    from .. import categories as cats
    from ..categorize import manual
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    try:
        result = manual.remove_category(str(body["name"]), str(body["merge_into"]))
    except (manual.CategorizeError, ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {**result, "categories": sorted(cats.spend_categories())}

@api.get("/api/subcategories")
def subcategories(category: str, month: str | None = None) -> list[dict]:
    return reports.subcategory_breakdown(category, _checked(month))

@api.post("/api/split-subscriptions")
def split_subscriptions() -> dict:
    from ..categorize import manual
    return {"split": manual.split_subscriptions()}

@api.post("/api/subcategory-budget")
async def subcategory_budget(request: Request) -> dict:
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected a JSON object")
        amt = str(body.get("amount", "")).strip()
        if amt:
            budgets_mod.set_limit(str(body["category"]),
                                  cents_from_amount_str(amt), subcategory=str(body["subcategory"]))
        else:
            budgets_mod.clear_limit(str(body["category"]), subcategory=str(body["subcategory"]))
    except (budgets_mod.BudgetError, KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}

@api.post("/api/rename-subcategory")
async def rename_subcategory(request: Request) -> dict:
    from ..categorize import manual
    body = await request.json()
    try:
        n = manual.rename_subcategory(str(body["category"]), str(body["old"]), str(body.get("new", "")))
    except KeyError as e:
        raise HTTPException(400, str(e)) from e
    return {"updated": n}

@api.get("/api/transactions")
def transactions(category: str, month: str | None = None) -> list[dict]:
    """Posted rows in a spend category for the scope (sanitized columns only).

    Spend-category allow-list (defense-in-depth for direct callers): Income /
    Transfer / Uncategorized never appear as clickable bars, so a request for
    a non-spend category is invalid → 400.
    """
    from .. import categories as cats
    if not cats.is_spend(category):
        raise HTTPException(400, "not a spend category")
    return reports.transactions_in_category(category, _checked(month))

@api.get("/api/income-sources")
def income_sources(month: str | None = None) -> list[dict]:
    """Income grouped by normalized source for the scope (top-6 + 'Other sources'
    fold). Display-only fold flagged with `other:true`; totals sum to income_cents."""
    return reports.income_by_source(_checked(month))

@api.get("/api/income-transactions")
def income_transactions(source: str, month: str | None = None) -> list[dict]:
    """Posted income rows whose normalized source key == `source` (sanitized cols).

    No allow-list: any `source` is safe — a match returns income rows, an
    unmatched source returns []. The synthetic fold ('Other sources') is
    display-only and never reaches this endpoint (no data-src in the UI)."""
    return reports.income_transactions(source, _checked(month))
