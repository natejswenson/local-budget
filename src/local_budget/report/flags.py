"""Flags-section business rules, moved out of skill prose into tested code.

These rules previously lived only in budget-visualizer/SKILL.md (~100 lines
of natural language the model had to re-execute correctly per render). The
semantics are unchanged — see that skill's fallback appendix for the original
wording and rationale.
"""
from __future__ import annotations

# Bill-like categories whose recurring charges are worth flagging: genuine
# discretionary subscriptions and large fixed obligations. Essential fixed
# utilities (Phone, Electricity, Gas/Propane, Internet, Sewer/Water/Trash)
# are deliberately excluded — they recur by nature and aren't something the
# user would reconsider, so flagging them is noise. NY529 is the user's own
# custom category, named explicitly (a custom category's meaning can't be
# derived generically).
BILL_LIKE_CATEGORIES = frozenset({"Subscriptions", "Insurance", "Housing", "NY529"})

# Statement descriptors drift for a few known merchants. Matching is
# exact-string only (substring/fuzzy matching is unsafe — "FUCHS SANITATION"
# vs "FUCHS SANITATION S" are distinct merchants), so each drifting merchant
# needs an explicit alias entry: {recurring_charges key: {other exact names}}.
MERCHANT_ALIASES: dict[str, frozenset[str]] = {
    "CLAUDE.AI SUBSCRIP ANTHROPIC.COM": frozenset(
        {"ANTHROPIC CLAUDE ANTHROPIC.COM", "PURCHASE ANTHROPIC C"}),
    "HLU HULU.COM BILL": frozenset({"HULU"}),
}

# sanitize.merchant_norm()'s only fallback value — can't be attributed to one
# recurring merchant, so it never participates in cross-referencing.
UNKNOWN_MERCHANT = "UNKNOWN"


def month_anomalies(anomalies: list[dict], month: str,
                    recurring: list[dict]) -> list[dict]:
    """find_anomalies rows scoped to `month`, minus known-recurring merchants.

    A merchant recurring_charges already recognizes as a stable pattern
    showing up again as "unusual" is a confusing double-flag, not an anomaly
    (a bill whose amount ticks up slightly can trip the statistical threshold
    while being entirely expected). The exclusion uses the FULL recurring
    list, not just the bill-like allowlist.
    """
    recurring_merchants = {r["merchant"] for r in recurring}
    return [
        a for a in anomalies
        if str(a.get("posted_date", "")).startswith(f"{month}-")
        and a.get("merchant") not in recurring_merchants
    ]


def _matches(merchant_norm: str, recurring_key: str) -> bool:
    if merchant_norm == UNKNOWN_MERCHANT:
        return False
    return (merchant_norm == recurring_key
            or merchant_norm in MERCHANT_ALIASES.get(recurring_key, frozenset()))


def month_recurring(recurring: list[dict], txns: list[dict],
                    month: str) -> list[dict]:
    """recurring_charges cross-referenced to the reported month's transactions.

    recurring_charges is one aggregate row per merchant (global avg/last_date)
    and cannot be month-filtered directly, so each merchant is matched against
    the month's transactions. A txn qualifies iff: exact merchant match (or
    curated alias), amount_cents < 0, and its category is in the bill-like
    allowlist. The displayed date/amount come from the single most recent
    qualifying txn (first-in-returned-order on a tie — amounts are never
    summed); "months seen" stays the recurring row's global figure.
    Returns [{merchant, amount_cents, posted_date, months}] sorted by months
    seen desc (recurring_charges' own order).
    """
    out = []
    for rec in recurring:
        matches = [
            t for t in txns
            if _matches(t.get("merchant_norm") or "", rec["merchant"])
            and str(t.get("posted_date", "")).startswith(f"{month}-")
            and int(t.get("amount_cents") or 0) < 0
            and t.get("category") in BILL_LIKE_CATEGORIES
        ]
        if not matches:
            continue
        latest_date = max(str(t["posted_date"]) for t in matches)
        shown = next(t for t in matches if str(t["posted_date"]) == latest_date)
        out.append({"merchant": rec["merchant"],
                    "amount_cents": int(shown["amount_cents"]),
                    "posted_date": str(shown["posted_date"]),
                    "months": rec["months"]})
    return out
