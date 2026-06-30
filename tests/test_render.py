"""Snapshot tests for the deterministic render layer (the 'clean & beautiful'
contract). These lock the exact markdown so output can't silently regress."""
from __future__ import annotations

from local_budget.agent import render as r


def test_money_signed_no_float():
    assert r.money(-123456) == "-$1,234.56"
    assert r.money(0) == "$0.00"
    assert r.money(1000000) == "$10,000.00"
    assert r.money(500) == "$5.00"


def test_table_alignment_and_null():
    out = r.table(
        [{"cat": "Groceries", "spent": "$50.00", "n": 3},
         {"cat": "Gas", "spent": "$12.00", "n": None}],
        [("cat", "Category"), ("spent", "Spent"), ("n", "#")],
    )
    assert out == (
        "| Category | Spent | # |\n"
        "| --- | ---: | ---: |\n"
        "| Groceries | $50.00 | 3 |\n"
        "| Gas | $12.00 | — |"
    )


def test_table_empty_is_header_only():
    assert r.table([], [("cat", "Category")]) == "| Category |\n| --- |"


def test_bars_share_and_widths():
    out = r.bars([("Groceries", -5000), ("Gas", -1200)])
    assert out == (
        "Groceries  " + "▇" * 20 + "  -$50.00 (81%)\n"
        "Gas  " + "▇" * 5 + "  -$12.00 (19%)"
    )


def test_bars_empty():
    assert r.bars([]) == ""
