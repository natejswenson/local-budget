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


# ── the login gate ───────────────────────────────────────────────────────────
#: Copy taken verbatim from a real anonymous fetch of walmart.com/orders.
GUEST_PAGE = """<html><title>Manage Account - Track your order - Walmart.com</title>
<main data-testid="maincontent">Track your order. If you don't have an account
yet, you can still track your order status. Email address Order number
View order status. Sign in to do more with your account.</main></html>"""

SIGNED_IN_PAGE = """<html><title>Purchase history</title>
<main data-testid="maincontent">Purchase history. Jul 20 order. $149.50</main></html>"""


def test_the_guest_wall_is_never_mistaken_for_an_order_history():
    """THE bug this file exists for. Walmart does not redirect a logged-out
    visitor away from /orders — it serves a 200 at that exact URL with a guest
    tracking form. A URL-only test called that success, closed the window about
    two seconds after opening it, and saved an anonymous browsing session."""
    assert browser_login.signed_in("https://www.walmart.com/orders", GUEST_PAGE) is False


def test_a_real_order_history_is_recognised():
    assert browser_login.signed_in("https://www.walmart.com/orders", SIGNED_IN_PAGE) is True


@pytest.mark.parametrize("url", [
    "https://www.walmart.com/account/login?returnUrl=/orders",
    "https://www.walmart.com/",
    "https://www.walmart.com/account",
])
def test_being_somewhere_other_than_the_orders_page_is_not_a_session(url):
    assert browser_login.signed_in(url, SIGNED_IN_PAGE) is False


def test_no_cookie_name_is_treated_as_evidence_of_a_session():
    """Checked, not assumed: a browser that has NEVER signed in comes back from
    /orders holding ACID, hasACID and AID. Any module-level list of "auth
    cookies" here is a bug waiting to be re-found."""
    assert not hasattr(browser_login, "AUTH_COOKIE_CANDIDATES")
    assert not hasattr(session, "AUTH_COOKIE_CANDIDATES")


def test_the_timeout_is_long_enough_for_a_texted_code():
    """300s was not: a code by SMS, a challenge and a slow page ate it."""
    assert browser_login.DEFAULT_TIMEOUT >= 600


# ── what gets written, and how ───────────────────────────────────────────────
def test_only_a_verified_capture_is_stamped():
    """The stamp is the entire basis on which a later run trusts the file, so
    only the path that has READ a signed-in orders page may write it."""
    session.save_storage_state(_state(_cookie("ACID")))
    assert session.CAPTURED_AT not in json.loads(
        session.storage_state_path().read_text())
    session.save_storage_state(_state(_cookie("ACID")), verified=True)
    assert session.CAPTURED_AT in json.loads(
        session.storage_state_path().read_text())


def test_an_unstamped_jar_is_not_treated_as_a_session():
    """It may well be the anonymous browsing session an earlier bug saved."""
    session.save_storage_state(_state(_cookie("ACID")))
    assert session.stored_session_looks_valid() is False


def test_a_verified_capture_is_a_session():
    session.save_storage_state(_state(_cookie("ACID")), verified=True)
    assert session.stored_session_looks_valid() is True


def test_a_capture_older_than_the_max_age_is_not_worth_a_browser_launch():
    session.save_storage_state(_state(_cookie("ACID")), verified=True)
    later = time.time() + (session.SESSION_MAX_AGE_DAYS + 1) * 86400
    assert session.stored_session_looks_valid(now=later) is False


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
