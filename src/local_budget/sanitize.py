"""Parse-time PII sanitization (design §7.6 / S7 / I14).

Two jobs:
  - `redact_account_numbers`: replace any ≥7-digit run — AFTER collapsing ANY
    non-alphanumeric separators (space, dash, `/`, `.`, …), so
    `4111 1111 1111 1111`, `1234-5678-9012`, `4111/2222/3333/4444` and
    `9.876.543.210` are all caught — with `[REDACTED]`. Used on `raw_ofx`.
  - `merchant_norm`: derive the agent-visible normalized merchant string. It is
    redacted of ≥7-digit runs (so it can never carry an account/card/routing
    number) and best-effort stripped of P2P/Zelle counterparty names.

Honest scope (I14): this guarantees NO ≥7-digit numeric run reaches the agent —
NOT "no PII". A short 5-6 digit fragment or a counterparty name is a bounded,
accepted residual (the agent runs on the user's own subscription).
"""
from __future__ import annotations

import re

# A maximal run of digits possibly joined by ANY non-alphanumeric separators
# (space, dash, /, ., *, #, _ …) — but NEVER bridging across letters. The inner
# group is anchored by a required \d, so there is no catastrophic backtracking.
_DIGIT_RUN = re.compile(r"\d(?:[^0-9A-Za-z]*\d)+|\d")
# Strip a P2P/Zelle/Venmo/CashApp clause and everything after it (the
# counterparty name typically follows the keyword).
_P2P = re.compile(r"\b(ZELLE|VENMO|CASH\s*APP|CASHAPP|PAYMENT\s+TO|PAYMENT\s+FROM)\b.*", re.IGNORECASE)
_WS = re.compile(r"\s+")

REDACTED = "[REDACTED]"


def redact_account_numbers(text: str | None) -> str | None:
    """Replace every ≥7-digit run (after separator-collapse) with [REDACTED]."""
    if not text:
        return text

    def _repl(m: re.Match) -> str:
        chunk = m.group(0)
        ndigits = sum(c.isdigit() for c in chunk)
        return REDACTED if ndigits >= 7 else chunk

    return _DIGIT_RUN.sub(_repl, text)


def strip_p2p_names(text: str) -> str:
    """Best-effort removal of P2P/Zelle counterparty names (not guaranteed)."""
    return _P2P.sub("", text)


# Wells Fargo transaction boilerplate, stripped so the same store collapses to
# one merchant key instead of a unique string per transaction.
# Date may be MM/DD, MM/DD YYYY, MM/DD/YYYY — and a "MM/DD YYYY" date gets
# bridged into a ≥7-digit run by redaction, so also accept a redacted date.
_WF_PREFIX = re.compile(
    r"^.*?\bAUTHORIZED ON\s+(?:\d{1,2}/\d{1,2}(?:[ /]\d{2,4})?|\[REDACTED\])\s*", re.IGNORECASE)
_WF_TAIL = re.compile(r"\s+(?:[SP]\d{3,}|[SP]\[REDACTED\]|\[REDACTED\]|CARD\s+\d+).*$", re.IGNORECASE)
# Leading third-party PAYMENT-PROCESSOR code that prefixes the REAL merchant name
# (Square "SQ *COFFEE SHOP", Toast "TST* CHIPOTLE", PayPal "PAYPAL *MERCH",
# "GOOGLE *YOUTUBE", …). The same charge can appear with or without this prefix
# across two downloads, so without stripping it the store-suffix rule below mangles
# "SQ *COFFEE SHOP" → "SQ SHOP" while a plain "COFFEE SHOP" stays "COFFEE SHOP" —
# a different first token → a SILENT double-count on overlapping re-downloads
# (red-team M11-1). Stripping the prefix collapses both to "COFFEE SHOP" so exact
# dedup catches the re-download. A KNOWN processor list (not a generic `XX*`) avoids
# false-stripping real merchant brands like "AMZN*"/"BRAND*PRODUCT".
_PROCESSOR_PREFIX = re.compile(
    r"^(?:SQ|SQU|TST|PP|PYPL|PAYPAL|GOOGLE|GOOGL|WPY|CLV|CKE|SP|EZP|WU|TSYS|IZ)\s*\*\s*")
_STORE_SUFFIX = re.compile(r"[#*]\S*")              # store / auth suffix: #520, *NK07Q
_NONWORD = re.compile(r"[^A-Z0-9&'. ]")
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


def merchant_norm(payee: str | None, memo: str | None = None) -> str:
    """Derive the agent-visible normalized MERCHANT KEY.

    Redacts ≥7-digit runs (I14) and P2P names, then strips Wells Fargo
    transaction boilerplate (`PURCHASE AUTHORIZED ON MM/DD …`, trailing
    `CARD ####`/auth codes, store numbers, a trailing state code) and keeps the
    leading merchant words — so every "Walmart.com" or "Amazon" transaction maps
    to ONE key instead of a unique per-transaction string. This is what lets
    categorization group merchants and rules generalize.
    """
    base = redact_account_numbers((payee or memo or "").strip()) or ""
    base = strip_p2p_names(base)
    s = _WS.sub(" ", base).strip().upper()
    if not s:
        return "UNKNOWN"

    # Strip WF boilerplate. The prefix regex accepts a redacted date too, so a
    # "MM/DD YYYY" date that redaction bridged into [REDACTED] is still removed
    # (otherwise the tail-strip would eat the merchant — the "PURCHASE AUTHORIZED
    # ON" bug).
    s = _WF_PREFIX.sub("", s)        # drop "PURCHASE AUTHORIZED ON 06/03 [2026] "
    s = _PROCESSOR_PREFIX.sub("", s) # drop a leading "SQ *"/"TST*"/"PAYPAL *" so the
                                     # real merchant survives the store-suffix strip
                                     # (red-team M11-1 — kills the processor-prefix
                                     # silent-double-count residual)
    s = _WF_TAIL.sub("", s)          # drop trailing " [REDACTED]/S304/CARD 1840 …"
    s = _STORE_SUFFIX.sub(" ", s)    # drop "#520" / "*NK07Q"
    s = _NONWORD.sub(" ", s)
    words = [w for w in s.split() if w]
    if len(words) > 1 and words[-1] in _US_STATES:
        words = words[:-1]          # drop a trailing state code
    key = " ".join(words[:3]).strip()
    return key or "UNKNOWN"


def has_long_digit_run(text: str | None) -> bool:
    """True if `text` still contains a ≥7-digit run after separator-collapse.

    Used by the security test net (I14) to assert agent-visible values are clean.
    """
    if not text:
        return False
    for m in _DIGIT_RUN.finditer(text):
        if sum(c.isdigit() for c in m.group(0)) >= 7:
            return True
    return False
