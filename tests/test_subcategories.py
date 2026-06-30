"""Subcategories + per-subscription budgets."""
from __future__ import annotations

from local_budget import budgets, db, reports
from local_budget.categorize import manual
from local_budget.ingest import importer

from ofx_fixtures import write_ofx


def _seed_subs(tmp_path):
    db.init_schema()
    txns = [
        {"trntype": "DEBIT", "dtposted": "20260405", "amount": "-15.49", "fitid": "N1", "name": "NETFLIX.COM"},
        {"trntype": "DEBIT", "dtposted": "20260505", "amount": "-15.49", "fitid": "N2", "name": "NETFLIX.COM"},
        {"trntype": "DEBIT", "dtposted": "20260605", "amount": "-15.49", "fitid": "N3", "name": "NETFLIX LOS GATOS"},
        {"trntype": "DEBIT", "dtposted": "20260405", "amount": "-11.99", "fitid": "S1", "name": "SPOTIFY USA"},
    ]
    importer.import_file(write_ofx(tmp_path / "wf.qfx", txns))
    # mark them Subscriptions
    manual.set_merchant_category("NETFLIX", "Subscriptions")
    manual.set_merchant_category("SPOTIFY", "Subscriptions")


def test_split_subscriptions_assigns_subcategory(data_dir, tmp_path):
    _seed_subs(tmp_path)
    n = manual.split_subscriptions()
    assert n >= 2
    with db.connect() as conn:
        subs = {r[0] for r in conn.execute(
            "SELECT DISTINCT subcategory FROM transactions WHERE category='Subscriptions'").fetchall()}
    assert "Netflix" in subs and "Spotify" in subs


def test_rename_subcategory_merges(data_dir, tmp_path):
    _seed_subs(tmp_path)
    manual.split_subscriptions()
    # "NETFLIX LOS GATOS" -> "Netflix Los"; merge it into "Netflix"
    manual.rename_subcategory("Subscriptions", "Netflix Los", "Netflix")
    bd = {r["subcategory"]: r for r in reports.subcategory_breakdown("Subscriptions", "all")}
    assert "Netflix Los" not in bd
    # All 3 Netflix txns now under one subcategory
    assert bd["Netflix"]["count"] == 3


def test_per_subscription_budget(data_dir, tmp_path):
    _seed_subs(tmp_path)
    manual.split_subscriptions()
    budgets.set_limit("Subscriptions", 1000, subcategory="Netflix")  # $10
    lims = budgets.list_limits()
    assert any(x["category"] == "Subscriptions" and x["subcategory"] == "Netflix"
               and x["limit_cents"] == 1000 for x in lims)
    bd = {r["subcategory"]: r for r in reports.subcategory_breakdown("Subscriptions", "all")}
    assert bd["Netflix"]["limit_cents"] == 1000


def test_subcategory_budget_status(data_dir, tmp_path):
    _seed_subs(tmp_path)
    manual.split_subscriptions()
    # Effective at/before the viewed month so the point-in-time resolution (S-2)
    # applies it to April; a same-month effective date keeps this a single-month test.
    budgets.set_limit("Subscriptions", 1000, subcategory="Spotify",
                      effective_from="2026-04-01")  # $10 limit, $11.99 spent in 04
    with db.connect() as conn:
        bs = reports.budget_status(conn, "2026-04")
    spotify = next(b for b in bs if b["subcategory"] == "Spotify")
    assert spotify["actual_cents"] == 1199
    assert spotify["over_cents"] == 199  # $1.99 over


def test_friendly_name():
    assert manual.friendly_name("NETFLIX.COM NETFLIX.COM") == "Netflix"
    assert manual.friendly_name("APPLE.COM BILL") == "Apple"
    assert manual.friendly_name("SPOTIFY USA") == "Spotify"


def test_set_merchant_subcategory_keeps_category(data_dir, tmp_path):
    _seed_subs(tmp_path)
    manual.set_merchant_subcategory("SPOTIFY", "Music")
    with db.connect() as conn:
        r = conn.execute("SELECT category, subcategory FROM transactions WHERE merchant_norm LIKE 'SPOTIFY%'").fetchone()
    assert r["category"] == "Subscriptions" and r["subcategory"] == "Music"
