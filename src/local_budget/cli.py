"""CLI entry point: `budget <subcommand>` (design §5)."""
from __future__ import annotations

from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from . import budgets as budgets_mod
from . import categories, db, detect, reconcile, reports
from . import splits as splits_mod
from .ingest import importer
from .money import cents_from_amount_str, dollars


@click.group()
def main() -> None:
    """Local-first bank-statement spending agent."""


@main.command()
def setup() -> None:
    """Initialize the databases (0700 dir / 0600 files) and store your name."""
    db.init_schema()
    click.echo(f"  ✓ budget.db ready at {db.get_db_path()}")
    name = click.prompt("Your name (used in reports)", default="").strip()
    if name:
        db.set_setting("user_name", name)
        click.echo(f"  ✓ saved name: {name}")
    click.echo("\nNext:\n"
               "  • budget import <statement.qfx>   – load a bank statement export\n"
               "  • budget report                   – this month's spending\n"
               "  • budget serve                    – open the local dashboard")


@main.command(name="import")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--detect-duplicates", is_flag=True,
              help="flag near-duplicate (pending→posted) charges for review — "
                   "use only for incremental re-imports of recent statements, "
                   "NOT bulk/historical imports (it over-flags recurring charges)")
def import_cmd(file: Path, detect_duplicates: bool) -> None:
    """Import a bank OFX/QFX (or CSV) statement export (rule-based categorization)."""
    db.init_schema()
    r = importer.import_file(file, detect_near_duplicates=detect_duplicates)
    line = f"  {r['inserted']} new · {r['skipped']} duplicates"
    if r["conflicts"]:
        line += f" · {r['conflicts']} conflicts (run `budget reconcile`)"
    click.echo(line)
    click.echo("  ⚠ delete the raw export file — it holds full account numbers "
               "(not retained by design)")


@main.command()
@click.option("--yes", is_flag=True, help="skip the confirmation prompt")
def reset(yes: bool) -> None:
    """Wipe imported transactions + conflicts (keeps rules, budgets, settings)."""
    db.init_schema()
    if not yes and not click.confirm(
            "Delete ALL imported transactions, conflicts, and import history? "
            "(category rules, budgets, and settings are kept)"):
        click.echo("aborted")
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM import_conflicts")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM import_runs")
    click.echo("  ✓ transactions cleared — re-import with `budget import <file>`")


@main.command()
def intake() -> None:
    """Import new bank statement exports from your inbox folder and categorize."""
    from . import inbox_adapter
    from . import intake as intake_mod
    db.init_schema()
    r = intake_mod.run_intake()
    if not r["ran"]:
        click.echo(f"  {r['reason']}")
        return
    if r["disposed"]:
        click.echo(f"  · filed {r['disposed']} previously-imported file(s) into processed/")
    click.echo(f"  ✓ imported {r['files_imported']} file(s) · {r['new_transactions']} new · "
               f"{r['deduped']} already had")
    # Surface possible double-counts so a real bank reformat-across-downloads is never
    # invisible to a CLI-only user (red-team F-1). The data is already correct (both
    # rows posted + advisory near_duplicate conflict recorded); this just makes the
    # over-count visible and fixable via `budget reconcile`.
    if r.get("possible_duplicates", 0) > 0:
        n = r["possible_duplicates"]
        click.secho(f"  ⚠ {n} possible duplicate charge(s) flagged — "
                    f"run `budget reconcile` to review", fg="yellow")
    # Surface malformed/unrecoverable rows so a permanently-dropped charge is never
    # silent (red-team F1). Good rows still imported; these specific rows could not
    # be read (bad amount/date) and were NOT imported.
    if r.get("dropped_rows", 0) > 0:
        n = r["dropped_rows"]
        click.secho(f"  ⚠ {n} transaction row(s) in your export could not be read "
                    f"(malformed) and were NOT imported — check the file", fg="yellow")
    if r.get("files_errored", 0) > 0:
        click.secho(f"  ⚠ {r['files_errored']} file(s) failed to import — will retry on "
                    f"the next intake (or were quarantined after repeated failures)",
                    fg="yellow")
    if r["files_quarantined"]:
        click.secho(f"  ⚠ {r['files_quarantined']} file(s) skipped — not a recognized bank "
                    f"statement export ({', '.join(r['quarantine_reasons'])})", fg="yellow")
    if r["files_imported"] == 0 and r["files_quarantined"] == 0:
        click.echo(f"  (nothing new in {inbox_adapter.inbox_dir()})")
    if r["needs_review"]:
        click.echo(f"  ? {r['needs_review']} merchant(s) need a category — "
                   f"run `budget review` or open the dashboard")


@main.command()
def undo() -> None:
    """Undo the most recent import (removes its transactions + rules; restores the file)."""
    from . import intake as intake_mod
    db.init_schema()
    r = intake_mod.undo_last_import()
    if not r["undone"]:
        click.echo(f"  {r['reason']}")
        return
    click.echo(f"  ✓ undid import #{r['run_id']} — {r['transactions_removed']} transactions, "
               f"{r['rules_removed']} rules removed; {r['files_restored']} file(s) restored to inbox")


@main.command()
def normalize() -> None:
    """Collapse a vendor's many bank-statement spellings into one canonical merchant
    (Anthropic, Hulu, …). Built-in/cached brand aliases apply deterministically."""
    from . import normalize as norm
    db.init_schema()
    r = norm.apply_aliases()
    click.echo(f"  ✓ tidied {r['txns_updated']} transaction(s); "
               f"{r['budgets_merged']} sub-budget(s) merged")


@main.command(name="set-inbox")
@click.argument("folder", required=False, type=click.Path())
def set_inbox(folder: str | None) -> None:
    """Show or set the folder the app watches for bank statement exports."""
    from . import inbox_adapter
    db.init_schema()
    if folder:
        db.set_setting("inbox_dir", str(Path(folder).expanduser()))
    click.echo(f"  inbox folder: {inbox_adapter.inbox_dir()}")
    click.echo("  drop bank OFX/QFX/CSV statement exports here, then run `budget intake`")
    # F-2 (deferred feature, made non-silent): CSV files are all treated as a
    # SINGLE account, so two different accounts both exported as CSV can cross-dedup.
    click.echo("  note: CSV files are treated as a SINGLE account — for multiple "
               "accounts use OFX/QFX exports (they carry real account numbers)")


@main.command()
def review() -> None:
    """Interactively categorize the merchants the AI wasn't sure about, 1 by 1."""
    from . import categories
    from .categorize import manual
    db.init_schema()
    pending = manual.needs_review()
    if not pending:
        click.echo("  ✓ nothing to review — every merchant is categorized")
        return
    cats = sorted(categories.spend_categories())
    click.echo("Categories: " + ", ".join(cats))
    click.echo("Type a category name, 'a <name>' to add a new one, 's' to skip, 'q' to quit.\n")
    for m in pending:
        while True:
            ans = click.prompt(
                f"  {m['merchant']}  ({m['count']}x, {dollars(m['spent_cents'])})", default="s"
            ).strip()
            if ans.lower() == "q":
                click.echo("  stopped.")
                return
            if ans.lower() == "s" or not ans:
                break
            if ans.lower().startswith("a "):
                ans = categories.add_custom_category(ans[2:].strip())
            confirm_random = ans == "Random" and click.confirm(
                "    Random is discouraged — pick a real category if possible. Use it anyway?",
                default=False)
            if ans == "Random" and not confirm_random:
                click.echo("    ! skipped — pick a real category, or leave it in the review queue")
                continue
            try:
                n = manual.set_merchant_category(m["merchant"], ans, confirm_random=confirm_random)
                click.echo(f"    ✓ {ans} ({n} rows)")
                break
            except (manual.CategorizeError, ValueError) as e:
                click.echo(f"    ! {e}")


@main.command(name="set-category")
@click.argument("merchant")
@click.argument("category")
@click.option("--confirm-random", is_flag=True,
              help="required to pin a merchant to the discouraged Random catch-all")
def set_category(merchant: str, category: str, confirm_random: bool) -> None:
    """Pin a merchant to a category (sticks for future imports), e.g.
    `budget set-category NETFLIX Subscriptions`."""
    from .categorize import manual
    db.init_schema()
    try:
        n = manual.set_merchant_category(merchant, category, confirm_random=confirm_random)
    except manual.CategorizeError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  ✓ {merchant} -> {category} ({n} rows)")


@main.command(name="add-category")
@click.argument("name")
def add_category(name: str) -> None:
    """Add a custom spend category, e.g. `budget add-category \"Kid Activities\"`."""
    from . import categories
    db.init_schema()
    click.echo(f"  ✓ added category: {categories.add_custom_category(name)}")


@main.command("report-pdf")
@click.argument("period")
@click.option("--no-sync", is_flag=True,
              help="skip the Amazon refresh and render from stored data")
def report_pdf(period: str, no_sync: bool) -> None:
    """Render the visual report PDF for PERIOD (YYYY-MM) — the no-MCP path to
    the same deterministic renderer the render_report tool uses.

    Refreshes Amazon item data first when it would help: current or previous
    month, a saved session, and nothing synced in the last 12 hours. The
    refresh can never fail the report — a stale scraper cookie is not a reason
    to be unable to render your month.
    """
    from .report import render as report_render
    if not no_sync:
        from .connectors.amazon import autosync
        r = autosync.maybe_sync(period)
        if r["status"] == "synced":
            click.echo(f"  ✓ amazon refreshed — {r['detail']}")
        elif r["status"] in ("no-session", "failed"):
            # Surfaced, never fatal. Silence here would let item data quietly
            # rot for months behind a report that still looks complete.
            click.echo(f"  ! amazon not refreshed — {r['detail']}")
    try:
        out = report_render.render_report(period)
    except (ValueError, report_render.ChromeNotFoundError) as e:
        raise SystemExit(f"✗ {e}") from e
    click.echo(f"  ✓ report saved to {out['path']}")


@main.command()
@click.option("--month", default=None, help="YYYY-MM (default current month)")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
def report(month: str | None, as_json: bool) -> None:
    """Month-to-date spending report."""
    db.init_schema()
    s = reports.month_summary(month)
    if as_json:
        import json
        click.echo(json.dumps(s, indent=2))
        return
    click.echo(f"\nSpending — {s['month']}")
    click.echo(f"  Spent:  {dollars(s['spend_total_cents'])}  "
               f"(prev {dollars(s['prev_spend_total_cents'])}, "
               f"Δ {dollars(s['mom_delta_cents'])})")
    click.echo(f"  Income: {dollars(s['income_cents'])}   "
               f"Transfers: {dollars(s['transfer_cents'])}")
    click.echo("\n  By category:")
    for cat, amt in s["spend_by_category"].items():
        click.echo(f"    {cat:20s} {dollars(amt):>12s}")
    if s["budgets"]:
        click.echo("\n  Budgets:")
        floor_set = categories.floor_categories()   # fetched once, not per-row
        for b in s["budgets"]:
            # Per-row: each budget dict carries its own category.
            if b["over_cents"] <= 0:
                flag = "ok"
            else:
                flag = categories.off_track_label(b["category"], floor_set=floor_set)
            click.echo(f"    {b['category']:20s} {dollars(b['actual_cents'])} / "
                       f"{dollars(b['limit_cents'])}  [{flag}]")
    c = s["unresolved_conflicts"]
    if c["count"]:
        click.secho(f"\n  ⚠ {dollars(c['total_cents'])} across {c['count']} unresolved "
                    f"conflicts excluded — run `budget reconcile`", fg="yellow")
    u = s["uncategorized_spend"]
    if u["count"]:
        click.secho(f"\n  ⚠ {dollars(u['total_cents'])} across {u['count']} uncategorized "
                    f"transactions not in the spend total — categorize them to include",
                    fg="yellow")


@main.command(name="set-limit")
@click.argument("category")
@click.argument("amount")
@click.option("--sub", default=None, help="subcategory, e.g. a subscription name (Netflix)")
def set_limit(category: str, amount: str, sub: str | None) -> None:
    """Set a monthly limit, e.g. `budget set-limit Dining 400` or
    `budget set-limit Subscriptions 16 --sub Netflix`."""
    db.init_schema()
    cents = cents_from_amount_str(amount)
    budgets_mod.set_limit(category, cents, subcategory=sub)
    label = f"{category} / {sub}" if sub else category
    click.echo(f"  ✓ {label} limit {dollars(cents)}/mo")


@main.command()
def limits() -> None:
    """List monthly category / subcategory limits."""
    db.init_schema()
    rows = budgets_mod.list_limits()
    if not rows:
        click.echo("(no limits set)")
        return
    for r in rows:
        label = f"{r['category']} / {r['subcategory']}" if r["subcategory"] else r["category"]
        click.echo(f"  {label:30s} {dollars(r['limit_cents'])}/mo")


@main.command(name="split-subscriptions")
def split_subscriptions_cmd() -> None:
    """Give each subscription its own subcategory so it can be budgeted."""
    from .categorize import manual
    db.init_schema()
    n = manual.split_subscriptions()
    click.echo(f"  ✓ split {n} subscriptions into subcategories")


@main.command()
@click.option("--month", default="all", help="YYYY-MM or 'all' (default all-time)")
def subscriptions(month: str) -> None:
    """List each subscription's monthly cost and budget."""
    from .categorize import manual
    db.init_schema()
    manual.split_subscriptions()
    rows = reports.subcategory_breakdown("Subscriptions", month)
    if not rows:
        click.echo("  no subscriptions found")
        return
    click.echo(f"\nSubscriptions ({month}):")
    # Hoisted: `subcategory_breakdown()`'s rows carry no `category` field —
    # "Subscriptions" is a fixed literal for this whole command, so `floor_set`
    # is fetched once and threaded through every per-row call below, which
    # skips the DB read entirely instead of just skipping the label choice.
    floor_set = categories.floor_categories()
    for r in rows:
        line = f"  {r['subcategory']:24s} {dollars(r['monthly_avg_cents'])}/mo"
        if r["limit_cents"]:
            off_track = categories.is_off_track("Subscriptions", r["monthly_avg_cents"], r["limit_cents"],
                                                floor_set=floor_set)
            flag = categories.off_track_label("Subscriptions", floor_set=floor_set) if off_track else "ok"
            line += f"   budget {dollars(r['limit_cents'])} [{flag}]"
        click.echo(line)
    click.echo("\n  set a budget: budget set-limit Subscriptions 16 --sub <name>")


@main.command()
def recurring() -> None:
    """Detected recurring / subscription charges."""
    db.init_schema()
    for r in detect.recurring():
        click.echo(f"  {r['merchant']:24s} ~{dollars(r['avg_amount_cents'])}/mo "
                   f"({r['occurrences']}x, last {r['last_date']})")


@main.command()
@click.option("--sd", default=detect.ANOMALY_DEFAULT_SD, help="std-dev threshold")
def anomalies(sd: float) -> None:
    """Transactions far above their merchant's usual amount."""
    db.init_schema()
    for a in detect.anomalies(sd):
        click.echo(f"  {a['posted_date']}  {a['merchant']:24s} {dollars(a['amount_cents'])} "
                   f"(usual ~{dollars(a['merchant_mean_cents'])})")


@main.command()
@click.argument("conflict_id", type=int, required=False)
@click.argument("action", required=False)
def reconcile_cmd(conflict_id: int | None, action: str | None) -> None:
    """Review/resolve import conflicts. With no args, lists open conflicts."""
    db.init_schema()
    if conflict_id is None:
        rows = reconcile.list_open()
        if not rows:
            click.echo("(no open conflicts)")
            return
        for r in rows:
            click.echo(f"  #{r['conflict_id']} {r['kind']}: "
                       f"existing {dollars(r['existing_amount_cents'] or 0)} "
                       f"vs incoming {dollars(r['incoming_amount_cents'] or 0)}")
        click.echo("\nResolve with: budget reconcile <id> "
                   "<keep_one|mark_distinct|merge|accept_incoming>")
        return
    reconcile.resolve(conflict_id, action)
    click.echo(f"  ✓ resolved #{conflict_id} ({action})")


main.add_command(reconcile_cmd, name="reconcile")


@main.command()
@click.option("--out", default=None, help="output path (must be under data/ or backup_root)")
def backup(out: str | None) -> None:
    """Export a copy of the masked budget.db (allowlisted destinations only)."""
    from . import backup as backup_mod
    db.init_schema()
    dest = backup_mod.backup(out)
    click.echo(f"  ✓ backup written to {dest}")


@main.command()
@click.option("--host", default="127.0.0.1", envvar="LOCAL_BUDGET_HOST",
              help="bind host (default 127.0.0.1 / loopback-only)")
@click.option("--port", default=8770, help="port (default 8770)")
@click.option("--open", "open_browser", is_flag=True, help="open the dashboard in your browser")
def serve(host: str, port: int, open_browser: bool) -> None:
    """Start the local web dashboard (loopback-only)."""
    from .web.server import serve as serve_app
    if open_browser:
        import threading
        import time
        import webbrowser
        threading.Thread(target=lambda: (time.sleep(1), webbrowser.open(f"http://{host}:{port}")),
                         daemon=True).start()
    click.echo(f"  dashboard → http://{host}:{port}  (ctrl-c to stop)")
    serve_app(host=host, port=port)


@main.command()
def status() -> None:
    """Show DB stats and the last import run."""
    db.init_schema()
    with db.connect() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("accounts", "transactions", "import_conflicts", "budgets")}
        last = conn.execute("SELECT * FROM import_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    click.echo(f"DB: {db.get_db_path()}")
    for k, v in counts.items():
        click.echo(f"  {k:20s} {v:>8}")
    if last:
        click.echo(f"  last import: {dict(last)['status']} "
                   f"({dict(last).get('rows_inserted')} new)")


@main.group()
def config() -> None:
    """View or set user settings."""


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    db.init_schema()
    if key == "name":
        key = "user_name"
    db.set_setting(key, value)
    click.echo(f"  ✓ {key} = {value}")


@config.command("get")
@click.argument("key", required=False)
def config_get(key: str | None) -> None:
    db.init_schema()
    if key:
        click.echo(db.get_setting("user_name" if key == "name" else key) or "(unset)")
    else:
        for k, v in db.all_settings().items():
            click.echo(f"  {k} = {v}")


# ── transaction splits ───────────────────────────────────────────────────────
def propose_from_any_connector(conn, txn_id: int) -> tuple[str, dict]:
    """The order behind a charge, from whichever connector reconciled it.

    Tried in turn rather than dispatched on the merchant string: the merchant is
    the bank's text, and which connector actually has the order is a fact about
    what has been synced. Asking is cheap and cannot be wrong; guessing from
    `WM SUPERC…` can.

    Returns ``(source, proposal)``. `source` is recorded on the split rows, so a
    later reader can tell where an apportionment came from.
    """
    from .connectors.amazon import split as az_split
    from .connectors.walmart import split as wm_split

    if conn.execute("SELECT 1 FROM transactions WHERE txn_id = ?",
                    (txn_id,)).fetchone() is None:
        raise click.ClickException(f"no transaction {txn_id}")

    reasons = []
    for source, mod in (("amazon", az_split), ("walmart", wm_split)):
        try:
            return source, mod.propose(conn, txn_id)
        except mod.NoOrderBehind as e:
            reasons.append(f"  {source}: {e}")
    raise click.ClickException(
        "no reconciled order behind that charge:\n" + "\n".join(reasons))


@main.command("split")
@click.argument("txn_id", type=int)
@click.option("--dry-run", is_flag=True, help="show the proposed allocation, write nothing")
@click.option("--category", "cats", multiple=True, metavar="ITEM_REF=CATEGORY",
              help="assign a category to an item line, by ASIN or Walmart item "
                   "number (repeatable)")
@click.option("--rest", default=None, metavar="CATEGORY",
              help="category for every line not named by --category")
def split_cmd(txn_id: int, dry_run: bool, cats: tuple[str, ...], rest: str | None) -> None:
    """Split a charge across categories using the order behind it.

    Works from an Amazon or a Walmart order, whichever reconciled to the charge.

    Item prices do not sum to what the card was charged — discounts and
    promotions land between them — so lines are scaled proportionally and every
    cent of the charge is attributed to a real item.
    """
    db.init_schema()
    with db.connect() as conn:
        source, p = propose_from_any_connector(conn, txn_id)

        assigned = dict(c.split("=", 1) for c in cats if "=" in c)
        click.echo(f"Split {dollars(p['charge_cents'])} · "
                   f"{p['txn']['merchant_norm']} · {p['txn']['posted_date']} "
                   f"· from the {source} order")
        if p["scaled"]:
            click.echo(f"  items list at {dollars(p['item_total_cents'])}; "
                       f"scaled to the {dollars(p['charge_cents'])} charged")
        lines = []
        for it in p["items"]:
            # An ASIN for Amazon, a Walmart item number for Walmart — the same
            # role in both, which is why txn_splits calls the column `item_ref`.
            ref = it.get("asin") or it.get("product_id")
            cat = assigned.get(ref or "") or rest
            click.echo(f"  {dollars(it['suggested_cents']):>10}  "
                       f"{(cat or '?'):<16}  {(it['title'] or '—')[:44]}")
            if cat:
                lines.append({"amount_cents": it["suggested_cents"], "category": cat,
                              "item_ref": ref, "note": (it["title"] or "")[:80]})

        if len(lines) != len(p["items"]):
            click.echo("\n  ! every line needs a category — use "
                       "--category ITEM_REF=Cat and/or --rest Cat")
            return
        if dry_run:
            click.echo("\n  (dry run — nothing written)")
            return
        try:
            n = splits_mod.apply(conn, txn_id, lines, source=source)
        except splits_mod.SplitError as e:
            raise click.ClickException(str(e)) from e
    click.echo(f"\n  ✓ split into {n} lines — `budget report --month "
               f"{p['txn']['posted_date'][:7]}` reflects it")


@main.command("unsplit")
@click.argument("txn_id", type=int)
def unsplit_cmd(txn_id: int) -> None:
    """Remove a transaction's splits; it reverts to its own category."""
    db.init_schema()
    with db.connect() as conn:
        n = splits_mod.unsplit(conn, txn_id)
    click.echo(f"  ✓ removed {n} split lines" if n else "  (no splits on that transaction)")


@main.command("splits")
@click.option("--month", default=None, help="YYYY-MM (default: all)")
def splits_cmd(month: str | None) -> None:
    """Every split transaction and its parts — so a total that moved is traceable."""
    db.init_schema()
    with db.connect() as conn:
        rows = splits_mod.list_split_txns(conn, month)
    if not rows:
        click.echo("  no split transactions")
        return
    for r in rows:
        click.echo(f"\n{r['posted_date']}  {dollars(r['amount_cents'])}  "
                   f"{r['merchant_norm']}  (was {r['original_category']})")
        for s in r["splits"]:
            click.echo(f"    {dollars(s['amount_cents']):>10}  {s['category']:<18} "
                       f"{(s['note'] or '')[:40]}")


@main.command("verify-splits")
def verify_splits_cmd() -> None:
    """Audit the invariant: every split set sums to its transaction."""
    db.init_schema()
    with db.connect() as conn:
        bad = splits_mod.verify(conn)
        total = conn.execute("SELECT COUNT(DISTINCT txn_id) n FROM txn_splits").fetchone()["n"]
    if not bad:
        click.echo(f"  ✓ {total} split transaction(s), all summing to their parent")
        return
    click.echo(f"  ✗ {len(bad)} transaction(s) whose splits do NOT sum to the charge:")
    for b in bad:
        click.echo(f"    txn {b['txn_id']}  {b['posted_date']}  "
                   f"charge {dollars(b['txn_cents'])} vs splits "
                   f"{dollars(b['split_cents'])}  (drift {dollars(b['drift_cents'])})")
    raise SystemExit(1)


# ── amazon connector ─────────────────────────────────────────────────────────
@main.group()
def amazon() -> None:
    """Pull Amazon order + item detail and reconcile it against the ledger.

    Amazon publishes no consumer order API, so this signs in to your account
    and parses the consumer site. Your data, your account — but Amazon's terms
    prohibit automated extraction, and a page redesign can break it. Set
    AMAZON_USERNAME / AMAZON_PASSWORD (and AMAZON_OTP_SECRET_KEY, which is what
    makes sync unattended) in .env.
    """


@amazon.command("login")
@click.option("--password", "use_password", is_flag=True,
              help="force the AMAZON_USERNAME/PASSWORD flow instead of a browser")
@click.option("--timeout", default=300, show_default=True,
              help="seconds to wait for you to finish signing in")
def amazon_login(use_password: bool, timeout: int) -> None:
    """Sign in and cache the session (0600, under data/amazon/).

    Defaults to opening a real browser window, which is the only thing that
    works for a passkey account — there is no replayable secret in a passkey,
    so we capture the resulting session instead of storing a credential. Falls
    back to the password flow only if you ask for it explicitly.
    """
    from .connectors.amazon import browser_login, session as az_session
    db.init_schema()
    try:
        if use_password:
            az_session.build_session(force_login=True)
            click.echo(f"  ✓ signed in — session cached at {az_session.cookie_path()}")
        else:
            r = browser_login.login(timeout=timeout, echo=click.echo)
            click.echo(f"  ✓ session cached at {r['path']} (0600)")
            click.echo("    now run: budget amazon sync --days 60")
    except Exception as e:
        raise click.ClickException(str(e)) from e


@amazon.command("sync")
@click.option("--days", type=int, default=365, show_default=True,
              help="how far back to pull")
@click.option("--year", type=int, default=None,
              help="pull one calendar year instead of a rolling window")
def amazon_sync(days: int, year: int | None) -> None:
    """Fetch orders + charges, store them, and match them to transactions."""
    from .connectors.amazon import sync as az_sync
    db.init_schema()
    try:
        r = az_sync.run_sync(days=None if year else days, year=year)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    cov = r["coverage"]
    click.echo(f"  ✓ {r['orders']} orders · {r['transactions']} Amazon charges")
    click.echo(f"    matched {r['matched']} "
               f"({r['exact']} exact, {r['windowed']} windowed)"
               + (f" · {r['ambiguous']} need confirming" if r["ambiguous"] else ""))
    click.echo(f"    coverage {cov['coverage_pct']}% of Amazon spend "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")


@amazon.command("backfill")
@click.option("--from", "from_year", type=int, default=None,
              help="first year (default: earliest Amazon charge in the ledger)")
@click.option("--to", "to_year", type=int, default=None, help="last year")
@click.option("--no-resume", is_flag=True, help="re-fetch years already recorded")
@click.option("--dry-run", is_flag=True, help="show what would be fetched")
def amazon_backfill(from_year: int | None, to_year: int | None,
                    no_resume: bool, dry_run: bool) -> None:
    """Pull order history across years, not just the rolling sync window.

    A long job — full order detail is one request per order — so it is
    resumable: each completed year is recorded, and re-running skips it.
    """
    from .connectors.amazon import backfill as az_backfill
    db.init_schema()
    with db.connect() as conn:
        p = az_backfill.plan(conn, from_year, to_year, not no_resume)
    if p["reason"]:
        click.echo(f"  {p['reason']}")
        return
    click.echo(f"  years to fetch: {p['years'] or '(none — all done)'}")
    if p["skipped"]:
        click.echo(f"  already done:   {p['skipped']}  (--no-resume to redo)")
    click.echo(f"  transactions:   one call reaching back {p['days']} days")
    if dry_run:
        click.echo("\n  (dry run — nothing fetched)")
        return
    if not p["years"]:
        click.echo("  nothing to do")
        return
    try:
        r = az_backfill.run_backfill(from_year=from_year, to_year=to_year,
                                     resume=not no_resume, on_progress=click.echo)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    cov, hz = r["coverage"], r["horizon"]
    click.echo(f"\n  ✓ {r['orders']} orders across {r['years']} · "
               f"{r['transactions']} charges")
    click.echo(f"    matched {r['matched']}"
               + (f" · {r['ambiguous']} need confirming" if r["ambiguous"] else ""))
    click.echo(f"    coverage {cov['coverage_pct']}% "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")
    if hz["has_backlog"]:
        click.echo(f"    reconcilable back to {hz['earliest']}; "
                   f"{hz['pre_count']} older charges ({dollars(hz['pre_cents'])}) "
                   f"have no transaction record to match on")


@amazon.command("report")
@click.option("--since", default=None, help="YYYY-MM-DD (default: all history)")
@click.option("--until", default=None, help="YYYY-MM-DD")
def amazon_report(since: str | None, until: str | None) -> None:
    """Render a PRESS-branded PDF breaking down what was bought at Amazon.

    Deliberately separate from the monthly budget report: that page is a fixed
    one-pager about a month, and hundreds of product titles would swamp it.
    """
    from .connectors.amazon import report as az_report
    db.init_schema()
    try:
        r = az_report.render(since, until)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  ✓ {r['items']} items across {r['orders']} orders "
               f"({dollars(r["spent_cents"])})")
    click.echo(f"  ✓ saved to {r['path']}")


@amazon.command("status")
@click.option("--month", default=None, help="YYYY-MM (default: all time)")
def amazon_status(month: str | None) -> None:
    """Coverage — what share of Amazon dollars have item detail behind them."""
    from .connectors.amazon import match as az_match
    db.init_schema()
    with db.connect() as conn:
        cov = az_match.coverage(conn, month)
        hz = az_match.horizon(conn)
        last = conn.execute(
            "SELECT started_at, status, scope, orders_upserted, error_message "
            "FROM amazon_sync_runs ORDER BY sync_run_id DESC LIMIT 1").fetchone()
    scope = month or "all time"
    click.echo(f"Amazon coverage — {scope}")
    click.echo(f"  {cov['coverage_pct']}% of dollars "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")
    click.echo(f"  {cov['matched_txns']} of {cov['total_txns']} charges explained")
    # Without this line a low percentage is unreadable — it looks like a data
    # quality problem when it is a window problem.
    if hz["has_backlog"]:
        click.echo(f"  reconcilable back to {hz['earliest']} — "
                   f"{hz['pre_count']} older charges ({dollars(hz['pre_cents'])}) "
                   f"predate any transaction record")
        click.echo("    `budget amazon backfill` pulls what history the source allows")
    if last:
        click.echo(f"  last sync: {last['started_at']} · {last['status']} · {last['scope']}")
        if last["error_message"]:
            click.echo(f"    ! {last['error_message']}")
    else:
        click.echo("  last sync: never — run `budget amazon sync`")


@amazon.command("match")
@click.option("--confirm", "confirm_pair", default=None, metavar="AMAZON_ID:TXN_ID",
              help="resolve one ambiguous pair by hand")
def amazon_match(confirm_pair: str | None) -> None:
    """Re-run matching, or confirm an ambiguous pair.

    Ambiguity is never guessed: two Amazon charges of the same amount days
    apart is routine, and attributing the wrong basket of items to a charge is
    worse than leaving it unexplained.
    """
    from .connectors.amazon import match as az_match
    db.init_schema()
    if confirm_pair:
        try:
            a_id, t_id = (int(x) for x in confirm_pair.split(":", 1))
        except ValueError as e:
            raise click.ClickException("use --confirm AMAZON_ID:TXN_ID") from e
        with db.connect() as conn:
            az_match.confirm(conn, a_id, t_id)
        click.echo(f"  ✓ amazon txn {a_id} -> transaction {t_id}")
        return
    with db.connect() as conn:
        r = az_match.run(conn)
    click.echo(f"  ✓ matched {r['matched']} ({r['exact']} exact, {r['windowed']} windowed)")
    for a in r["ambiguous"]:
        click.echo(f"\n  ? amazon txn {a['amazon_txn_id']} · {a['completed_date']} · "
                   f"{dollars(a['amount_cents'])} · order {a['order_number'] or '—'}")
        for c in a["candidates"]:
            click.echo(f"      confirm with: budget amazon match --confirm "
                       f"{a['amazon_txn_id']}:{c['txn_id']}"
                       f"   ({c['posted_date']} {c['merchant_norm']})")


@amazon.command("items")
@click.option("--month", default=None, help="YYYY-MM (default: all time)")
def amazon_items(month: str | None) -> None:
    """What you actually bought, behind the matched Amazon charges."""
    from .connectors.amazon import match as az_match
    db.init_schema()
    with db.connect() as conn:
        rows = az_match.breakdown(conn, month)
        cov = az_match.coverage(conn, month)
    if not rows:
        click.echo("  no matched Amazon items yet — run `budget amazon sync`")
        return
    click.echo(f"Amazon items — {month or 'all time'}")
    click.echo(f"  {'DATE':<11} {'AMOUNT':>10}  ITEM")
    for r in rows:
        line = (r["unit_price_cents"] or 0) * (r["quantity"] or 1)
        qty = f" x{r['quantity']}" if (r["quantity"] or 1) > 1 else ""
        title = (r["title"] or "—")[:58]
        click.echo(f"  {r['posted_date']:<11} {dollars(line):>10}  {title}{qty}")
    if cov["coverage_pct"] < 100:
        click.echo(f"\n  ! {100 - cov['coverage_pct']:.1f}% of Amazon spend is still "
                   f"unexplained — `budget amazon status`")


@amazon.command("unmatched")
@click.option("--month", default=None, help="YYYY-MM (default: all time)")
def amazon_unmatched(month: str | None) -> None:
    """Amazon charges in the ledger with no item detail behind them."""
    from .connectors.amazon.match import MERCHANT_LIKE
    db.init_schema()
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    where_month = " AND posted_date LIKE ?" if month else ""
    params = (*MERCHANT_LIKE, *((f"{month}-%",) if month else ()))
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT txn_id, posted_date, merchant_norm, amount_cents
                  FROM transactions
                 WHERE status='posted' AND amount_cents < 0 AND ({like}){where_month}
                   AND txn_id NOT IN (SELECT txn_id FROM amazon_matches)
              ORDER BY amount_cents""", params).fetchall()
    if not rows:
        click.echo("  ✓ every Amazon charge has item detail behind it")
        return
    click.echo(f"Unexplained Amazon charges — {month or 'all time'}")
    for r in rows:
        click.echo(f"  {r['txn_id']:>5}  {r['posted_date']}  "
                   f"{dollars(r['amount_cents']):>10}  {r['merchant_norm']}")


# ── walmart connector ────────────────────────────────────────────────────────
@main.group()
def walmart() -> None:
    """Pull Walmart order + item detail and reconcile it against the ledger.

    Same idea as `budget amazon`, and this ledger carries roughly twice as many
    Walmart dollars. Walmart publishes no consumer order API, so this reads your
    own order history through a browser session you capture once with
    `budget walmart login`. Your data, your account — but Walmart's terms
    prohibit automated extraction, and a page redesign can break it.

    No password is ever stored: Walmart sign-in ends in a one-time code that no
    stored secret can answer, so the session itself is the only credential.
    """


@walmart.command("login")
@click.option("--timeout", default=None, type=int,
              help="seconds to wait for you to finish signing in "
                   "(default: the connector's, currently 600)")
def walmart_login(timeout: int | None) -> None:
    """Sign in through a browser window and cache the session (0600)."""
    from .connectors.walmart import browser_login
    db.init_schema()
    # Defaulted HERE rather than in the option, so the connector stays the one
    # place that decides how long a sign-in reasonably takes. A literal here
    # silently overrode it once already — the module said 600 and the window
    # closed at 300.
    timeout = timeout or browser_login.DEFAULT_TIMEOUT
    try:
        r = browser_login.login(timeout=timeout, echo=click.echo)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  ✓ session cached at {r['path']} (0600)")
    click.echo("    now run: budget walmart capture")


@walmart.command("capture")
@click.option("--headed", is_flag=True,
              help="show the browser window (try this if headless is challenged)")
def walmart_capture(headed: bool) -> None:
    """Dump what the order pages actually serve, for parser development.

    Diagnostic only — nothing it writes is ever read by the connector. Output
    lands in data/walmart/capture/, which is gitignored: it is real order
    content.
    """
    from .connectors.walmart import capture as wm_capture, session as wm_session
    db.init_schema()
    try:
        m = wm_capture.run(headless=not headed, echo=click.echo)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  ✓ captured {'headed' if headed else 'headless'} — "
               f"{len(m['pages'])} pages, {len(m['responses'])} JSON responses")
    for p in m["pages"]:
        keys = ", ".join(f"{k} ({v:,}b)" for k, v in p["inline_keys"].items()) or "none"
        click.echo(f"    {p['label']:<13} {p['html_bytes']:>9,}b html · inline: {keys}")
    click.echo(f"    order links on the list page: {m['order_links']}")
    click.echo(f"  ✓ written to {wm_session.capture_dir()}")


@walmart.command("sync")
@click.option("--days", type=int, default=90, show_default=True,
              help="how far back to pull")
@click.option("--detail", is_flag=True,
              help="also read each order's page for item lines (slow; one page "
                   "load per order, and the part Walmart challenges)")
@click.option("--headed", is_flag=True, help="show the browser window")
def walmart_sync(days: int, detail: bool, headed: bool) -> None:
    """Fetch recent orders, store them, and match them to bank charges.

    Reconciling needs only the order total, which the list carries — so this is
    quick and safe to run often. Item lines come from `budget walmart backfill`,
    or from `--detail` here.
    """
    from .connectors.walmart import sync as wm_sync
    db.init_schema()
    try:
        r = wm_sync.run_sync(days=days, detail=detail, headless=not headed,
                             on_progress=click.echo)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    cov = r["coverage"]
    click.echo(f"  ✓ {r['orders']} orders · {r['items']} item lines"
               + (f" · {r['detailed']} detail pages read" if "detailed" in r else ""))
    click.echo(f"    {r['matched']} orders reconcile"
               + (f" (+{r['new_matches']} this run: {r['exact']} single-charge, "
                  f"{r['split']} split settlement)" if r["new_matches"] else "")
               + (f" · {r['ambiguous']} need confirming" if r["ambiguous"] else ""))
    click.echo(f"    coverage {cov['coverage_pct']}% of Walmart spend "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")


@walmart.command("backfill")
@click.option("--since", default=None,
              help="YYYY-MM-DD (default: earliest Walmart charge in the ledger)")
@click.option("--limit", type=int, default=None,
              help="stop after this many detail pages (the rest resume next run)")
@click.option("--headed", is_flag=True, help="show the browser window")
@click.option("--dry-run", is_flag=True, help="show what would be fetched")
def walmart_backfill(since: str | None, limit: int | None, headed: bool,
                     dry_run: bool) -> None:
    """Pull order history across the whole range the ledger covers.

    A long job — item detail is one request per order — so it is resumable:
    every order records whether its detail page has been read, and re-running
    picks up where it stopped. The cheap list pass runs first, so even an
    interrupted backfill leaves coverage better than it found it.
    """
    from .connectors.walmart import backfill as wm_backfill
    db.init_schema()
    with db.connect() as conn:
        p = wm_backfill.plan(conn, since)
    if p["reason"]:
        click.echo(f"  {p['reason']}")
        return
    click.echo(f"  history from:   {p['since']}")
    click.echo(f"  orders stored:  {p['stored']}")
    click.echo(f"  need detail:    {p['pending']}"
               + (f" (fetching {limit} this run)" if limit else ""))
    if dry_run:
        click.echo("\n  (dry run — nothing fetched)")
        return
    try:
        r = wm_backfill.run_backfill(since=since, limit=limit,
                                     headless=not headed, on_progress=click.echo)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    cov, hz = r["coverage"], r["horizon"]
    click.echo(f"\n  ✓ {r['orders']} orders · {r['detailed']} detail pages read")
    if r["remaining"]:
        click.echo(f"    {r['remaining']} still need detail — re-run to continue")
    click.echo(f"    matched {r['matched']}"
               + (f" · {r['ambiguous']} need confirming" if r["ambiguous"] else ""))
    click.echo(f"    coverage {cov['coverage_pct']}% "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")
    if hz["has_backlog"]:
        click.echo(f"    reconcilable back to {hz['earliest']}; "
                   f"{hz['pre_count']} older charges ({dollars(hz['pre_cents'])}) "
                   f"have no order record to match on")


@walmart.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="show what would be written")
def walmart_import(path: str, dry_run: bool) -> None:
    """Load a purchase-history spreadsheet export (.xlsx).

    The backfill path that does not touch Walmart. `sync` and `backfill` walk
    the site, which works for the last handful of orders and gets challenged
    long before it reaches last year; an export from an already-signed-in
    browser carries the same orders with no bot-detection surface at all.

    Safe to run over data you already have: orders upsert by number, so this
    fills in what is missing and leaves existing matches intact.
    """
    from .connectors.walmart import import_xlsx as wm_import
    from .connectors.walmart.parse import WalmartParseError
    db.init_schema()

    if dry_run:
        try:
            s = wm_import.summarize(wm_import.load(path))
        except WalmartParseError as e:
            raise click.ClickException(str(e)) from e
        click.echo(f"  orders:    {s['orders']} ({s['since']} → {s['until']})")
        click.echo(f"  items:     {s['items']}")
        click.echo("  channels:  "
                   + ", ".join(f"{k} {v}" for k, v in sorted(s["channels"].items())))
        click.echo(f"  lines tie to subtotal: {s['reconciling']}/{s['comparable']}")
        click.echo("\n  (dry run — nothing written)")
        return

    try:
        r = wm_import.run_import(path, on_progress=click.echo)
    except WalmartParseError as e:
        raise click.ClickException(str(e)) from e
    s, cov = r["summary"], r["coverage"]
    click.echo(f"\n  ✓ {r['orders']} orders · {r['items']} item lines stored")
    click.echo(f"    matched {r['matched']}"
               + (f" · {r['ambiguous']} need confirming" if r["ambiguous"] else ""))
    click.echo(f"    coverage {cov['coverage_pct']}% "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")
    # Stated plainly rather than buried: these lines are the source's, and the
    # source does not always agree with its own subtotals. A reader who thinks
    # the item totals are exact will over-trust a category breakdown built on
    # them.
    click.echo(f"    {s['reconciling']} of {s['comparable']} orders have item "
               f"lines summing to their subtotal; the rest differ (the export "
               f"does not restate checkout price changes)")
    hz = r["horizon"]
    if hz["has_backlog"]:
        click.echo(f"    reconcilable back to {hz['earliest']}; "
                   f"{hz['pre_count']} older charges ({dollars(hz['pre_cents'])}) "
                   f"have no order record to match on")


@walmart.command("status")
@click.option("--month", default=None, help="YYYY-MM (default: all time)")
def walmart_status(month: str | None) -> None:
    """Coverage — what share of Walmart dollars have item detail behind them."""
    from .connectors.walmart import match as wm_match
    db.init_schema()
    with db.connect() as conn:
        cov = wm_match.coverage(conn, month)
        hz = wm_match.horizon(conn)
        last = conn.execute(
            "SELECT started_at, status, scope, orders_upserted, error_message "
            "FROM walmart_sync_runs ORDER BY sync_run_id DESC LIMIT 1").fetchone()
    click.echo(f"Walmart coverage — {month or 'all time'}")
    click.echo(f"  {cov['coverage_pct']}% of dollars "
               f"({dollars(cov['matched_cents'])} of {dollars(cov['total_cents'])})")
    click.echo(f"  {cov['matched_txns']} of {cov['total_txns']} charges explained")
    # Online and in-store are different problems with different fixes; a single
    # averaged number says which neither.
    for name, c in cov["channels"].items():
        if c["total_cents"]:
            click.echo(f"    {name:<9} {c['coverage_pct']:>5}%  "
                       f"({dollars(c['matched_cents'])} of {dollars(c['total_cents'])})")
    st = cov["split_settlements"]
    if st["split_orders"]:
        click.echo(f"  {st['split_orders']} of {st['orders']} matched orders "
                   f"settled as more than one charge (up to {st['max_parts']})")
    # Without this line a low percentage is unreadable — it looks like a data
    # quality problem when it is a window problem.
    if hz["has_backlog"]:
        click.echo(f"  reconcilable back to {hz['earliest']} — "
                   f"{hz['pre_count']} older charges ({dollars(hz['pre_cents'])}) "
                   f"predate any order record")
        click.echo("    `budget walmart backfill` pulls what history the source allows")
    if last:
        click.echo(f"  last sync: {last['started_at']} · {last['status']} · {last['scope']}")
        if last["error_message"]:
            click.echo(f"    ! {last['error_message']}")
    else:
        click.echo("  last sync: never — run `budget walmart sync`")


@walmart.command("match")
@click.option("--confirm", "confirm_pair", default=None, metavar="CHARGE_ID:TXN_ID",
              help="resolve one ambiguous pair by hand")
def walmart_match(confirm_pair: str | None) -> None:
    """Re-run matching, or confirm an ambiguous pair.

    Ambiguity is never guessed: two Walmart charges of the same amount days
    apart is routine, and attributing the wrong basket of items to a charge is
    worse than leaving it unexplained.
    """
    from .connectors.walmart import match as wm_match
    db.init_schema()
    if confirm_pair:
        try:
            c_id, t_id = (int(x) for x in confirm_pair.split(":", 1))
        except ValueError as e:
            raise click.ClickException("use --confirm CHARGE_ID:TXN_ID") from e
        with db.connect() as conn:
            wm_match.confirm(conn, c_id, t_id)
        click.echo(f"  ✓ walmart charge {c_id} -> transaction {t_id}")
        return
    with db.connect() as conn:
        r = wm_match.run(conn)
    click.echo(f"  ✓ matched {r['matched']} ({r['exact']} exact, {r['windowed']} windowed)")
    for a in r["ambiguous"]:
        click.echo(f"\n  ? walmart charge {a['walmart_charge_id']} · "
                   f"{a['charged_date']} · {dollars(a['amount_cents'])} · "
                   f"order {a['order_number'] or '—'}"
                   + ("  (date inferred from the order)" if a["derived"] else ""))
        for c in a["candidates"]:
            click.echo(f"      confirm with: budget walmart match --confirm "
                       f"{a['walmart_charge_id']}:{c['txn_id']}"
                       f"   ({c['posted_date']} {c['merchant_norm']})")


@walmart.command("items")
@click.option("--month", default=None, help="YYYY-MM (default: all time)")
def walmart_items(month: str | None) -> None:
    """What you actually bought, behind the matched Walmart charges."""
    from .connectors.walmart import match as wm_match
    db.init_schema()
    with db.connect() as conn:
        rows = wm_match.breakdown(conn, month)
        cov = wm_match.coverage(conn, month)
    if not rows:
        click.echo("  no matched Walmart items yet — run `budget walmart sync`")
        return
    click.echo(f"Walmart items — {month or 'all time'}")
    click.echo(f"  {'DATE':<11} {'AMOUNT':>10}  ITEM")
    for r in rows:
        qty = f" x{r['quantity']}" if (r["quantity"] or 1) > 1 else ""
        title = (r["title"] or "—")[:58]
        click.echo(f"  {r['posted_date']:<11} "
                   f"{dollars(r['line_price_cents'] or 0):>10}  {title}{qty}")
    if cov["coverage_pct"] < 100:
        click.echo(f"\n  ! {100 - cov['coverage_pct']:.1f}% of Walmart spend is still "
                   f"unexplained — `budget walmart status`")


@walmart.command("unmatched")
@click.option("--month", default=None, help="YYYY-MM (default: all time)")
def walmart_unmatched(month: str | None) -> None:
    """Walmart charges in the ledger with no item detail behind them."""
    from .connectors.walmart.match import MERCHANT_LIKE
    db.init_schema()
    like = " OR ".join("merchant_norm LIKE ?" for _ in MERCHANT_LIKE)
    where_month = " AND posted_date LIKE ?" if month else ""
    params = (*MERCHANT_LIKE, *((f"{month}-%",) if month else ()))
    with db.connect() as conn:
        rows = conn.execute(
            f"""SELECT txn_id, posted_date, merchant_norm, amount_cents
                  FROM transactions
                 WHERE status='posted' AND amount_cents < 0 AND ({like}){where_month}
                   AND txn_id NOT IN (SELECT txn_id FROM walmart_matches)
              ORDER BY amount_cents""", params).fetchall()
    if not rows:
        click.echo("  ✓ every Walmart charge has item detail behind it")
        return
    click.echo(f"Unexplained Walmart charges — {month or 'all time'}")
    for r in rows:
        click.echo(f"  {r['txn_id']:>5}  {r['posted_date']}  "
                   f"{dollars(r['amount_cents']):>10}  {r['merchant_norm']}")


@walmart.command("report")
@click.option("--since", default=None, help="YYYY-MM-DD (default: all history)")
@click.option("--until", default=None, help="YYYY-MM-DD")
def walmart_report(since: str | None, until: str | None) -> None:
    """Render a PRESS-branded PDF breaking down what was bought at Walmart."""
    from .connectors.walmart import report as wm_report
    db.init_schema()
    try:
        r = wm_report.render(since, until)
    except Exception as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"  ✓ {r['items']} items across {r['orders']} orders "
               f"({dollars(r['spent_cents'])})")
    click.echo(f"  ✓ saved to {r['path']}")


if __name__ == "__main__":
    main()
