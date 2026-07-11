"""flags.py — the table-driven version of the rules that lived in skill prose."""
from __future__ import annotations

from local_budget.report import flags


def _rec(merchant, months=6):
    return {"merchant": merchant, "months": months, "avg_amount_cents": 1500,
            "last_date": "2026-05-05", "occurrences": 6, "stable": True}


def _txn(merchant, cents, date="2026-06-05", category="Subscriptions"):
    return {"merchant_norm": merchant, "amount_cents": cents,
            "posted_date": date, "category": category}


# ── month_anomalies ───────────────────────────────────────────────────────────
def test_anomalies_scoped_to_month_and_recurring_excluded():
    anomalies = [
        {"merchant": "COSTCO", "posted_date": "2026-06-10", "amount_cents": -90000},
        {"merchant": "COSTCO", "posted_date": "2025-01-10", "amount_cents": -80000},
        {"merchant": "NETFLIX", "posted_date": "2026-06-12", "amount_cents": -3000},
    ]
    out = flags.month_anomalies(anomalies, "2026-06", [_rec("NETFLIX")])
    assert out == [anomalies[0]]          # other-month + known-recurring dropped


def test_anomalies_recurring_exclusion_uses_full_list_not_allowlist():
    # Exclusion keys on the FULL recurring list even for a merchant whose
    # category would never pass the bill-like allowlist.
    anomalies = [{"merchant": "SHELL", "posted_date": "2026-06-02", "amount_cents": -9000}]
    assert flags.month_anomalies(anomalies, "2026-06", [_rec("SHELL")]) == []


# ── month_recurring ───────────────────────────────────────────────────────────
def test_recurring_cross_reference_exact_match_only():
    txns = [_txn("FUCHS SANITATION S", -4500)]     # near-miss, NOT a match
    assert flags.month_recurring([_rec("FUCHS SANITATION")], txns, "2026-06") == []


def test_recurring_alias_matches_and_shows_month_figures():
    txns = [_txn("ANTHROPIC CLAUDE ANTHROPIC.COM", -2000, "2026-06-14")]
    out = flags.month_recurring([_rec("CLAUDE.AI SUBSCRIP ANTHROPIC.COM")], txns, "2026-06")
    assert out == [{"merchant": "CLAUDE.AI SUBSCRIP ANTHROPIC.COM",
                    "amount_cents": -2000, "posted_date": "2026-06-14", "months": 6}]


def test_recurring_requires_billlike_category_and_negative_amount():
    rec = [_rec("NETFLIX")]
    assert flags.month_recurring(rec, [_txn("NETFLIX", -1500, category="Shopping")], "2026-06") == []
    assert flags.month_recurring(rec, [_txn("NETFLIX", 1500)], "2026-06") == []   # refund
    assert flags.month_recurring(rec, [_txn("NETFLIX", -1500, category="NY529")], "2026-06")


def test_recurring_unknown_placeholder_never_matches():
    assert flags.month_recurring([_rec("UNKNOWN")], [_txn("UNKNOWN", -1500)], "2026-06") == []


def test_recurring_latest_txn_wins_no_summing():
    txns = [_txn("NETFLIX", -1500, "2026-06-05"), _txn("NETFLIX", -1600, "2026-06-20"),
            _txn("NETFLIX", -1700, "2026-06-20")]  # tie on latest date
    out = flags.month_recurring([_rec("NETFLIX")], txns, "2026-06")
    assert len(out) == 1
    # tie → first in returned order, never a sum
    assert out[0]["amount_cents"] == -1600 and out[0]["posted_date"] == "2026-06-20"


def test_recurring_merchant_absent_this_month_is_omitted():
    assert flags.month_recurring([_rec("NETFLIX")], [_txn("NETFLIX", -1500, "2026-05-05")],
                                 "2026-06") == []
