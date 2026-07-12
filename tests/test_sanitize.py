"""PII sanitization (design §7.6/S7, invariant I14)."""
from __future__ import annotations

from local_budget import sanitize


def test_redacts_unbroken_long_run():
    assert "[REDACTED]" in sanitize.redact_account_numbers("ACCT 4111222233334444 OK")


def test_redacts_spaced_card_number():
    out = sanitize.redact_account_numbers("CARD 4111 1111 1111 1111 END")
    assert "4111" not in out and "[REDACTED]" in out


def test_redacts_dashed_number():
    out = sanitize.redact_account_numbers("REF 1234-5678-9012 DONE")
    assert "[REDACTED]" in out


def test_redacts_slash_separated_number():
    out = sanitize.redact_account_numbers("CARD 4111/2222/3333/4444 END")
    assert "4111" not in out and "[REDACTED]" in out


def test_redacts_dot_separated_number():
    out = sanitize.redact_account_numbers("ROUTE 9.876.543.210 DONE")
    assert "9.876" not in out and "[REDACTED]" in out


def test_keeps_short_numbers():
    # A store number (<7 digits) is kept; letters between groups are not bridged.
    assert sanitize.redact_account_numbers("STORE 1234 LANE 5") == "STORE 1234 LANE 5"


def test_merchant_norm_has_no_long_run():
    m = sanitize.merchant_norm("ZELLE 4111 1111 1111 1111 PAYMENT")
    assert not sanitize.has_long_digit_run(m)


def test_merchant_norm_redacts_slash_separated():
    m = sanitize.merchant_norm("PMT 4111/2222/3333/4444")
    assert not sanitize.has_long_digit_run(m)
    assert "4111" not in m


def test_merchant_norm_redacts_dot_separated():
    m = sanitize.merchant_norm("ACH 9.876.543.210")
    assert not sanitize.has_long_digit_run(m)
    assert "9.876" not in m


def test_merchant_norm_strips_p2p_name():
    m = sanitize.merchant_norm("ZELLE TO JANE DOE")
    assert "JANE DOE" not in m


def test_merchant_norm_uppercases_and_collapses():
    assert sanitize.merchant_norm("  walmart   store ") == "WALMART STORE"


def test_merchant_norm_empty_is_unknown():
    assert sanitize.merchant_norm(None, None) == "UNKNOWN"


def test_has_long_digit_run_detects_spaced():
    assert sanitize.has_long_digit_run("4111 1111 1111 1111")
    assert not sanitize.has_long_digit_run("12 34 56")


def test_has_long_digit_run_detects_slash_and_dot():
    assert sanitize.has_long_digit_run("4111/2222/3333/4444")
    assert sanitize.has_long_digit_run("9.876.543.210")
    # Short groups separated by letters are NOT bridged.
    assert not sanitize.has_long_digit_run("STORE 1234 LANE 5")


def test_merchant_norm_strips_bank_boilerplate():
    # All these bank descriptions for the same store must collapse to one key.
    a = sanitize.merchant_norm("PURCHASE AUTHORIZED ON 10/31 WALMART.COM 8009666546 AR S304 CARD 1840")
    b = sanitize.merchant_norm("PURCHASE AUTHORIZED ON 02/23 WALMART.COM 8009666546 AR S771 CARD 1840")
    assert a == b == "WALMART.COM"


def test_merchant_norm_drops_card_and_state_and_storenum():
    m = sanitize.merchant_norm("PURCHASE AUTHORIZED ON 05/30 POTBELLY #520 SIOUX FALLS SD S123 CARD 1189")
    assert "CARD" not in m and "1189" not in m and "#520" not in m
    assert m.startswith("POTBELLY")


def test_merchant_norm_cash_back_prefix():
    m = sanitize.merchant_norm("PURCHASE WITH CASH BACK $ 20.00 AUTHORIZED ON 09/06 EXAMPLE MARKET ANYTOWN ST P12 CARD 4666")
    assert m.startswith("EXAMPLE")
    assert "AUTHORIZED" not in m and "CARD" not in m


def test_merchant_norm_strips_payment_processor_prefix():
    # The SAME charge can appear with or without a payment-processor prefix across
    # two downloads; both must collapse to the SAME key so exact dedup catches the
    # re-download instead of silently double-counting (red-team M11-1).
    plain = sanitize.merchant_norm("COFFEE SHOP")
    for variant in ("SQ *COFFEE SHOP", "TST* COFFEE SHOP", "PAYPAL *COFFEE SHOP", "SP *COFFEE SHOP"):
        assert sanitize.merchant_norm(variant) == plain == "COFFEE SHOP"
    assert sanitize.merchant_norm("GOOGLE *YOUTUBE") == "YOUTUBE"


def test_merchant_norm_does_not_strip_real_merchant_brands():
    # Only KNOWN third-party processors are stripped — a real merchant brand whose
    # name precedes a '*' (Amazon, or a mid-string auth '*code') is preserved.
    assert sanitize.merchant_norm("AMZN MKTP US*1A2B3C") == "AMZN MKTP US"
    assert sanitize.merchant_norm("AMAZON.COM*XK4Q9") == "AMAZON.COM"
