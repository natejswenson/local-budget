"""CLI entry point: `budget <subcommand>` (design §5)."""
from __future__ import annotations

from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from . import budgets as budgets_mod
from . import categories, db, detect, reconcile, reports
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
def report_pdf(period: str) -> None:
    """Render the visual report PDF for PERIOD (YYYY-MM) — the no-MCP path to
    the same deterministic renderer the render_report tool uses."""
    from .report import render as report_render
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


if __name__ == "__main__":
    main()
