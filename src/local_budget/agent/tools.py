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
            return _err(str(e))
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


def _txn_table(rows: list[dict]) -> str:
    disp = [{"Date": r["posted_date"], "Amount": render.money(int(r["amount_cents"])),
             "Category": r.get("category") or "—", "Merchant": r.get("merchant_norm") or "—",
             "Acct": r.get("account_last4") or "—", "Type": r.get("txn_type") or "—"} for r in rows]
    return render.table(disp, [("Date", "Date"), ("Amount", "Amount"), ("Category", "Category"),
                               ("Merchant", "Merchant"), ("Acct", "Acct"), ("Type", "Type")])


# ── read tools ───────────────────────────────────────────────────────────────
@_with_ro_conn
async def get_month_summary(args: dict, conn) -> dict:
    month = _month_or_current(args.get("month"))
    rows = _rows(conn, "SELECT category, SUM(amount_cents) AS total FROM transactions "
                       "WHERE status = 'posted' AND posted_date LIKE ? GROUP BY category",
                 (f"{month}-%",))
    conflicts = _conflicts_for(conn, month)
    uncategorized = _uncategorized_for(conn, month)
    by_cat = {r["category"] or "Uncategorized": int(r["total"] or 0) for r in rows}
    spend = {c: -t for c, t in by_cat.items() if categories.is_spend(c)}
    spend_total = sum(spend.values())
    income = by_cat.get(categories.INCOME, 0)
    data = {
        "month": month, "spend_total_cents": spend_total,
        "spend_by_category": dict(sorted(spend.items(), key=lambda kv: kv[1], reverse=True)),
        "income_cents": income, "transfer_cents": by_cat.get(categories.TRANSFER, 0),
        "unresolved_conflicts": conflicts, "uncategorized_spend": uncategorized,
    }
    lines = [f"## {month}",
             f"Spent **{render.money(spend_total)}** · Income **{render.money(income)}** · "
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
    lines += _flag_lines(conflicts, uncategorized)
    return {"data": data, "rendered": "\n".join(lines)}


@_with_ro_conn
async def get_category_breakdown(args: dict, conn) -> dict:
    month = _month_or_current(args.get("month"))
    rows = _rows(conn, "SELECT category, SUM(-amount_cents) AS spent, COUNT(*) AS n "
                       "FROM transactions WHERE status = 'posted' AND posted_date LIKE ? "
                       "GROUP BY category ORDER BY spent DESC", (f"{month}-%",))
    conflicts = _conflicts_for(conn, month)
    breakdown = [r for r in rows if categories.is_spend(r["category"])]
    disp = [{"Category": r["category"], "Spent": render.money(int(r["spent"])), "#": r["n"]}
            for r in breakdown]
    rendered = "\n".join([f"## {month} — by category",
                          render.table(disp, [("Category", "Category"), ("Spent", "Spent"), ("#", "#")],
                                       numbered=True,
                                       drill_hint="Reply with a row number to drill into that category's transaction list."),
                          *_flag_lines(conflicts)])
    return {"data": {"month": month, "breakdown": breakdown, "unresolved_conflicts": conflicts},
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
    sql = ("SELECT t.posted_date, t.amount_cents, t.category, t.merchant_norm, "
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

    def spend(month: str) -> int:
        rows = _rows(conn, "SELECT category, SUM(-amount_cents) AS s FROM transactions "
                           "WHERE status = 'posted' AND posted_date LIKE ? AND amount_cents < 0 "
                           "GROUP BY category", (f"{month}-%",))
        return sum(int(r["s"]) for r in rows if categories.is_spend(r["category"]))

    sa, sb = spend(a), spend(b)
    data = {"month_a": a, "spend_a_cents": sa, "month_b": b, "spend_b_cents": sb,
            "delta_cents": sa - sb,
            "unresolved_conflicts": {"a": _conflicts_for(conn, a), "b": _conflicts_for(conn, b)}}
    rendered = (f"**{a}** {render.money(sa)} vs **{b}** {render.money(sb)} — "
                f"delta **{render.money(sa - sb)}**")
    return {"data": data, "rendered": rendered}


@_with_ro_conn
async def recurring_charges(_args: dict, conn) -> dict:
    rows = _rows(conn, "SELECT posted_date, amount_cents, merchant_norm, category "
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
    rows = _rows(conn, "SELECT posted_date, amount_cents, merchant_norm, category "
                       "FROM transactions WHERE status = 'posted'")
    found = detect.find_anomalies(rows, sd)
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
    except sqlite3.Error:
        return _err("query failed (rejected or invalid)")
    # Redaction-on-read: a free-form SELECT can surface raw payee/memo, so every
    # string cell passes through the account-number redactor (design §3 — closes
    # the largest read-side leak). Non-str values pass through unchanged.
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
                                     args.get("subcategory"), conn=conn)
    return {"ok": True, "rendered": f"✓ pinned {args['merchant_norm']} → {args['category']} "
                                    f"({n} transaction(s) + a rule)"}


@_with_rw_conn
async def set_txn_category(args: dict, conn) -> dict:
    manual.set_transaction_category(int(args["txn_id"]), args["category"],
                                    args.get("subcategory"), conn=conn)
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


# ── Phase-4 read tools (back the skills; {data, rendered}) ────────────────────
async def budget_overview(args: dict) -> dict:
    data = reports.budget_overview(args.get("month"))
    rows = [{"Category": ("⚠ " if c["over"] else "") + c["category"],
             "Spent": render.money(c["spent_cents"]),
             "Budget": render.money(c["budget_cents"]) if c["budget_cents"] is not None else "—",
             "%": f"{c['pct']}%" if c["pct"] is not None else "—"}
            for c in data["categories"]]
    rendered = "## Budget overview\n" + render.table(
        rows, [("Category", "Category"), ("Spent", "Spent"), ("Budget", "Budget"), ("%", "% used")])
    return {"data": data, "rendered": rendered}


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
    rows = [{"Subcategory": r["subcategory"], "Spent": render.money(r["spent_cents"])} for r in data]
    rendered = f"## {args['category']} — by subcategory\n" + render.table(
        rows, [("Subcategory", "Subcategory"), ("Spent", "Spent")])
    return {"data": {"subcategories": data}, "rendered": rendered}


async def insights(args: dict) -> dict:
    data = reports.insights(args.get("month"))
    lines = ["## Ways to save"]
    lines += [f"- {i['label']}: {render.money(i['amount_cents'])}" for i in data]
    if len(lines) == 1:
        lines.append("- (nothing obvious flagged)")
    return {"data": {"insights": data}, "rendered": "\n".join(lines)}


@_with_ro_conn
async def monthly_trend(args: dict, conn) -> dict:
    data = reports.monthly_trend(conn, int(args.get("limit") or 24))
    rows = [{"Month": r["month"], "Spent": render.money(r["spend_cents"]),
             "Income": render.money(r["income_cents"])} for r in data]
    rendered = "## Monthly trend\n" + render.table(
        rows, [("Month", "Month"), ("Spent", "Spent"), ("Income", "Income")])
    return {"data": {"trend": data}, "rendered": rendered}


async def review_queue(_args: dict) -> dict:
    merchants = manual.needs_review()
    checks = manual.checks_to_review()
    m_rows = [{"Merchant": r["merchant"], "#": r["count"], "Spent": render.money(r["spent_cents"])}
              for r in merchants]
    c_rows = [{"Date": r["posted_date"], "Amount": render.money(r["amount_cents"]),
               "Merchant": r["merchant_norm"]} for r in checks]
    parts = [
        "## Uncategorized merchants",
        render.table(m_rows, [("Merchant", "Merchant"), ("#", "#"), ("Spent", "Spent")],
                     numbered=True,
                     drill_hint="Reply with a row number to categorize that merchant.") if m_rows else "(none)",
        "\n## Checks to review",
        render.table(c_rows, [("Date", "Date"), ("Amount", "Amount"), ("Merchant", "Merchant")],
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
             "Compare spend between two months (YYYY-MM each). Returns each total and the delta.",
             _obj({"month_a": {"type": "string"}, "month_b": {"type": "string"}}, ["month_a", "month_b"]),
             compare_periods),
    ToolSpec("recurring_charges", "Detected recurring/subscription charges (near-monthly, stable amount).",
             _obj(), recurring_charges),
    ToolSpec("find_anomalies", "Transactions far above their merchant's historical mean (default 2 sd).",
             _obj({"sd_threshold": {"type": "number"}}), find_anomalies),
    ToolSpec("run_sql",
             "Run a read-only SELECT/WITH query against the `transactions` table (columns: "
             "posted_date, amount_cents, status, category, subcategory, category_source, "
             "merchant_norm, txn_type, txn_id, account_id). Rows of ALL statuses are visible — "
             "add `WHERE status='posted'` to match the spend tools. No writes, no ATTACH; PII "
             "columns (raw_ofx, acct_hash) are blocked by the authorizer.",
             _obj({"query": {"type": "string"}}, ["query"]), run_sql),
    ToolSpec("save_user_note", "Save a NEW durable user preference (one sentence). Not financial data.",
             _obj({"note": {"type": "string"}}, ["note"]), save_user_note),
    ToolSpec("list_user_notes", "List saved user-preference notes.", _obj(), list_user_notes),
    ToolSpec("delete_user_note", "Delete the note at the given line index.",
             _obj({"line": {"type": "integer"}}, ["line"]), delete_user_note),
    # ── write tools ──
    ToolSpec("set_merchant_category",
             "Pin a merchant (merchant_norm substring) to a category (+ optional subcategory): "
             "adds a rule and recategorizes that merchant's existing transactions.",
             _obj({"merchant_norm": {"type": "string"}, "category": {"type": "string"},
                   "subcategory": {"type": "string"}}, ["merchant_norm", "category"]),
             set_merchant_category),
    ToolSpec("set_txn_category", "Categorize a SINGLE transaction by txn_id (no rule).",
             _obj({"txn_id": {"type": "integer"}, "category": {"type": "string"},
                   "subcategory": {"type": "string"}}, ["txn_id", "category"]),
             set_txn_category),
    ToolSpec("add_custom_category", "Add a user-defined spend category.",
             _obj({"name": {"type": "string"}}, ["name"]), add_custom_category),
    ToolSpec("remove_category", "Remove a spend category by MERGING it into another (re-points its "
             "transactions/rules/budgets, then hides it).",
             _obj({"name": {"type": "string"}, "merge_into": {"type": "string"}},
                  ["name", "merge_into"]), remove_category),
    ToolSpec("set_budget_limit", "Set a monthly budget limit (cents) for a category or "
             "(category, subcategory).",
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
    ToolSpec("budget_overview", "Spend vs budget per category for a month (over-budget flagged).",
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
             "discretionary, subscriptions).", _obj({"month": {"type": "string"}}), insights),
    ToolSpec("monthly_trend", "Spend + income per month (most recent N, oldest-first).",
             _obj({"limit": {"type": "integer"}}), monthly_trend),
    ToolSpec("review_queue", "The categorization review queue: uncategorized merchants + "
             "individual checks to review.", _obj(), review_queue),
    ToolSpec("open_conflicts", "Open (unresolved) import conflicts to reconcile "
             "(advisory; resolve via the CLI).", _obj(), open_conflicts),
]

SPEC_BY_NAME: dict[str, ToolSpec] = {s.name: s for s in TOOL_SPECS}
