"""Money is signed INTEGER CENTS everywhere. Never float dollars.

The one conversion entry point is `cents_from_amount_str`. It follows the
design's mandatory rule (§3/S5): parse the raw amount STRING with Decimal,
RAISE on anything carrying more than 2 decimal places (never silently round),
and never go through `int(float * 100)` (which loses pennies — `int(19.99*100)`
== 1998).
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

_CENTS = Decimal("0.01")
# Comma-only inputs are European cents ONLY when there is exactly one comma
# followed by exactly 2 trailing digits (e.g. "19,99"); else commas are
# US thousands separators ("1,234" -> 1234).
_EURO_CENTS = re.compile(r"^[+-]?\d{1,3}(?:\d*),\d{2}$")
# A plain decimal number — no exponent. Rejects scientific notation (M2).
_PLAIN_DECIMAL = re.compile(r"^[+-]?\d*\.?\d+$")


class AmountParseError(ValueError):
    """Raised when an amount string is malformed or carries sub-cent precision.

    Deliberately a hard error: an import that hits one fails all-or-nothing
    (§4.1) rather than silently rounding money.
    """


def cents_from_amount_str(raw: str | None) -> int:
    """Convert a raw OFX `TRNAMT`-style amount STRING to signed integer cents.

    - Negative = money out (spend); positive = money in. Sign is taken from the
      string itself.
    - More than 2 decimal places RAISES (we never silently round money).
    - Separator rules (US-correct; US-bank exports): if both `,` and `.` are present,
      `,` is a thousands separator. If only `,` is present, it is a decimal
      separator ONLY for European cents (exactly one comma + exactly 2 trailing
      digits, e.g. "19,99"); otherwise it is a thousands separator ("1,234" ->
      1234, "1,500" -> 1500).
    """
    if raw is None:
        raise AmountParseError("amount is required")
    s = str(raw).strip()
    if not s:
        raise AmountParseError("amount is empty")
    if "," in s and "." in s:
        s = s.replace(",", "")          # comma = thousands separator
    elif "," in s:
        if _EURO_CENTS.match(s):
            s = s.replace(",", ".")     # European cents (e.g. "19,99")
        else:
            s = s.replace(",", "")      # US thousands ("1,234" -> 1234)

    # Reject scientific notation and any non-plain-decimal form (M2).
    if not _PLAIN_DECIMAL.match(s):
        raise AmountParseError(f"unparseable amount: {raw!r}")

    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise AmountParseError(f"unparseable amount: {raw!r}") from e

    if not d.is_finite():
        raise AmountParseError(f"non-finite amount: {raw!r}")

    # >2 decimal places -> malformed for money; raise, do NOT round (S3/S5).
    if d.as_tuple().exponent < -2:
        raise AmountParseError(
            f"amount has more than 2 decimal places (refusing to round): {raw!r}"
        )

    # Exactly-<=2-decimal normal case: quantize is a no-op normalizer.
    # quantize raises InvalidOperation when the value exceeds the default
    # 28-digit decimal context precision (e.g. a 29-digit amount). That is a
    # malformed (out-of-range) amount, not a server fault: turn it into an
    # AmountParseError (a ValueError) so callers return a clean 400, not a 500.
    try:
        q = d.quantize(_CENTS, rounding=ROUND_HALF_EVEN)
        return int(q * 100)
    except InvalidOperation as e:
        raise AmountParseError(f"amount is out of range: {raw!r}") from e


def dollars(cents: int) -> str:
    """Format signed integer cents as a $ string for display."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}${whole:,}.{frac:02d}"
