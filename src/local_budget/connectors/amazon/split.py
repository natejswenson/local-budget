"""Propose a split for a bank charge from the Amazon order behind it.

This module does arithmetic, not judgment. It returns the item lines and what
each is worth once scaled to the actual charge; **it never assigns a category**,
because there is no product category in the source data — only titles, ASINs and
sellers. Deciding that a cat-food line is Groceries is the agent's call, made
explicitly and confirmed by a human, and the ledger should never appear to have
asserted it on its own.
"""
from __future__ import annotations

import sqlite3

from ... import splits


class NoOrderBehind(RuntimeError):
    """The charge has no reconciled Amazon order to split by."""


def propose(conn: sqlite3.Connection, txn_id: int) -> dict:
    """Item lines for `txn_id`, scaled so they sum to the charge exactly.

    Returns ``{txn, items[], item_total_cents, charge_cents, scaled}`` where
    each item carries ``suggested_cents`` (its share of the charge) alongside
    ``list_cents`` (what it lists at). Both are reported because they differ —
    the charge is net of discounts and promotions — and quoting the wrong one
    contradicts the report on the same page.
    """
    txn = conn.execute(
        "SELECT txn_id, posted_date, merchant_norm, amount_cents, category "
        "FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
    if txn is None:
        raise NoOrderBehind(f"no transaction {txn_id}")

    rows = conn.execute(
        """SELECT i.asin, i.title, i.quantity, i.unit_price_cents, o.order_number
             FROM amazon_matches m
             JOIN amazon_transactions a ON a.amazon_txn_id = m.amazon_txn_id
             JOIN amazon_orders o       ON o.order_number  = a.order_number
             JOIN amazon_items i        ON i.order_number  = o.order_number
            WHERE m.txn_id = ?
         ORDER BY (i.unit_price_cents * COALESCE(i.quantity,1)) DESC""",
        (txn_id,)).fetchall()
    if not rows:
        raise NoOrderBehind(
            f"transaction {txn_id} has no reconciled Amazon order — run "
            f"`budget amazon sync`, or `budget amazon match` if it is ambiguous")

    # Item prices are positive magnitudes; the charge is negative. Carry the
    # ledger's sign so the scaled lines land the right way round.
    line_cents = [-(int(r["unit_price_cents"] or 0) * int(r["quantity"] or 1))
                  for r in rows]
    charge = int(txn["amount_cents"])
    scaled = splits.allocate(charge, line_cents)

    items = [{
        "asin": r["asin"], "title": r["title"],
        "quantity": r["quantity"] or 1,
        "order_number": r["order_number"],
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
