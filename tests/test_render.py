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


def test_table_empty_renders_explicit_empty_state():
    # Skills print `rendered` verbatim — a header-only skeleton reads as a
    # broken response, so empty rows get an explicit line instead.
    assert r.table([], [("cat", "Category")]) == "(nothing to show)"


def test_bars_share_and_widths():
    out = r.bars([("Groceries", -5000), ("Gas", -1200)])
    assert out == (
        "Groceries  " + "▇" * 20 + "  -$50.00 (81%)\n"
        "Gas  " + "▇" * 5 + "  -$12.00 (19%)"
    )


def test_bars_empty():
    assert r.bars([]) == ""


def test_table_numbered_prepends_row_column():
    out = r.table(
        [{"cat": "Groceries", "spent": "$50.00", "n": 3},
         {"cat": "Gas", "spent": "$12.00", "n": 1}],
        [("cat", "Category"), ("spent", "Spent"), ("n", "#")],
        numbered=True,
    )
    assert out == (
        "| Row | Category | Spent | # |\n"
        "| ---: | --- | ---: | ---: |\n"
        "| 1 | Groceries | $50.00 | 3 |\n"
        "| 2 | Gas | $12.00 | 1 |"
    )


def test_table_numbered_false_is_byte_identical_to_current_behavior():
    rows = [{"cat": "Groceries", "spent": "$50.00", "n": 3},
            {"cat": "Gas", "spent": "$12.00", "n": None}]
    cols = [("cat", "Category"), ("spent", "Spent"), ("n", "#")]
    assert r.table(rows, cols, numbered=False) == r.table(rows, cols)


def test_table_numbered_empty_rows():
    assert r.table([], [("cat", "Category")], numbered=True) == "(nothing to show)"


def test_bars_numbered_prefixes_ordinal():
    out = r.bars([("Groceries", -5000), ("Gas", -1200)], numbered=True)
    assert out == (
        "1. Groceries  " + "▇" * 20 + "  -$50.00 (81%)\n"
        "2. Gas  " + "▇" * 5 + "  -$12.00 (19%)"
    )


def test_bars_numbered_false_is_byte_identical_to_current_behavior():
    items = [("Groceries", -5000), ("Gas", -1200)]
    assert r.bars(items, numbered=False) == r.bars(items)


def test_table_drill_hint_present():
    out = r.table([{"cat": "Groceries"}], [("cat", "Category")], drill_hint="Reply with a row number.")
    assert out == (
        "| Category |\n"
        "| --- |\n"
        "| Groceries |\n"
        "\n"
        "_Reply with a row number._"
    )


def test_table_drill_hint_absent_is_byte_identical():
    rows = [{"cat": "Groceries"}]
    cols = [("cat", "Category")]
    assert r.table(rows, cols, drill_hint=None) == r.table(rows, cols)


def test_table_drill_hint_empty_string_appends_nothing():
    rows = [{"cat": "Groceries"}]
    cols = [("cat", "Category")]
    assert r.table(rows, cols, drill_hint="") == r.table(rows, cols)


def test_table_drill_hint_suppressed_on_empty_rows():
    assert r.table([], [("cat", "Category")], drill_hint="Reply with a row number.") == (
        "(nothing to show)"
    )


def test_bars_drill_hint_present():
    out = r.bars([("Groceries", -5000)], drill_hint="Reply with a row number.")
    assert out == (
        "Groceries  " + "▇" * 20 + "  -$50.00 (100%)\n"
        "\n"
        "_Reply with a row number._"
    )


def test_bars_drill_hint_absent_is_byte_identical():
    items = [("Groceries", -5000)]
    assert r.bars(items, drill_hint=None) == r.bars(items)


def test_bars_drill_hint_empty_string_appends_nothing():
    items = [("Groceries", -5000)]
    assert r.bars(items, drill_hint="") == r.bars(items)


def test_bars_drill_hint_suppressed_on_empty_items():
    assert r.bars([], drill_hint="Reply with a row number.") == ""
