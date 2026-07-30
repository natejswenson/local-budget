"""The contract with `amazon-orders` — real parser, real HTML, no network.

`test_amazon_connector.py` tests our logic against fakes we wrote. This file
tests our *assumptions*, by running the actual library parser over real Amazon
page snapshots (see fixtures/amazon/PROVENANCE.md) and pushing the resulting
real entity objects through `store` and `match`.

The distinction is not academic. The fakes had `Order.grand_total` negative and
`Transaction.grand_total` negative. Reality is positive and negative
respectively — opposite conventions, one of them wrong in our code, and 29
green mock-based tests could not see it.

This is the tier that catches an upstream field rename, a type change, or a
flipped sign on upgrade. It still cannot tell us the scraper works against
Amazon today; only a live `budget amazon sync` does that.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from local_budget import db
from local_budget.connectors.amazon import match, store

FIXTURES = Path(__file__).parent / "fixtures" / "amazon"

pytest.importorskip("bs4", reason="amazon-orders not installed")


@pytest.fixture()
def cfg(tmp_path):
    from amazonorders.conf import AmazonOrdersConfig
    return AmazonOrdersConfig(data={"output_dir": str(tmp_path / "out"),
                                    "cookie_jar_path": str(tmp_path / "c.json")},
                              config_path=str(tmp_path / "config.yml"))


def _soup(name: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup((FIXTURES / name).read_text(encoding="utf-8"), "html.parser")


@pytest.fixture()
def real_order(cfg):
    from amazonorders.entity.order import Order
    return Order(_soup("order-snippet.html"), cfg, full_details=True)


@pytest.fixture()
def real_charge(cfg):
    from amazonorders.entity.transaction import Transaction
    return Transaction(_soup("transaction-charge.html"), cfg, date(2026, 7, 20))


@pytest.fixture()
def real_refund(cfg):
    from amazonorders.entity.transaction import Transaction
    return Transaction(_soup("transaction-refund.html"), cfg, date(2026, 7, 9))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1, 'Test', 'csv')")
        yield c


# ── the attribute contract ───────────────────────────────────────────────────
# Every attribute store.py reads, by name. store.py uses getattr(..., None)
# defaults, which is deliberate (a partially-parsed order is still worth
# keeping) but means a RENAMED field degrades to a silent NULL forever rather
# than an error. This list is the tripwire for that.
ORDER_ATTRS = ["order_number", "order_placed_date", "grand_total", "subtotal",
               "estimated_tax", "shipping_total", "refund_total",
               "payment_method", "item_count", "cancelled", "items"]
ITEM_ATTRS = ["asin", "title", "quantity", "price", "seller", "condition"]
TXN_ATTRS = ["completed_date", "grand_total", "is_refund", "order_number",
             "payment_method", "seller"]


@pytest.mark.parametrize("attr", ORDER_ATTRS)
def test_order_still_exposes_every_field_we_read(real_order, attr):
    assert hasattr(real_order, attr), (
        f"amazon-orders no longer exposes Order.{attr} — store.py reads it with a "
        f"getattr default, so this would silently write NULL on every order")


@pytest.mark.parametrize("attr", ITEM_ATTRS)
def test_item_still_exposes_every_field_we_read(real_order, attr):
    assert real_order.items, "fixture parsed no items"
    assert hasattr(real_order.items[0], attr), (
        f"amazon-orders no longer exposes Item.{attr}")


@pytest.mark.parametrize("attr", TXN_ATTRS)
def test_transaction_still_exposes_every_field_we_read(real_charge, attr):
    assert hasattr(real_charge, attr), (
        f"amazon-orders no longer exposes Transaction.{attr}")


# ── the sign contract ────────────────────────────────────────────────────────
def test_order_totals_are_positive_and_transaction_charges_are_negative(
        real_order, real_charge, real_refund):
    """The two entities use OPPOSITE conventions, and the schema depends on it.

    `amazon_transactions` is a posting compared directly against
    `transactions.amount_cents`, so it must stay signed the ledger's way.
    `amazon_orders` / `amazon_items` are prices — what a thing cost — and are
    stored as positive magnitudes. Getting either backwards silently inverts
    every comparison built on them.
    """
    assert real_order.grand_total > 0, "Order totals are positive magnitudes"
    assert real_charge.grand_total < 0, "a Transaction CHARGE is negative"
    assert real_refund.grand_total > 0, "a Transaction REFUND is positive"
    assert real_charge.is_refund is False and real_refund.is_refund is True


# ── real entities through our storage ────────────────────────────────────────
def test_real_order_stores_with_ledger_consistent_signs(conn, real_order):
    run = store.start_run(conn, "contract")
    assert store.store_orders(conn, [real_order], run) == 1
    row = conn.execute("SELECT * FROM amazon_orders").fetchone()
    assert row["order_number"] == real_order.order_number
    assert row["grand_total_cents"] > 0
    assert row["order_placed_date"] == real_order.order_placed_date.isoformat()
    items = conn.execute("SELECT * FROM amazon_items").fetchall()
    assert len(items) == len(real_order.items)
    assert items[0]["title"] and items[0]["unit_price_cents"] > 0


def test_real_order_quantity_none_becomes_one(conn, real_order):
    """The real parser returns `quantity=None` on a single-item line; the fakes
    returned 1. A None reaching the line-total arithmetic in `breakdown` would
    raise, so store normalises it."""
    assert real_order.items[0].quantity is None, "fixture no longer exercises this"
    run = store.start_run(conn, "contract")
    store.store_orders(conn, [real_order], run)
    assert conn.execute("SELECT quantity FROM amazon_items").fetchone()["quantity"] == 1


def test_real_charge_and_refund_round_trip(conn, real_charge, real_refund):
    run = store.start_run(conn, "contract")
    assert store.store_transactions(conn, [real_charge, real_refund], run) == 2
    charge = conn.execute(
        "SELECT * FROM amazon_transactions WHERE is_refund=0").fetchone()
    refund = conn.execute(
        "SELECT * FROM amazon_transactions WHERE is_refund=1").fetchone()
    assert charge["grand_total_cents"] < 0 < refund["grand_total_cents"]
    assert charge["order_number"] == real_charge.order_number


def test_a_real_charge_matches_a_bank_row_end_to_end(conn, real_order, real_charge):
    """The full chain on real parser output: bank row → Amazon charge → order
    → items. This is the closest we get to the live path without credentials."""
    run = store.start_run(conn, "contract")
    store.store_orders(conn, [real_order], run)
    # Point the charge at the order we stored, then mirror it as a bank row.
    real_charge.order_number = real_order.order_number
    store.store_transactions(conn, [real_charge], run)
    cents = store.to_cents(real_charge.grand_total)
    conn.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, imported_at) "
        "VALUES (1,1,'f1','2026-07-20',?,'posted','AMAZON MKTPL AMZN.COM','2026-07-01')",
        (cents,))

    assert match.run(conn)["exact"] == 1
    rows = match.breakdown(conn, "2026-07")
    assert rows and rows[0]["title"] == real_order.items[0].title
    cov = match.coverage(conn, "2026-07")
    assert cov["coverage_pct"] == 100.0
