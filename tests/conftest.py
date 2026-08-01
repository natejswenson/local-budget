"""Shared fixtures — hermetic temp data dir + the no-network-egress guard (I2)."""
from __future__ import annotations

import socket

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the app at a hermetic temp data dir (mirrors local-fitness)."""
    d = tmp_path / "data"
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def no_network_egress(monkeypatch):
    """Regression guard (I2/S3): deterministic-path tests open no socket.

    A test-time guard, not a proof of production behavior. Agent/network tests
    (none in the deterministic core) would opt out explicitly.
    """
    def _blocked(*args, **kwargs):
        raise RuntimeError("network egress blocked in deterministic tests (I2)")

    monkeypatch.setattr(socket, "socket", _blocked)


@pytest.fixture
def clock_july_2026(monkeypatch):
    """Pin "now" to 2026-07 for tests that assert an exact budget factor.

    `_budget_window("all")` spans [first_data_month, current_month - 1], so its
    whole-month `factor` grows by one every time the calendar rolls over. A test
    whose fixture posts a June 2026 transaction and asserts "$100 short" is
    really asserting "factor == 1", which is only true while today is July 2026 —
    it passed for a month and then started failing on the 1st, in CI (UTC) before
    it failed locally.

    Freezing the seam makes these tests about the logic instead of about the date
    they were written. `current_month` is the single place production reads the
    clock, so patching it here covers the whole window calculation.
    """
    from local_budget import reports
    monkeypatch.setattr(reports, "current_month", lambda: "2026-07")
