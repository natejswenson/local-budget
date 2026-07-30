"""The Walmart tools the agent sees, and the split proposal that now serves two
connectors.

What is worth pinning here is not the SQL — `test_walmart_connector.py` covers
that — but what the agent is TOLD. A breakdown that omits its coverage caveat
reads as a complete account of a month's Walmart spend when it may be a quarter
of it, and that is the failure this surface is designed against.
"""
from __future__ import annotations

import asyncio

import pytest

from local_budget import db
from local_budget.agent import tools
from local_budget.connectors.walmart import store


@pytest.fixture(autouse=True)
def _use_data_dir(data_dir):
    yield


@pytest.fixture(autouse=True)
def no_network_egress():
    """Shadows the conftest guard, which blocks `socket.socket` — asyncio needs
    it to build an event loop. These tools make no request; the guard is aimed
    at the deterministic core, and `fetch.py` is the only module that egresses."""
    yield


def _call(name, args=None):
    return asyncio.run(tools.SPEC_BY_NAME[name].handler(args or {}))


def _seed(*, channel="online", charges=None, items=None, bank=None):
    db.init_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO accounts (account_id, acct_last4, acct_hash, "
                     "created_at) VALUES (1,'1234','h',?)", (db.now_iso(),))
        for i, (dt, cents, merchant) in enumerate(bank or [], start=1):
            conn.execute(
                "INSERT INTO transactions (account_id, fitid, posted_date, "
                " amount_cents, status, txn_type, payee, memo, merchant_norm, "
                " category, raw_ofx, imported_at) "
                "VALUES (1,?,?,?, 'posted','DEBIT',?,'m',?,'Shopping','raw',?)",
                (f"f{i}", dt, cents, merchant, merchant, db.now_iso()))
        if items is not None:
            store.store_orders(conn, [{
                "order_number": "O1", "order_placed_date": "2026-06-03",
                "grand_total": "80.00", "channel": channel, "detail_fetched": True,
                "items": items, "charges": charges or []}],
                store.start_run(conn, "t"))
        from local_budget.connectors.walmart import match
        match.run(conn)


def _item(title, price, **kw):
    return {"title": title, "unit_price": price, "quantity": 1,
            "product_id": "p1", "seller": "Walmart.com", **kw}


# ── breakdown ────────────────────────────────────────────────────────────────
def test_breakdown_says_what_to_run_when_there_is_nothing_yet():
    """An empty table with no next action reads as "you bought nothing"."""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")], items=None)
    r = _call("walmart_breakdown", {"month": "2026-06"})
    assert r["data"]["items"] == []
    assert "budget walmart sync" in r["rendered"]


def test_breakdown_leads_with_the_shape_then_the_rows():
    """Forty undifferentiated rows is a list, not a breakdown."""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")],
          items=[_item("Great Value milk", "3.48"),
                 _item("Mainstays bookcase", "76.52")])
    r = _call("walmart_breakdown", {"month": "2026-06"})
    assert "2 items across 1 orders" in r["rendered"]
    assert "Mainstays bookcase" in r["rendered"]


def test_breakdown_shows_where_each_purchase_happened():
    _seed(channel="in-store",
          bank=[("2026-06-03", -8000, "WM SUPERCENTER FARGO")],
          items=[_item("Milk", "80.00")])
    r = _call("walmart_breakdown", {"month": "2026-06"})
    assert "in-store" in r["rendered"]


def test_breakdown_carries_its_coverage_caveat_when_incomplete():
    """The whole point of the surface. Without this the agent quotes a quarter
    of a month's Walmart spend as though it were all of it."""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM"),
                ("2026-06-10", -24000, "WALMART.COM")],
          items=[_item("Bookcase", "80.00")])
    r = _call("walmart_breakdown", {"month": "2026-06"})
    assert "⚠" in r["rendered"]
    assert "25.0% of Walmart spend is explained" in r["rendered"]


# ── coverage ─────────────────────────────────────────────────────────────────
def test_coverage_reports_dollars_and_splits_online_from_in_store():
    """One is a question about the parser, the other about whether Walmart holds
    the receipt at all. A single averaged number says which neither."""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM"),
                ("2026-06-04", -12000, "WAL MART FARGO")],
          items=[_item("Bookcase", "80.00")])
    r = _call("walmart_coverage", {"month": "2026-06"})
    assert "online: **100.0%**" in r["rendered"]
    assert "in-store: **0.0%**" in r["rendered"]


def test_coverage_never_renders_spend_as_a_refund():
    """coverage() already returns positive outflow magnitudes; negating here
    once made every figure on the page read as money coming back."""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")],
          items=[_item("Bookcase", "80.00")])
    r = _call("walmart_coverage", {"month": "2026-06"})
    assert "-$" not in r["rendered"]


def test_coverage_discloses_an_inferred_settle_date():
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")],
          items=[_item("Bookcase", "80.00")])
    r = _call("walmart_coverage", {"month": "2026-06"})
    assert "the settle date is inferred" in r["rendered"]


def test_no_inference_disclosure_when_the_charge_was_observed():
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")],
          items=[_item("Bookcase", "80.00")],
          charges=[{"charged_date": "2026-06-03", "amount": "-80.00"}])
    r = _call("walmart_coverage", {"month": "2026-06"})
    assert "inferred" not in r["rendered"]


# ── propose_split across two connectors ──────────────────────────────────────
def test_propose_split_falls_back_to_the_walmart_order():
    """Tried in turn rather than dispatched on the merchant string: the merchant
    is the bank's text, while which connector holds the order is a fact about
    what has been synced."""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")],
          items=[_item("Bookcase", "60.00"), _item("Lamp", "20.00")])
    txn = _call("walmart_breakdown", {"month": "2026-06"})["data"]["items"][0]["txn_id"]
    r = _call("propose_split", {"txn_id": txn})
    assert r["data"]["source"] == "walmart"
    assert "from the walmart order" in r["rendered"]
    assert sum(i["suggested_cents"] for i in r["data"]["items"]) == -8000


def test_propose_split_reports_both_connectors_when_neither_has_an_order():
    """A bare "no order" cannot be acted on — which connector was expected to
    have it, and what should be run?"""
    _seed(bank=[("2026-06-03", -8000, "WALMART.COM")], items=None)
    with db.connect() as conn:
        txn = conn.execute("SELECT txn_id FROM transactions").fetchone()["txn_id"]
    r = _call("propose_split", {"txn_id": txn})
    assert "amazon:" in r["error"] and "walmart:" in r["error"]
