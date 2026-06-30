"""Money-cents conversion (design §3/S5/S3, invariant I1)."""
from __future__ import annotations

import pytest

from local_budget.money import AmountParseError, cents_from_amount_str, dollars


def test_basic_two_decimals():
    assert cents_from_amount_str("19.99") == 1999


def test_no_float_penny_loss():
    # The footgun the rule exists to prevent: int(19.99 * 100) == 1998.
    assert cents_from_amount_str("19.99") == 1999
    assert int(19.99 * 100) == 1998  # documents why we never do this


def test_addition_is_exact():
    assert cents_from_amount_str("0.10") + cents_from_amount_str("0.20") == 30


def test_negative_spend():
    assert cents_from_amount_str("-73.20") == -7320


def test_integer_amount():
    assert cents_from_amount_str("20") == 2000


def test_one_decimal():
    assert cents_from_amount_str("19.9") == 1990


def test_more_than_two_decimals_raises():
    # MUST raise, never silently round 19.999 -> 20.00 (S3).
    with pytest.raises(AmountParseError):
        cents_from_amount_str("19.999")


def test_sub_cent_raises():
    with pytest.raises(AmountParseError):
        cents_from_amount_str("1.005")


def test_malformed_raises():
    for bad in ("", "  ", "abc", "$5.00", None):
        with pytest.raises(AmountParseError):
            cents_from_amount_str(bad)


def test_european_decimal_comma():
    assert cents_from_amount_str("19,99") == 1999


def test_thousands_separator():
    assert cents_from_amount_str("1,234.56") == 123456


def test_comma_only_thousands_us():
    # US thousands grouping with no decimal part: comma is a thousands separator.
    assert cents_from_amount_str("1,234") == 123400
    assert cents_from_amount_str("1,500") == 150000


def test_scientific_notation_raises():
    # M2: never accept exponent form.
    with pytest.raises(AmountParseError):
        cents_from_amount_str("1e3")


def test_oversized_amount_raises_not_invalid_operation():
    # A >=29-digit amount exceeds the default 28-digit decimal context, so
    # quantize would raise decimal.InvalidOperation. That must surface as an
    # AmountParseError (a ValueError) -> clean 400, never a 500.
    with pytest.raises(AmountParseError):
        cents_from_amount_str("9" * 29)
    with pytest.raises(AmountParseError):
        cents_from_amount_str("9" * 29 + ".99")


def test_large_but_sane_amount_still_parses():
    # A big-but-in-range amount must still parse to correct cents.
    assert cents_from_amount_str("9999999.99") == 999999999


def test_dollars_formatting():
    assert dollars(123456) == "$1,234.56"
    assert dollars(-7320) == "-$73.20"
    assert dollars(5) == "$0.05"
