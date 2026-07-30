"""Propose a split for a bank charge from the Walmart order behind it.

This module does arithmetic, not judgment. It returns the item lines and what
each is worth once scaled to the actual charge; **it never assigns a category**.
Even where Walmart carries its own product taxonomy, that is Walmart's shelf
label, not this budget's category — deciding a bag of dog food is Groceries
rather than Pets is a judgment the agent states explicitly and a human confirms,
and the ledger should never appear to have asserted it on its own.
"""
from __future__ import annotations

import sqlite3

from ... import splits


class NoOrderBehind(RuntimeError):
    """The charge has no reconciled Walmart order to split by."""


def propose(conn: sqlite3.Connection, txn_id: int) -> dict:
    """Item lines for `txn_id`, scaled so they sum to the charge exactly.

    Returns ``{txn, items[], item_total_cents, charge_cents, scaled}`` where
    each item carries ``suggested_cents`` (its share of the charge) alongside
    ``list_cents`` (what it lists at). Both are reported because they differ —
    the charge is net of discounts, rollbacks and any Walmart Cash applied, and
    tax pushes the other way — and quoting the wrong one contradicts the report
    on the same page.

    A split settlement is the normal case here, not an edge: the items belong to
    the ORDER, and this charge is frequently only one part of what paid for it —
    an order settling as five bank rows will propose the same item list against
    each. Scaling to the charge keeps every cent attributed to a real item, and
    `scaled` tells the caller the two figures are not the same number.
    """
    txn = conn.execute(
        "SELECT txn_id, posted_date, merchant_norm, amount_cents, category "
        "FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if txn is None:
        raise NoOrderBehind(f"no transaction {txn_id}")

    rows = conn.execute(
        """SELECT i.product_id, i.title, i.quantity, i.line_price_cents,
                  i.category AS source_category, o.order_number
             FROM walmart_matches m
             JOIN walmart_orders o ON o.order_number = m.order_number
             JOIN walmart_items i  ON i.order_number = o.order_number
            WHERE m.txn_id = ?
         ORDER BY i.line_price_cents DESC""",
        (txn_id,)).fetchall()
    if not rows:
        raise NoOrderBehind(
            f"transaction {txn_id} has no reconciled Walmart order — run "
            f"`budget walmart sync`, `budget walmart backfill` for older "
            f"charges, or `budget walmart match` if it is ambiguous")

    # Item prices are positive magnitudes; the charge is negative. Carry the
    # ledger's sign so the scaled lines land the right way round.
    line_cents = [-int(r["line_price_cents"] or 0) for r in rows]
    charge = int(txn["amount_cents"])
    scaled = splits.allocate(charge, line_cents)

    items = [{
        "product_id": r["product_id"], "title": r["title"],
        "quantity": r["quantity"] or 1,
        "order_number": r["order_number"],
        # Walmart's own shelf category when the page carried one. Passed through
        # as a HINT for whoever is choosing a budget category, never as one.
        "source_category": r["source_category"],
        "list_cents": line_cents[i],
        "suggested_cents": scaled[i],
    } for i, r in enumerate(rows)]

    return {
        "txn": dict(txn),
        "items": items,
        "item_total_cents": sum(line_cents),
        "charge_cents": charge,
        "scaled": sum(line_cents) != charge,
    }
