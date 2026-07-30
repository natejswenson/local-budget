"""Budget MCP tools — ALL read `budget.db` through `db.agent_connect()`, the
connection-scoped column-level authorizer (design §1).

SDK-free: tools are a plain ``ToolSpec`` registry (``TOOL_SPECS`` / ``SPEC_BY_NAME``)
consumed by ``web/mcp_server.py``. Each read handler is a self-contained
``async def handler(args) -> {"data": ..., "rendered": "<markdown>"}``; the
``rendered`` markdown (built by ``render.py``) is what skills print verbatim.
Errors return ``{"error": "<msg>"}``. The agent reads only the posted
``transactions`` rows (matching the old sanitized projection) behind the
authorizer: imported facts are immutable, raw_ofx/acct_hash/inbox_files/
import_runs PII columns are read-denied, all writes denied. ``run_sql`` is
SELECT/WITH-only with a secondary keyword guard (the authorizer is the real
control) and scrubs exception strings of any row data (I16).
"""
from __future__ import annotations

import functools
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta

from .. import budgets as budgets_mod
from .. import categories, db, detect, notes, paths, reports, sanitize
from ..categorize import manual
from ..money import cents_from_amount_str
from . import render

SERVER_NAME = "budget"
ROW_CAP = 500

_FORBIDDEN_SQL = (
    "insert", "update", "delete", "drop", "alter", "create", "attach", "detach",
    "pragma", "vacuum", "reindex",
)


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool: a JSON-Schema ``input_schema`` + a single-arg async handler."""
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Awaitable[dict]]


def _obj(props: dict | None = None, required: list[str] | None = None) -> dict:
    """A valid JSON-Schema object (raw mcp serializes this over stdio — the SDK
    shorthand ``{"month": str}`` is NOT JSON-serializable)."""
    return {"type": "object", "properties": props or {}, "required": required or []}


def _err(msg: str) -> dict:
    return {"error": msg}


def _with_ro_conn(fn):
    """Open a fresh read-only ``agent_connect()`` over budget.db, pass it to
    ``fn(args, conn)`` (exposing a single-arg handler), and close it. The
    column-level authorizer is the isolation boundary; there is no staleness
    gate (one DB). NOT applied to run_sql/notes (self-contained)."""
    @functools.wraps(fn)
    async def wrapper(args: dict) -> dict:
        with db.agent_connect() as conn:
            return await fn(args, conn)
    return wrapper


def _with_rw_conn(fn):
    """Open ONE guarded write connection (`agent_connect(write=True)`) and thread
    it through the backing helper so the column-level authorizer is in the write
    path (design §1). The CM commits on normal exit, rolls back on error. Any
    helper exception (validation, or an authorizer abort on a denied target) is
    surfaced as ``{"error": msg}`` — a tool boundary never crashes the server."""
    @functools.wraps(fn)
    async def wrapper(args: dict) -> dict:
        try:
            with db.agent_connect(write=True) as conn:
                return await fn(args, conn)
        except Exception as e:  # noqa: BLE001 — tool boundary
            # Name the failing tool so a multi-write turn reads unambiguously.
            return _err(f"{fn.__name__} failed: {e}")
    return wrapper


def _rows(conn, sql, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _conflicts_for(conn, month: str) -> dict:
    return reports.unresolved_conflicts(conn, month)


def _uncategorized_for(conn, month: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(-amount_cents), 0) AS total FROM transactions "
        "WHERE status = 'posted' AND posted_date LIKE ? "
        "AND category = 'Uncategorized' AND amount_cents < 0",
        (f"{month}-%",),
    ).fetchone()
    return {"count": int(row["n"]), "total_cents": int(row["total"])}


def _month_or_current(month: str | None) -> str:
    return month or date.today().strftime("%Y-%m")


def _flag_lines(conflicts: dict, uncategorized: dict | None = None) -> list[str]:
    out = []
    if conflicts.get("count"):
        out.append(f"⚠ {conflicts['count']} unresolved conflict(s) "
                   f"({render.money(conflicts['total_cents'])}) excluded from totals.")
    if uncategorized and uncategorized.get("count"):
        out.append(f"⚠ {uncategorized['count']} uncategorized charge(s) "
                   f"({render.money(uncategorized['total_cents'])}) — not in a spend category yet.")
    return out


_EMPTY_DB_HINT = ("_(no transactions imported yet — run `budget intake` or "
                  "`budget import <file>` in a terminal, then ask again)_")


def _empty_db_hint(conn=None) -> list[str]:
    """One-line import pointer appended to the two entry-point read tools when
    the DB holds no posted transactions — a cold all-zero summary otherwise
    gives the user no direction. Only get_month_summary/budget_overview carry
    it (lean; every other tool stays unchanged)."""
    if conn is not None:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE status='posted'").fetchone()["n"]
    else:
        with db.agent_connect() as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE status='posted'").fetchone()["n"]
    return [] if n else ["", _EMPTY_DB_HINT]


def _txn_table(rows: list[dict]) -> str:
    # `Txn id` is the stable handle set_txn_category needs — surfacing it here
    # (and in review_queue's checks table) is what makes single-transaction
    # categorization actually reachable from a printed table.
    disp = [{"Date": r["posted_date"], "Amount": render.money(int(r["amount_cents"])),
             "Category": r.get("category") or "—", "Merchant": r.get("merchant_norm") or "—",
             "Acct": r.get("account_last4") or "—", "Type": r.get("txn_type") or "—",
             "Txn id": r.get("txn_id") or "—"} for r in rows]
    return render.table(disp, [("Date", "Date"), ("Amount", "Amount"), ("Category", "Category"),
                               ("Merchant", "Merchant"), ("Acct", "Acct"), ("Type", "Type"),
                               ("Txn id", "Txn id")])


# ── read tools ───────────────────────────────────────────────────────────────
@_with_ro_conn
async def get_month_summary(args: dict, conn) -> dict:
    month = _month_or_current(args.get("month"))
    rows = _rows(conn, "SELECT category, SUM(amount_cents) AS total FROM effective_txns "
                       "WHERE status = 'posted' AND posted_date LIKE ? GROUP BY category",
                 (f"{month}-%",))
    conflicts = _conflicts_for(conn, month)
    uncategorized = _uncategorized_for(conn, month)
    by_cat = {r["category"] or "Uncategorized": int(r["total"] or 0) for r in rows}
    floor_set = categories.floor_categories(conn=conn)
    spend = {c: -t for c, t in by_cat.items()
             if categories.is_spend(c) and not categories.is_floor(c, floor_set=floor_set)}
    savings = {c: -t for c, t in by_cat.items() if categories.is_savings(c, floor_set=floor_set)}
    spend_total = sum(spend.values())
    savings_total = sum(savings.values())
    income = by_cat.get(categories.INCOME, 0)
    data = {
        "month": month, "spend_total_cents": spend_total,
        "spend_by_category": dict(sorted(spend.items(), key=lambda kv: kv[1], reverse=True)),
        "savings_total_cents": savings_total,
        "savings_by_category": dict(sorted(savings.items(), key=lambda kv: kv[1], reverse=True)),
        "income_cents": income, "transfer_cents": by_cat.get(categories.TRANSFER, 0),
        "unresolved_conflicts": conflicts, "uncategorized_spend": uncategorized,
    }
    lines = [f"## {month}",
             f"Spent **{render.money(spend_total)}**"
             + (f" · Savings **{render.money(savings_total)}**" if savings_total else "")
             + f" · Income **{render.money(income)}** · "
             f"Net **{render.money(income - spend_total)}**", ""]
    if spend:
        pct_total = sum(abs(v) for v in spend.values()) or 1
        cat_rows = [{"Category": cat, "Spent": render.money(cents),
                     "%": f"{round(abs(cents) / pct_total * 100)}%"}
                    for cat, cents in sorted(spend.items(), key=lambda kv: kv[1], reverse=True)]
        lines += ["**Where it goes**",
                  render.table(cat_rows, [("Category", "Category"), ("Spent", "Spent"), ("%", "%")],
                               numbered=True,
                               drill_hint="Reply with a row number to see that category's transactions.")]
    if savings:
        sav_rows = [{"Category": cat, "Amount": render.money(cents)}
                    for cat, cents in sorted(savings.items(), key=lambda kv: kv[1], reverse=True)]
        lines += ["", "**Savings**",
                  render.table(sav_rows, [("Category", "Category"), ("Amount", "Amount")])]
    lines += _flag_lines(conflicts, uncategorized)
    lines += _empty_db_hint(conn)
    return {"data": data, "rendered": "\n".join(lines)}


@_with_ro_conn
async def get_category_breakdown(args: dict, conn) -> dict:
    month = _month_or_current(args.get("month"))
    rows = _rows(conn, "SELECT category, SUM(-amount_cents) AS spent, COUNT(DISTINCT txn_id) AS n "
                       "FROM effective_txns WHERE status = 'posted' AND posted_date LIKE ? "
                       "GROUP BY category ORDER BY spent DESC", (f"{month}-%",))
    conflicts = _conflicts_for(conn, month)
    floor_set = categories.floor_categories(conn=conn)
    breakdown = [r for r in rows if categories.is_spend(r["category"])
                 and not categories.is_floor(r["category"], floor_set=floor_set)]
    savings = [r for r in rows if categories.is_savings(r["category"], floor_set=floor_set)]
    disp = [{"Category": r["category"], "Spent": render.money(int(r["spent"])), "#": r["n"]}
            for r in breakdown]
    sections = [f"## {month} — by category",
               render.table(disp, [("Category", "Category"), ("Spent", "Spent"), ("#", "#")],
                             numbered=True,
                             drill_hint="Reply with a row number to drill into that category's transaction list.")]
    if savings:
        sav_disp = [{"Category": r["category"], "Amount": render.money(int(r["spent"])), "#": r["n"]}
                    for r in savings]
        sections += ["", "**Savings**",
                    render.table(sav_disp, [("Category", "Category"), ("Amount", "Amount"), ("#", "#")])]
    sections += _flag_lines(conflicts)
    rendered = "\n".join(sections)
    return {"data": {"month": month, "breakdown": breakdown, "savings": savings,
                     "unresolved_conflicts": conflicts},
            "rendered": rendered}


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string"},
        "merchant": {"type": "string", "description": "substring match on merchant_norm"},
        "month": {"type": "string", "description": "YYYY-MM; when given, days is ignored entirely (not ANDed)"},
        "days": {"type": "integer"},
        "min_amount_dollars": {"type": "number", "description": "min absolute amount"},
        "limit": {"type": "integer", "description": "default 50, max 500"},
    },
    "required": [],
}


@_with_ro_conn
async def query_transactions(args: dict, conn) -> dict:
    where, params = ["t.status = 'posted'"], []
    if args.get("category"):
        where.append("t.category = ?")
        params.append(args["category"])
    if args.get("merchant"):
        where.append("t.merchant_norm LIKE ?")
        params.append(f"%{args['merchant'].upper()}%")
    # month and days are mutually exclusive: month wins if both are given, and
    # days is skipped entirely (not ANDed in) — see design doc
    # 2026-07-05-conversational-numbered-drilldown-design.md Architecture §2.
    if args.get("month"):
        where.append("t.posted_date LIKE ?")
        params.append(f"{args['month']}-%")
    elif args.get("days"):
        where.append("t.posted_date >= ?")
        params.append((date.today() - timedelta(days=int(args["days"]))).isoformat())
    if args.get("min_amount_dollars"):
        where.append("ABS(t.amount_cents) >= ?")
        params.append(cents_from_amount_str(str(args["min_amount_dollars"])))
    limit = min(int(args.get("limit") or 50), ROW_CAP)
    sql = ("SELECT t.txn_id, t.posted_date, t.amount_cents, t.category, t.merchant_norm, "
           "a.acct_last4 AS account_last4, t.txn_type "
           "FROM transactions t JOIN accounts a ON a.account_id = t.account_id "
           "WHERE " + " AND ".join(where) + " ORDER BY t.posted_date DESC LIMIT ?")
    rows = _rows(conn, sql, (*params, limit))
    return {"data": {"rows": rows, "count": len(rows)}, "rendered": _txn_table(rows)}


@_with_ro_conn
async def top_merchants(args: dict, conn) -> dict:
    month = _month_or_current(args.get("month"))
    limit = min(int(args.get("limit") or 5), ROW_CAP)
    rows = _rows(conn, "SELECT merchant_norm, SUM(-amount_cents) AS spent, COUNT(*) AS n "
                       "FROM transactions WHERE status = 'posted' AND posted_date LIKE ? "
                       "AND amount_cents < 0 "
                       "GROUP BY merchant_norm ORDER BY spent DESC LIMIT ?", (f"{month}-%", limit))
    if not rows:
        rendered = "(no spend)"
    else:
        total = sum(abs(int(r["spent"])) for r in rows) or 1
        disp = [{"Merchant": r["merchant_norm"] or "—", "Spent": render.money(int(r["spent"])),
                 "%": f"{round(abs(int(r['spent'])) / total * 100)}%", "#": r["n"]} for r in rows]
        rendered = render.table(
            disp, [("Merchant", "Merchant"), ("Spent", "Spent"), ("%", "%"), ("#", "#")],
            numbered=True,
            drill_hint="Reply with a row number to see that merchant's transactions.")
    return {"data": {"rows": rows, "month": month}, "rendered": f"## Top merchants — {month}\n{rendered}"}


@_with_ro_conn
async def compare_periods(args: dict, conn) -> dict:
    a, b = args["month_a"], args["month_b"]

    def by_cat(month: str) -> dict[str, int]:
        rows = _rows(conn, "SELECT category, SUM(-amount_cents) AS s FROM effective_txns "
                           "WHERE status = 'posted' AND posted_date LIKE ? AND amount_cents < 0 "
                           "GROUP BY category", (f"{month}-%",))
        return {r["category"]: int(r["s"]) for r in rows if categories.is_spend(r["category"])}

    ca, cb = by_cat(a), by_cat(b)
    sa, sb = sum(ca.values()), sum(cb.values())
    # Per-category deltas so "what changed between A and B" is one call, not a
    # hand-diffed pair of breakdowns (which rule 3 forbids the model to compute).
    per_cat = sorted(
        ({"category": c, "a_cents": ca.get(c, 0), "b_cents": cb.get(c, 0),
          "delta_cents": ca.get(c, 0) - cb.get(c, 0)} for c in set(ca) | set(cb)),
        key=lambda r: abs(r["delta_cents"]), reverse=True)
    data = {"month_a": a, "spend_a_cents": sa, "month_b": b, "spend_b_cents": sb,
            "delta_cents": sa - sb, "by_category": per_cat,
            "unresolved_conflicts": {"a": _conflicts_for(conn, a), "b": _conflicts_for(conn, b)}}
    # Headline line is unchanged (byte-compat); the delta table appends below.
    lines = [f"**{a}** {render.money(sa)} vs **{b}** {render.money(sb)} — "
             f"delta **{render.money(sa - sb)}**"]
    if per_cat:
        disp = [{"Category": r["category"], a: render.money(r["a_cents"]),
                 b: render.money(r["b_cents"]), "Δ": render.money(r["delta_cents"])}
                for r in per_cat]
        lines += ["", render.table(disp, [("Category", "Category"), (a, a), (b, b), ("Δ", "Δ")])]
    return {"data": data, "rendered": "\n".join(lines)}


@_with_ro_conn
async def recurring_charges(_args: dict, conn) -> dict:
    rows = _rows(conn, "SELECT posted_date, amount_cents, merchant_norm, canonical_merchant, category "
                       "FROM transactions WHERE status = 'posted'")
    found = detect.find_recurring(rows)
    disp = [{"Merchant": r.get("merchant") or "—", "Amount": render.money(int(r["avg_amount_cents"])),
             "Months seen": r.get("months"), "Last charge": r.get("last_date")} for r in found]
    rendered = "## Recurring charges\n" + render.table(
        disp, [("Merchant", "Merchant"), ("Amount", "Avg amount"),
               ("Months seen", "Months seen"), ("Last charge", "Last charge")], numbered=True,
        drill_hint="Reply with a row number to see that merchant's transactions.")
    return {"data": {"recurring": found}, "rendered": rendered}


@_with_ro_conn
async def find_anomalies(args: dict, conn) -> dict:
    sd = float(args.get("sd_threshold") or detect.ANOMALY_DEFAULT_SD)
    rows = _rows(conn, "SELECT posted_date, amount_cents, merchant_norm, canonical_merchant, category "
                       "FROM transactions WHERE status = 'posted'")
    # Detection always runs over FULL history (per-merchant baselines need it);
    # month/limit only scope which flagged rows are returned — without them the
    # rendered block spans ~2 years, which skills then print verbatim.
    found = detect.find_anomalies(rows, sd)
    month = args.get("month")
    if month:
        found = [r for r in found if str(r.get("posted_date", "")).startswith(f"{month}-")]
    limit = args.get("limit")
    if limit:
        found = found[: max(int(limit), 0)]
    disp = [{"Date": r.get("posted_date"), "Merchant": r.get("merchant") or "—",
             "Amount": render.money(int(r["amount_cents"]))} for r in found]
    rendered = "## Unusual charges\n" + render.table(
        disp, [("Date", "Date"), ("Merchant", "Merchant"), ("Amount", "Amount")])
    return {"data": {"anomalies": found}, "rendered": rendered}


async def run_sql(args: dict) -> dict:
    q = (args.get("query") or "").strip().rstrip(";")
    lowered = q.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return _err("read-only: only SELECT/WITH queries permitted")
    tokens = set(re.split(r"[^a-z]+", lowered))
    for kw in _FORBIDDEN_SQL:
        if kw in tokens:
            return _err(f"forbidden keyword: {kw}")
    try:
        with db.agent_connect() as conn:
            cur = conn.execute(q)
            rows = [dict(r) for r in cur.fetchmany(ROW_CAP + 1)]
    except sqlite3.Error as e:
        # Classified, still row-data-free (I16): authorizer aborts and missing
        # schema names echo only identifiers the agent itself wrote — never a
        # value from the DB. Everything else stays the generic string.
        msg = str(e)
        if "prohibited" in msg:
            return _err("denied: the query reads an agent-blocked column or table "
                        "(raw_ofx, payee, memo, acct_hash are read-denied — use "
                        "merchant_norm for merchant text)")
        if msg.startswith("no such"):
            return _err(f"invalid query: {msg}")
        return _err("query failed (rejected or invalid)")
    # Defense-in-depth: payee/memo are authorizer-denied outright, and every
    # string cell still passes through the account-number redactor (design §3)
    # in case a future column carries embedded digits. Non-str values unchanged.
    rows = [{k: (sanitize.redact_account_numbers(v) if isinstance(v, str) else v)
             for k, v in r.items()} for r in rows]
    truncated = len(rows) > ROW_CAP
    shown = rows[:ROW_CAP]
    cols = list(shown[0].keys()) if shown else []
    rendered = render.table([{c: str(r.get(c)) for c in cols} for r in shown],
                            [(c, c) for c in cols]) if shown else "(no rows)"
    return {"data": {"rows": shown, "count": len(shown), "truncated": truncated}, "rendered": rendered}


# ── notes (file-backed: user_notes.md, NOT the financial DB) ──────────────────
async def save_user_note(args: dict) -> dict:
    text = (args.get("note") or "").strip()
    if not text:
        return _err("note text is required")
    n = notes.append_note(text)
    return {"saved": True, "line": n["line"], "text": n["text"]}


async def list_user_notes(_args: dict) -> dict:
    return {"notes": notes.read_notes()}


async def delete_user_note(args: dict) -> dict:
    ok = notes.delete_note(int(args["line"]))
    return {"deleted": ok} if ok else _err("no note at that line")


# ── write tools (DB writes through agent_connect(write=True); design §3) ──────
@_with_rw_conn
async def set_merchant_category(args: dict, conn) -> dict:
    n = manual.set_merchant_category(args["merchant_norm"], args["category"],
                                     args.get("subcategory"),
                                     confirm_random=bool(args.get("confirm_random", False)), conn=conn)
    return {"ok": True, "rendered": f"✓ pinned {args['merchant_norm']} → {args['category']} "
                                    f"({n} transaction(s) + a rule)"}


@_with_rw_conn
async def set_txn_category(args: dict, conn) -> dict:
    manual.set_transaction_category(int(args["txn_id"]), args["category"],
                                    args.get("subcategory"),
                                    confirm_random=bool(args.get("confirm_random", False)), conn=conn)
    return {"ok": True, "rendered": f"✓ txn {args['txn_id']} → {args['category']}"}


@_with_rw_conn
async def add_custom_category(args: dict, conn) -> dict:
    name = categories.add_custom_category(args["name"], conn=conn)
    return {"ok": True, "rendered": f"✓ added category {name}"}


@_with_rw_conn
async def remove_category(args: dict, conn) -> dict:
    r = manual.remove_category(args["name"], args["merge_into"], conn=conn)
    return {"ok": True, "data": r,
            "rendered": f"✓ merged {args['name']} → {args['merge_into']} ({r['moved_txns']} transaction(s))"}


@_with_rw_conn
async def mark_floor_category(args: dict, conn) -> dict:
    categories.mark_floor_category(args["name"], conn=conn)
    return {"ok": True, "rendered": f"✓ {args['name']} is now floor-type (more spend is good)"}


@_with_rw_conn
async def unmark_floor_category(args: dict, conn) -> dict:
    categories.unmark_floor_category(args["name"], conn=conn)
    return {"ok": True, "rendered": f"✓ {args['name']} reverted to ceiling-type (less spend is good)"}


@_with_rw_conn
async def set_budget_limit(args: dict, conn) -> dict:
    sub = args.get("subcategory")
    budgets_mod.set_limit(args["category"], int(args["amount_cents"]), sub, conn=conn)
    label = f"{args['category']}/{sub}" if sub else args["category"]
    return {"ok": True, "rendered": f"✓ budget {label} = {render.money(int(args['amount_cents']))}/mo"}


@_with_rw_conn
async def clear_budget_limit(args: dict, conn) -> dict:
    budgets_mod.clear_limit(args["category"], args.get("subcategory"), conn=conn)
    return {"ok": True, "rendered": f"✓ cleared budget for {args['category']}"}


@_with_rw_conn
async def set_expected_income(args: dict, conn) -> dict:
    budgets_mod.set_expected_income(int(args["cents"]), conn=conn)
    return {"ok": True, "rendered": f"✓ expected income = {render.money(int(args['cents']))}/mo"}


@_with_rw_conn
async def split_subscriptions(args: dict, conn) -> dict:
    n = manual.split_subscriptions(conn=conn)
    return {"ok": True, "rendered": f"✓ gave {n} subscription merchant(s) their own sub-budget"}


_PERIOD_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$|^all$|^last\d+$")


async def save_brief(args: dict) -> dict:
    """File-backed (NOT the DB, so OUTSIDE the authorizer — self-guarded). `period`
    is regex-validated and the output path is resolved-and-confined under
    briefings_dir() (design S7)."""
    period = (args.get("period") or "").strip()
    if not _PERIOD_RE.match(period):
        return _err("invalid period (use YYYY-MM, 'all', or 'lastN')")
    base = paths.briefings_dir().resolve()
    out = (base / f"{period}.md").resolve()
    if not out.is_relative_to(base):
        return _err("invalid period")
    out.write_text(args.get("markdown") or "")
    return {"ok": True, "path": out.name}


async def render_report(args: dict) -> dict:
    """Deterministic visual-report PDF (design 2026-07-11). File-backed like
    save_brief: period regex-validated, output confined under reports_dir(),
    0600. The optional narrative is HTML-escaped into a fixed slot — the
    agent's only free-text contribution to the page."""
    from ..report import render as report_render
    period = (args.get("period") or "").strip()
    if not report_render.PERIOD_RE.match(period):
        return _err("invalid period (use YYYY-MM)")
    try:
        result = report_render.render_report(period, args.get("narrative"))
    except report_render.ChromeNotFoundError as e:
        return _err(f"{e}. Fallback: the hand-authored HTML recipe in "
                    "budget-visualizer's appendix still works with any browser.")
    except Exception as e:  # noqa: BLE001 — tool boundary
        return _err(f"render_report failed: {e}")
    return {"ok": True, "path": result["path"],
            "rendered": f"✓ visual report saved to {result['path']} — "
                        "yours to open, move, or delete"}


# ── Phase-4 read tools (back the skills; {data, rendered}) ────────────────────
async def budget_overview(args: dict) -> dict:
    data = reports.budget_overview(args.get("month"))
    rows = [{"Category": ("⚠ " if c["over"] else "") + c["category"],
             "Spent": render.money(c["spent_cents"]),
             "Budget": render.money(c["budget_cents"]) if c["budget_cents"] is not None else "—",
             "%": f"{c['pct']}%" if c["pct"] is not None else "—"}
            for c in data["categories"]]
    rendered = "\n".join(["## Budget overview\n" + render.table(
        rows, [("Category", "Category"), ("Spent", "Spent"), ("Budget", "Budget"), ("%", "% used")]),
        *_empty_db_hint()])
    return {"data": data, "rendered": rendered}


@_with_ro_conn
async def amazon_breakdown(args: dict, conn) -> dict:
    """Item lines behind matched Amazon charges. Read-only for the agent: the
    `amazon_*` tables are absent from `_AGENT_WRITE_TABLES`, so they are
    imported facts on the same footing as transactions."""
    from ..connectors.amazon import match as az_match
    month = _month_or_current(args.get("month"))
    items = az_match.breakdown(conn, month)
    cov = az_match.coverage(conn, month)
    if not items:
        return {"data": {"month": month, "items": [], "coverage": cov},
                "rendered": f"## Amazon items — {month}\n\nNo matched Amazon items. "
                            "Run `budget amazon sync` to pull order detail."}
    lines = [(i, (i["unit_price_cents"] or 0) * (i["quantity"] or 1)) for i in items]
    orders = {i["order_number"] for i in items}
    rows = [{"Date": i["posted_date"],
             "Amount": render.money(line),
             "Qty": i["quantity"] or 1,
             "Item": (i["title"] or "—")[:60]}
            for i, line in lines]
    # Lead with the shape. "Break down my Amazon purchases" answered by twenty-six
    # undifferentiated rows is a list, not a breakdown — the header is what makes
    # the table readable, and it is summed here so the agent never has to.
    rendered = (
        f"## Amazon items — {month}\n\n"
        f"{len(items)} items across {len(orders)} orders · "
        f"{render.money(sum(v for _, v in lines))} in line totals\n\n"
        + render.table(rows, [("Date", "Date"), ("Amount", "Amount"),
                              ("Qty", "Qty"), ("Item", "Item")]))
    if cov["coverage_pct"] < 100:
        rendered += (f"\n\n⚠ {cov['coverage_pct']}% of Amazon spend is explained "
                     f"({render.money(cov['matched_cents'])} of "
                     f"{render.money(cov['total_cents'])}) — the rest has no item detail.")
    return {"data": {"month": month, "items": items, "coverage": cov,
                     "order_count": len(orders),
                     "line_total_cents": sum(v for _, v in lines)},
            "rendered": rendered}


@_with_ro_conn
async def amazon_coverage(args: dict, conn) -> dict:
    """The honesty check on any Amazon answer: what fraction of the dollars
    actually reconciled. Measured in dollars, not transaction count."""
    from ..connectors.amazon import match as az_match
    month = _month_or_current(args.get("month"))
    cov = az_match.coverage(conn, month)
    hz = az_match.horizon(conn)
    rendered = (f"## Amazon coverage — {month}\n\n"
                # coverage() already returns POSITIVE outflow magnitudes —
                # negating here rendered spend as a negative, i.e. as a refund.
                f"- **{cov['coverage_pct']}%** of Amazon spend has item detail\n"
                f"- {render.money(cov['matched_cents'])} of "
                f"{render.money(cov['total_cents'])}\n"
                f"- {cov['matched_txns']} of {cov['total_txns']} charges explained")
    if hz["has_backlog"]:
        # Report the window, not just the number. A low percentage reads as bad
        # data unless it says the older charges have nothing to match against.
        rendered += (f"\n- reconcilable back to **{hz['earliest']}** — "
                     f"{hz['pre_count']} older charges "
                     f"({render.money(hz['pre_cents'])}) predate any transaction "
                     f"record, so they cannot be matched (not a data problem; "
                     f"`budget amazon backfill` pulls what the source allows)")
    return {"data": {**cov, "horizon": hz}, "rendered": rendered}


@_with_ro_conn
async def walmart_breakdown(args: dict, conn) -> dict:
    """Item lines behind matched Walmart charges. Read-only for the agent: the
    `walmart_*` tables are absent from `_AGENT_WRITE_TABLES`, so they are
    imported facts on the same footing as transactions."""
    from ..connectors.walmart import match as wm_match
    month = _month_or_current(args.get("month"))
    items = wm_match.breakdown(conn, month)
    cov = wm_match.coverage(conn, month)
    if not items:
        return {"data": {"month": month, "items": [], "coverage": cov},
                "rendered": f"## Walmart items — {month}\n\nNo matched Walmart items. "
                            "Run `budget walmart sync` to pull order detail."}
    lines = [(i, (i["unit_price_cents"] or 0) * (i["quantity"] or 1)) for i in items]
    orders = {i["order_number"] for i in items}
    rows = [{"Date": i["posted_date"],
             "Amount": render.money(line),
             "Qty": i["quantity"] or 1,
             "Where": i["channel"] or "—",
             "Item": (i["title"] or "—")[:60]}
            for i, line in lines]
    # Lead with the shape. "Break down my Walmart purchases" answered by forty
    # undifferentiated rows is a list, not a breakdown — the header is what makes
    # the table readable, and it is summed here so the agent never has to.
    rendered = (
        f"## Walmart items — {month}\n\n"
        f"{len(items)} items across {len(orders)} orders · "
        f"{render.money(sum(v for _, v in lines))} in line totals\n\n"
        + render.table(rows, [("Date", "Date"), ("Amount", "Amount"),
                              ("Qty", "Qty"), ("Where", "Where"), ("Item", "Item")]))
    if cov["coverage_pct"] < 100:
        rendered += (f"\n\n⚠ {cov['coverage_pct']}% of Walmart spend is explained "
                     f"({render.money(cov['matched_cents'])} of "
                     f"{render.money(cov['total_cents'])}) — the rest has no item detail.")
    return {"data": {"month": month, "items": items, "coverage": cov,
                     "order_count": len(orders),
                     "line_total_cents": sum(v for _, v in lines)},
            "rendered": rendered}


@_with_ro_conn
async def walmart_coverage(args: dict, conn) -> dict:
    """The honesty check on any Walmart answer: what fraction of the dollars
    actually reconciled. Measured in dollars, not transaction count."""
    from ..connectors.walmart import match as wm_match
    month = _month_or_current(args.get("month"))
    cov = wm_match.coverage(conn, month)
    hz = wm_match.horizon(conn)
    rendered = (f"## Walmart coverage — {month}\n\n"
                # coverage() already returns POSITIVE outflow magnitudes —
                # negating here would render spend as a refund.
                f"- **{cov['coverage_pct']}%** of Walmart spend has item detail\n"
                f"- {render.money(cov['matched_cents'])} of "
                f"{render.money(cov['total_cents'])}\n"
                f"- {cov['matched_txns']} of {cov['total_txns']} charges explained")
    # Online and in-store are different problems with different fixes: one is a
    # question about the parser, the other about whether Walmart holds the
    # receipt at all. A single averaged number says which neither.
    for name, c in cov["channels"].items():
        if c["total_cents"]:
            rendered += (f"\n- {name}: **{c['coverage_pct']}%** "
                         f"({render.money(c['matched_cents'])} of "
                         f"{render.money(c['total_cents'])})")
    dv = cov["derived"]
    if dv["derived"]:
        rendered += (f"\n- {dv['derived']} of {dv['matched']} matched charges "
                     f"({render.money(dv['derived_cents'])}) were dated from the "
                     f"order rather than from a payment line — the items are "
                     f"real, the settle date is inferred")
    if hz["has_backlog"]:
        # Report the window, not just the number. A low percentage reads as bad
        # data unless it says the older charges have nothing to match against.
        rendered += (f"\n- reconcilable back to **{hz['earliest']}** — "
                     f"{hz['pre_count']} older charges "
                     f"({render.money(hz['pre_cents'])}) predate any order "
                     f"record, so they cannot be matched (not a data problem; "
                     f"`budget walmart backfill` pulls what the source allows)")
    return {"data": {**cov, "horizon": hz}, "rendered": rendered}


@_with_ro_conn
async def propose_split(args: dict, conn) -> dict:
    """Item lines behind a charge, scaled to what was actually charged.

    Works from an Amazon or a Walmart order, tried in turn rather than dispatched
    on the merchant string: the merchant is the bank's text, while which
    connector actually holds the order is a fact about what has been synced.

    Read-only and category-free ON PURPOSE. Amazon publishes no product category
    at all and Walmart's is a shelf label rather than a budget category, so
    assigning one is the agent's judgment — stated as such and confirmed by the
    user before `apply_split` writes anything.
    """
    from ..connectors.amazon import split as az_split
    from ..connectors.walmart import split as wm_split

    txn_id = int(args["txn_id"])
    p = source = None
    reasons = []
    for name, mod in (("amazon", az_split), ("walmart", wm_split)):
        try:
            p, source = mod.propose(conn, txn_id), name
            break
        except mod.NoOrderBehind as e:
            reasons.append(f"{name}: {e}")
        except Exception as e:
            return _err(str(e))
    if p is None:
        return _err("no reconciled order behind that charge — " + "; ".join(reasons))

    rows = [{"Ref": i.get("asin") or i.get("product_id") or "—",
             "Amount": render.money(i["suggested_cents"]),
             "Qty": i["quantity"], "Item": (i["title"] or "—")[:52]} for i in p["items"]]
    note = ("\n\n_Amounts are each item's share of the charge, scaled from a list "
            f"total of {render.money(p['item_total_cents'])}; they will not equal "
            "the sticker price._" if p["scaled"] else "")
    rendered = (f"## Proposed split — {render.money(p['charge_cents'])} "
                f"{p['txn']['merchant_norm']} (from the {source} order)\n"
                + render.table(rows, [("Ref", "Ref"), ("Amount", "Amount"),
                                      ("Qty", "Qty"), ("Item", "Item")]) + note)
    return {"data": {**p, "source": source}, "rendered": rendered}


@_with_rw_conn
async def apply_split(args: dict, conn) -> dict:
    """Write a confirmed split. Lines must sum to the charge exactly.

    The sum check is in `splits.apply`, not here — a split set that does not
    sum to its parent invents or destroys money in every total downstream, so
    it is refused at the one place every writer goes through.
    """
    from .. import splits as splits_mod
    txn_id = int(args["txn_id"])
    lines = [{"amount_cents": int(ln["amount_cents"]), "category": ln["category"],
              "subcategory": ln.get("subcategory"), "item_ref": ln.get("item_ref"),
              "note": ln.get("note")} for ln in args["lines"]]
    n = splits_mod.apply(conn, txn_id, lines, source=args.get("source") or "manual")
    parts = "\n".join(f"- {render.money(ln['amount_cents'])} → **{ln['category']}**"
                      for ln in lines)
    return {"data": {"txn_id": txn_id, "lines": n},
            "rendered": f"## Split applied — transaction {txn_id}\n\n{parts}\n\n"
                        f"Category totals move; the month's total spend does not."}


async def income_by_source(args: dict) -> dict:
    data = reports.income_by_source(args.get("month"))
    rows = [{"Source": r["source"], "Amount": render.money(r["total_cents"]), "#": r["count"]} for r in data]
    rendered = "## Income by source\n" + render.table(
        rows, [("Source", "Source"), ("Amount", "Amount"), ("#", "#")])
    return {"data": {"sources": data}, "rendered": rendered}


async def income_transactions(args: dict) -> dict:
    rows = reports.income_transactions(args["source"], args.get("month"))
    cols = list(rows[0].keys()) if rows else []
    disp = [{c: (render.money(r[c]) if c.endswith("_cents") and r[c] is not None else str(r.get(c)))
             for c in cols} for r in rows]
    rendered = render.table(disp, [(c, c) for c in cols]) if rows else "(no income transactions)"
    return {"data": {"rows": rows}, "rendered": rendered}


async def subcategory_breakdown(args: dict) -> dict:
    data = reports.subcategory_breakdown(args["category"], args.get("month"))
    # Avg/mo + Budget were computed by reports.subcategory_breakdown all along
    # but dropped at render time — they're exactly what the subscriptions skill
    # needs for price-creep and limit checks (and what the CLI already shows).
    rows = [{"Subcategory": r["subcategory"], "Spent": render.money(r["spent_cents"]),
             "Avg/mo": render.money(r["monthly_avg_cents"]),
             "Budget": render.money(r["limit_cents"]) if r["limit_cents"] is not None else "—"}
            for r in data]
    rendered = f"## {args['category']} — by subcategory\n" + render.table(
        rows, [("Subcategory", "Subcategory"), ("Spent", "Spent"),
               ("Avg/mo", "Avg/mo"), ("Budget", "Budget")])
    return {"data": {"subcategories": data}, "rendered": rendered}


async def insights(args: dict) -> dict:
    data = reports.insights(args.get("month"))
    # `under_target` (a floor category, e.g. Investments, short of its target) means
    # "add more", the opposite of every other kind here ("cut this"/"over budget"/
    # "cancel this subscription") — render it under its own heading with "short of
    # target" wording so it never reads as a cut (reports.insights() is the source
    # of truth for `kind`; see categories.off_track_label).
    save_items = [i for i in data if i["kind"] != "under_target"]
    under_items = [i for i in data if i["kind"] == "under_target"]
    lines = ["## Ways to save"]
    lines += [f"- {i['label']}: {render.money(i['amount_cents'])}" for i in save_items]
    if not save_items:
        lines.append("- (nothing obvious flagged)")
    if under_items:
        lines.append("## Under target (add more)")
        lines += [f"- {i['label']}: {render.money(i['amount_cents'])} short of target"
                  for i in under_items]
    return {"data": {"insights": data}, "rendered": "\n".join(lines)}


@_with_ro_conn
async def monthly_trend(args: dict, conn) -> dict:
    data = reports.monthly_trend(conn, int(args.get("limit") or 24))
    rows = [{"Month": r["month"], "Spent": render.money(r["spend_cents"]),
             "Income": render.money(r["income_cents"])} for r in data]
    rendered = "## Monthly trend\n" + render.table(
        rows, [("Month", "Month"), ("Spent", "Spent"), ("Income", "Income")])
    return {"data": {"trend": data}, "rendered": rendered}


async def list_categories(_args: dict) -> dict:
    """The assignable category vocabulary — every write tool validates against
    exact names from this set, so the agent needs a way to discover it instead
    of guessing (get_category_breakdown only shows categories that HAVE spend)."""
    floors = categories.floor_categories()
    customs = categories.custom_categories()
    structural = categories.STRUCTURAL_CATEGORIES

    def kind(name: str) -> str:
        return "structural" if name in structural else "spend"

    rows = [{"name": n, "kind": kind(n),
             "floor": n in floors, "custom": n in customs}
            for n in sorted(categories.all_categories())]
    disp = [{"Category": r["name"], "Kind": r["kind"],
             "Direction": "floor (more is good)" if r["floor"] else
                          ("—" if r["kind"] == "structural" else "ceiling"),
             "Custom": "yes" if r["custom"] else "—"} for r in rows]
    rendered = "## Categories\n" + render.table(
        disp, [("Category", "Category"), ("Kind", "Kind"),
               ("Direction", "Direction"), ("Custom", "Custom")])
    return {"data": {"categories": rows}, "rendered": rendered}


async def review_queue(_args: dict) -> dict:
    merchants = manual.needs_review()
    checks = manual.checks_to_review()
    m_rows = [{"Merchant": r["merchant"], "#": r["count"], "Spent": render.money(r["spent_cents"])}
              for r in merchants]
    # Txn id makes the drill hint's promise real: set_txn_category(txn_id=…)
    # needs a handle, and the checks table is where the agent learns it.
    c_rows = [{"Date": r["posted_date"], "Amount": render.money(r["amount_cents"]),
               "Merchant": r["merchant_norm"], "Txn id": r["txn_id"]} for r in checks]
    parts = [
        "## Uncategorized merchants",
        render.table(m_rows, [("Merchant", "Merchant"), ("#", "#"), ("Spent", "Spent")],
                     numbered=True,
                     drill_hint="Reply with a row number to categorize that merchant.") if m_rows else "(none)",
        "\n## Checks to review",
        render.table(c_rows, [("Date", "Date"), ("Amount", "Amount"), ("Merchant", "Merchant"),
                              ("Txn id", "Txn id")],
                     numbered=True,
                     drill_hint="Reply with a row number to categorize that transaction.") if c_rows else "(none)",
    ]
    return {"data": {"merchants": merchants, "checks": checks}, "rendered": "\n".join(parts)}


@_with_ro_conn
async def open_conflicts(_args: dict, conn) -> dict:
    # Explicit projection (NOT SELECT *) over agent_connect; incoming_payee is the
    # only payee text and is redacted on read (design S6). No existing-merchant column.
    rows = _rows(conn, "SELECT conflict_id, kind, existing_amount_cents, existing_posted_date, "
                       "incoming_amount_cents, incoming_posted_date, incoming_payee "
                       "FROM import_conflicts WHERE resolved = 0 ORDER BY conflict_id")
    for r in rows:
        r["incoming_payee"] = sanitize.redact_account_numbers(r.get("incoming_payee"))

    def side(amt, dt):
        return f"{render.money(amt)} {dt or ''}".strip() if amt is not None else "—"

    disp = [{"ID": r["conflict_id"], "Kind": r["kind"],
             "Existing": side(r["existing_amount_cents"], r["existing_posted_date"]),
             "Incoming": side(r["incoming_amount_cents"], r["incoming_posted_date"]),
             "Merchant": r["incoming_payee"] or "—"} for r in rows]
    rendered = "## Open conflicts\n" + (render.table(
        disp, [("ID", "ID"), ("Kind", "Kind"), ("Existing", "Existing"),
               ("Incoming", "Incoming"), ("Merchant", "Merchant")])
        if disp else "(none — nothing to reconcile)")
    return {"data": {"conflicts": rows}, "rendered": rendered}


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("get_month_summary",
             "Spend summary for a month (YYYY-MM, default current): spend total, "
             "per-category breakdown, income, transfers, and any unresolved conflicts. "
             "Call this first for 'how am I doing this month'.",
             _obj({"month": {"type": "string"}}), get_month_summary),
    ToolSpec("get_category_breakdown", "Per-category spend totals for a month (YYYY-MM).",
             _obj({"month": {"type": "string"}}), get_category_breakdown),
    ToolSpec("query_transactions",
             "List posted transactions with optional filters (category, merchant substring, "
             "month YYYY-MM or days lookback — month wins and days is ignored if both are given, "
             "min amount). Most recent first.",
             _QUERY_SCHEMA, query_transactions),
    ToolSpec("top_merchants", "Top merchants by spend for a month (YYYY-MM).",
             _obj({"month": {"type": "string"}, "limit": {"type": "integer"}}), top_merchants),
    ToolSpec("compare_periods",
             "Compare spend between two months (YYYY-MM each). Returns each total, the overall "
             "delta, and a per-category delta table (sorted by biggest change).",
             _obj({"month_a": {"type": "string"}, "month_b": {"type": "string"}}, ["month_a", "month_b"]),
             compare_periods),
    ToolSpec("recurring_charges", "Detected recurring/subscription charges (near-monthly, stable amount).",
             _obj(), recurring_charges),
    ToolSpec("find_anomalies",
             "Transactions far above their merchant's historical mean (default 2 sd). "
             "UNSCOPED by default — returns flags across ~2 years of history; pass month "
             "(YYYY-MM) and/or limit to scope the output (detection baselines still use "
             "full history).",
             _obj({"sd_threshold": {"type": "number"}, "month": {"type": "string"},
                   "limit": {"type": "integer"}}), find_anomalies),
    ToolSpec("run_sql",
             "Run a read-only SELECT/WITH query against the `transactions` table (columns: "
             "posted_date, amount_cents, status, category, subcategory, category_source, "
             "merchant_norm, txn_type, txn_id, account_id). Rows of ALL statuses are visible — "
             "add `WHERE status='posted'` to match the spend tools. No writes, no ATTACH; PII "
             "columns (raw_ofx, payee, memo, acct_hash) are read-blocked by the authorizer — "
             "merchant_norm is the only merchant text.",
             _obj({"query": {"type": "string"}}, ["query"]), run_sql),
    ToolSpec("save_user_note", "Save a NEW durable user preference (one sentence). Not financial data.",
             _obj({"note": {"type": "string"}}, ["note"]), save_user_note),
    ToolSpec("list_user_notes", "List saved user-preference notes.", _obj(), list_user_notes),
    ToolSpec("delete_user_note", "Delete the note at the given line index.",
             _obj({"line": {"type": "integer"}}, ["line"]), delete_user_note),
    # ── write tools ──
    ToolSpec("set_merchant_category",
             "Pin a merchant (merchant_norm substring) to a category (+ optional subcategory): "
             "adds a rule and recategorizes that merchant's existing transactions. Setting "
             "category='Random' requires confirm_random=true — it's discouraged, pick a real "
             "category or leave it in the review queue.",
             _obj({"merchant_norm": {"type": "string"}, "category": {"type": "string"},
                   "subcategory": {"type": "string"}, "confirm_random": {"type": "boolean"}},
                  ["merchant_norm", "category"]),
             set_merchant_category),
    ToolSpec("set_txn_category", "Categorize a SINGLE transaction by txn_id (no rule). Setting "
             "category='Random' requires confirm_random=true — it's discouraged, pick a real "
             "category or leave it in the review queue.",
             _obj({"txn_id": {"type": "integer"}, "category": {"type": "string"},
                   "subcategory": {"type": "string"}, "confirm_random": {"type": "boolean"}},
                  ["txn_id", "category"]),
             set_txn_category),
    ToolSpec("amazon_breakdown", "What was actually bought behind the Amazon charges in a "
             "month — item titles, quantities and line totals. Read-only; needs "
             "`budget amazon sync` to have run.",
             _obj({"month": {"type": "string"}}), amazon_breakdown),
    ToolSpec("amazon_coverage", "How much Amazon spend has item detail behind it, in DOLLARS. "
             "Check this before trusting an Amazon breakdown — a low number means most of the "
             "spend is still unexplained.",
             _obj({"month": {"type": "string"}}), amazon_coverage),
    ToolSpec("walmart_breakdown", "What was actually bought behind the Walmart charges in a "
             "month — item titles, quantities, line totals, and whether each was bought "
             "online or in store. Read-only; needs `budget walmart sync` to have run.",
             _obj({"month": {"type": "string"}}), walmart_breakdown),
    ToolSpec("walmart_coverage", "How much Walmart spend has item detail behind it, in "
             "DOLLARS, split by online vs in-store. Check this before trusting a Walmart "
             "breakdown — a low number means most of the spend is still unexplained, and "
             "in-store is usually the weaker half.",
             _obj({"month": {"type": "string"}}), walmart_coverage),
    ToolSpec("propose_split", "Item lines behind an Amazon or Walmart charge, each scaled "
             "to its share of what was actually charged. Read-only. Assign a category to "
             "every line yourself, show the user, and only then call apply_split.",
             _obj({"txn_id": {"type": "integer"}}, ["txn_id"]), propose_split),
    ToolSpec("apply_split", "Split one charge across categories. Lines MUST sum to the "
             "charge exactly or the write is refused. Confirm with the user first — a "
             "wrong allocation silently misstates a budget.",
             _obj({"txn_id": {"type": "integer"},
                   "lines": {"type": "array", "items": {"type": "object"}},
                   "source": {"type": "string"}}, ["txn_id", "lines"]), apply_split),
    ToolSpec("add_custom_category", "Add a user-defined spend category.",
             _obj({"name": {"type": "string"}}, ["name"]), add_custom_category),
    ToolSpec("remove_category", "Remove a spend category by MERGING it into another (re-points its "
             "transactions/rules/budgets, then hides it).",
             _obj({"name": {"type": "string"}, "merge_into": {"type": "string"}},
                  ["name", "merge_into"]), remove_category),
    ToolSpec("mark_floor_category", "Mark a category as floor-type: MORE spend is good (e.g. "
             "Investments), the opposite of every other (ceiling-type) category.",
             _obj({"name": {"type": "string"}}, ["name"]), mark_floor_category),
    ToolSpec("unmark_floor_category", "Revert a category to ordinary ceiling-type semantics "
             "(less spend is good).",
             _obj({"name": {"type": "string"}}, ["name"]), unmark_floor_category),
    ToolSpec("set_budget_limit", "Set a monthly budget limit (cents) for a category or "
             "(category, subcategory). Direction (over-budget-is-bad vs under-target-is-bad) "
             "comes from the category's floor/ceiling marking (see mark_floor_category), not "
             "from this call.",
             _obj({"category": {"type": "string"}, "amount_cents": {"type": "integer"},
                   "subcategory": {"type": "string"}}, ["category", "amount_cents"]),
             set_budget_limit),
    ToolSpec("clear_budget_limit", "Clear the budget limit for a category (or subcategory).",
             _obj({"category": {"type": "string"}, "subcategory": {"type": "string"}}, ["category"]),
             clear_budget_limit),
    ToolSpec("set_expected_income", "Set expected monthly income (cents).",
             _obj({"cents": {"type": "integer"}}, ["cents"]), set_expected_income),
    ToolSpec("split_subscriptions", "Give every Subscriptions merchant its own subcategory "
             "(blank ones only) so each can be budgeted individually.", _obj(), split_subscriptions),
    ToolSpec("save_brief", "Save a composed brief markdown to data/briefings/<period>.md "
             "(period = YYYY-MM | 'all' | 'lastN').",
             _obj({"period": {"type": "string"}, "markdown": {"type": "string"}},
                  ["period", "markdown"]), save_brief),
    # ── Phase-4 read tools ──
    ToolSpec("budget_overview", "Spend vs budget per category for a month (over-budget flagged; "
             "floor categories like Investments flip the comparison — under-target is flagged "
             "instead of over-budget).",
             _obj({"month": {"type": "string"}}), budget_overview),
    ToolSpec("income_by_source", "Income grouped by source for a month.",
             _obj({"month": {"type": "string"}}), income_by_source),
    ToolSpec("income_transactions", "Income transactions for a given source (+ optional month).",
             _obj({"source": {"type": "string"}, "month": {"type": "string"}}, ["source"]),
             income_transactions),
    ToolSpec("subcategory_breakdown", "Spend by subcategory within a category for a month.",
             _obj({"category": {"type": "string"}, "month": {"type": "string"}}, ["category"]),
             subcategory_breakdown),
    ToolSpec("insights", "Deterministic 'ways to save' for a month (over-budget, biggest "
             "discretionary, subscriptions; floor categories like Investments falling short "
             "of target are flagged separately as 'under target' — add more, not a cut).",
             _obj({"month": {"type": "string"}}), insights),
    ToolSpec("monthly_trend", "Spend + income per month (most recent N, oldest-first).",
             _obj({"limit": {"type": "integer"}}), monthly_trend),
    ToolSpec("render_report",
             "Render the month's visual report PDF (stat row, spend-vs-budget chart, "
             "trend, flags) deterministically to reports/budget-report-<period>.pdf. "
             "period is YYYY-MM; optional narrative is a short plain-text paragraph "
             "placed under the headline. Writes a local file — confirm with the user "
             "before calling.",
             _obj({"period": {"type": "string"}, "narrative": {"type": "string"}},
                  ["period"]), render_report),
    ToolSpec("list_categories",
             "The assignable category vocabulary: every category's exact name, kind "
             "(spend/structural), floor-vs-ceiling direction, and custom flag. Call this "
             "before any category write — set_merchant_category / set_txn_category / "
             "set_budget_limit require an EXACT name from this list.",
             _obj(), list_categories),
    ToolSpec("review_queue", "The categorization review queue: uncategorized merchants + "
             "individual checks to review.", _obj(), review_queue),
    ToolSpec("open_conflicts", "Open (unresolved) import conflicts to reconcile "
             "(advisory; resolve via the CLI).", _obj(), open_conflicts),
]

SPEC_BY_NAME: dict[str, ToolSpec] = {s.name: s for s in TOOL_SPECS}
