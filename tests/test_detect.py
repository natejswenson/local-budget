"""Recurring + anomaly detection (design §4.5)."""
from __future__ import annotations

from local_budget import detect


def _row(date_, cents, merchant, cat="Random"):
    return {"posted_date": date_, "amount_cents": cents, "merchant_norm": merchant, "category": cat}


def test_recurring_detects_monthly_subscription():
    rows = [
        _row("2026-04-05", -1549, "NETFLIX"),
        _row("2026-05-05", -1549, "NETFLIX"),
        _row("2026-06-05", -1549, "NETFLIX"),
    ]
    rec = detect.find_recurring(rows)
    assert len(rec) == 1
    assert rec[0]["merchant"] == "NETFLIX"
    assert rec[0]["avg_amount_cents"] == 1549


def test_recurring_ignores_irregular():
    rows = [
        _row("2026-04-01", -1000, "RANDOM"),
        _row("2026-04-03", -1000, "RANDOM"),
        _row("2026-06-20", -1000, "RANDOM"),
    ]
    assert detect.find_recurring(rows) == []


def test_recurring_includes_monthly_variable_bill():
    # A monthly bill whose amount varies (e.g. electric) IS recurring.
    rows = [_row(f"2026-0{m}-05", -1000*m, "WILD RICE ELECTRIC") for m in (4, 5, 6)]
    assert any(r["merchant"] == "WILD RICE ELECTRIC" for r in detect.find_recurring(rows))


def test_recurring_excludes_high_frequency_retail():
    # Many varied charges per month at one merchant (shopping) is NOT recurring.
    rows = []
    for m in (4, 5, 6):
        for d in (3, 8, 14, 20, 26):
            rows.append(_row(f"2026-0{m}-{d:02d}", -1000-d, "WALMART"))
    assert detect.find_recurring(rows) == []


def test_anomaly_flags_outlier():
    rows = [
        _row("2026-06-01", -1000, "AMZN"),
        _row("2026-06-02", -1100, "AMZN"),
        _row("2026-06-03", -900, "AMZN"),
        _row("2026-06-04", -34000, "AMZN"),   # 30x the usual
    ]
    out = detect.find_anomalies(rows, sd_threshold=2.0)
    assert any(a["amount_cents"] == -34000 for a in out)


def test_anomaly_excludes_transfers_and_income():
    rows = [
        _row("2026-06-01", -1000, "X"),
        _row("2026-06-02", -1000, "X"),
        _row("2026-06-03", -1000, "X"),
        _row("2026-06-04", -99999, "X", cat="Transfer"),  # not spend -> not anomaly
    ]
    assert detect.find_anomalies(rows) == []
