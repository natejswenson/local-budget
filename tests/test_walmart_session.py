"""The session layer and the login gates — the pure parts, offline.

Written after the first live `budget walmart login` timed out at 300s having
never once asked whether the sign-in had worked. The cause was treating a GUESS
about Walmart's cookie names as a precondition for checking: miss the guess, and
a perfectly good session is never confirmed. Everything here exists to keep that
class of failure from returning quietly.
"""
from __future__ import annotations

import json
import time

import pytest

from local_budget.connectors.walmart import browser_login, session


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))


def _state(*cookies, origins=None):
    return {"cookies": list(cookies), "origins": origins or []}


def _cookie(name, domain=".walmart.com", expires=-1):
    return {"name": name, "value": "v", "domain": domain, "path": "/",
            "expires": expires}


# ── the login gates ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,ok", [
    ("https://www.walmart.com/orders", True),
    ("https://www.walmart.com/orders/2000123", True),
    ("https://www.walmart.com/account/login?returnUrl=/orders", False),
    ("https://www.walmart.com/", False),
])
def test_only_actually_reaching_the_orders_page_counts_as_signed_in(url, ok):
    """Cookies are set long before sign-in completes. Loading the orders page is
    the only test that cannot pass while logged out."""
    assert browser_login.signed_in(url) is ok


def test_the_cookie_hint_is_an_accelerator_not_a_requirement():
    """The regression. `looks_done` returning False must NOT mean "never check" —
    the forced probe interval exists precisely so an unrecognised cookie name
    costs 30 seconds rather than the whole session."""
    assert browser_login.looks_done("https://www.walmart.com/", {"CID"}) is True
    assert browser_login.looks_done("https://www.walmart.com/", {"whatever"}) is False
    assert browser_login.FORCED_PROBE_SECONDS > 0
    assert browser_login.FORCED_PROBE_SECONDS < browser_login.DEFAULT_TIMEOUT


def test_the_hint_never_fires_while_still_on_a_sign_in_page():
    """Probing is cheap but not free; there is nothing to confirm yet."""
    for p in browser_login.LOGIN_PATHS:
        assert browser_login.looks_done(f"https://www.walmart.com{p}", {"CID"}) is False


def test_the_timeout_is_long_enough_for_a_texted_code():
    """300s was not: a code by SMS, a challenge and a slow page ate it."""
    assert browser_login.DEFAULT_TIMEOUT >= 600


# ── what gets written, and how ───────────────────────────────────────────────
def test_only_walmart_cookies_are_kept(tmp_path):
    """Third-party cookies are not ours to store, are not needed to read an
    order page, and would widen what a leaked file is worth."""
    n = session.save_storage_state(_state(
        _cookie("CID"), _cookie("_ga", domain=".doubleclick.net")))
    assert n == 1
    written = json.loads(session.storage_state_path().read_text())
    assert [c["name"] for c in written["cookies"]] == ["CID"]


def test_local_storage_is_kept_for_walmart_origins_only():
    """Walmart's app keeps part of its session context in localStorage, and a
    cookie-only restore lands on a page that is authenticated but behaves as
    though it is not."""
    session.save_storage_state(_state(_cookie("CID"), origins=[
        {"origin": "https://www.walmart.com", "localStorage": [{"name": "k", "value": "v"}]},
        {"origin": "https://ads.example.com", "localStorage": [{"name": "x", "value": "y"}]},
    ]))
    written = json.loads(session.storage_state_path().read_text())
    assert [o["origin"] for o in written["origins"]] == ["https://www.walmart.com"]


def test_the_session_file_is_written_0600():
    """A live Walmart session can place orders. Same at-rest posture as
    budget.db, not whatever the umask happens to be."""
    session.save_storage_state(_state(_cookie("CID")))
    assert session.storage_state_path().stat().st_mode & 0o777 == 0o600
    assert session.walmart_dir().stat().st_mode & 0o777 == 0o700


def test_a_state_with_no_walmart_cookies_is_refused():
    with pytest.raises(session.WalmartAuthError, match="not signed in"):
        session.save_storage_state(_state(_cookie("x", domain=".example.com")))


# ── the offline validity heuristic ───────────────────────────────────────────
def test_no_file_means_no_session():
    assert session.stored_session_looks_valid() is False


def test_a_session_cookie_with_no_expiry_is_live():
    session.save_storage_state(_state(_cookie("CID", expires=-1)))
    assert session.stored_session_looks_valid() is True


def test_an_expired_cookie_is_not_a_session():
    """Checked offline, unlike the Amazon equivalent: a session that has
    demonstrably lapsed should say so before opening a browser and walking into
    a login wall."""
    session.save_storage_state(_state(_cookie("CID", expires=time.time() - 60)))
    assert session.stored_session_looks_valid() is False


def test_an_unexpired_cookie_is_a_session():
    session.save_storage_state(_state(_cookie("CID", expires=time.time() + 8640)))
    assert session.stored_session_looks_valid() is True


def test_a_corrupt_state_file_is_not_a_session_and_does_not_raise():
    session.storage_state_path().write_text("{ not json")
    assert session.stored_session_looks_valid() is False
    assert session.load_storage_state() is None


def test_require_session_names_the_command_that_fixes_it():
    with pytest.raises(session.WalmartAuthError, match="budget walmart login"):
        session.require_session()


def test_capture_output_lives_under_the_gitignored_data_dir():
    """Those dumps are real order contents. Anywhere else and they are one
    `git add -A` away from being committed."""
    d = session.capture_dir()
    assert d.parent == session.walmart_dir()
    assert d.stat().st_mode & 0o777 == 0o700
