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
