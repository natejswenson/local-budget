"""Walmart session handling — a captured browser session, and nothing else.

Walmart publishes no consumer order API, so the only way to reach your own
order history programmatically is an authenticated browser session against the
consumer site. Consequences worth being explicit about, because they are
permanent properties of this connector and not bugs:

* Walmart's Terms of Use prohibit automated extraction. This is your own account
  and your own data, but it is a real term.
* Walmart's edge is bot-protected far more aggressively than Amazon's. Replaying
  cookies from a plain HTTP client gets challenged; requests have to come from a
  real browser, which is why `fetch.py` drives Playwright rather than `requests`.
* **The session is a credential.** A live Walmart session can place orders, so
  it is kept beside `budget.db` under the same at-rest posture (0700 dir / 0600
  file) rather than in a browser profile somewhere in `~`.

**There is deliberately no username/password path.** The Amazon connector has
one as a fallback; this does not, for two reasons. Walmart sign-in routinely
demands an emailed or texted one-time code that no stored secret can answer, so
a password flow would fail most of the time anyway — and a password that mostly
does not work is strictly worse than no password at all, because it is a
credential on disk earning nothing. A human signs in once in a real window and
we keep the resulting session.

Nothing here is imported at module scope by the rest of the app: a missing
Playwright install must degrade to "the Walmart commands don't work", never
"the budget CLI won't start".
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from ... import paths


class WalmartAuthError(RuntimeError):
    """No captured session, or the captured one is no longer honoured."""


class WalmartBlocked(RuntimeError):
    """Walmart's bot defence served a challenge instead of the page.

    A DIFFERENT failure from `WalmartAuthError`, and telling them apart matters
    more than it looks. Both render as "not the page we asked for", but the
    remedies are opposites: an expired session is fixed by signing in again,
    while a block is made WORSE by it — more traffic, more sign-in requests,
    from the address already being throttled. The only thing that fixes a block
    is waiting.

    Earned the hard way: replaying Walmart's own GraphQL endpoint returned 412s
    and escalated to a full PerimeterX interstitial. Hence the standing rule in
    `fetch.py` — navigate pages, never call the API directly.
    """


WALMART_DOMAIN = "walmart.com"

#: **There is no cookie that means "signed in".** This was checked, not assumed:
#: loading walmart.com/orders in a browser that has never signed in sets `ACID`,
#: `hasACID`, `AID` and forty others — the same names a signed-in session
#: carries. An earlier version of this module treated `ACID`-style names as
#: evidence and was wrong about every anonymous visit.
#:
#: So authentication is established ONE way only: ask for the orders page and
#: look at what came back (`browser_login.signed_in`). What this module can do
#: offline is remember that a verified capture happened, and when.
CAPTURED_AT = "captured_at"

#: How long a captured session is assumed worth trying. Not Walmart's real
#: expiry, which is Walmart's business and unknowable from here — just the point
#: past which "run login again" is better advice than a browser launch that ends
#: at a sign-in wall.
SESSION_MAX_AGE_DAYS = 30


def walmart_dir() -> Path:
    """`data/walmart/`, created 0700. Sibling of budget.db on purpose."""
    d = paths.data_dir() / "walmart"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(paths.DIR_MODE)
    return d


def storage_state_path() -> Path:
    """Playwright's full storage state — cookies plus per-origin localStorage.

    The whole state, not just cookies: Walmart's app keeps part of its session
    context in localStorage, and a cookie-only restore lands on a page that is
    authenticated but behaves as though it is not.
    """
    return walmart_dir() / "storage_state.json"


def capture_dir() -> Path:
    """Where `budget walmart capture` writes raw page payloads.

    Under `data/`, which is gitignored wholesale — these dumps contain real
    order contents and must never be committable.
    """
    d = walmart_dir() / "capture"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(paths.DIR_MODE)
    return d


def harden() -> None:
    """0600 whatever was written. Called after every session operation."""
    p = storage_state_path()
    if p.exists():
        p.chmod(paths.FILE_MODE)


def _walmart_cookies(state: dict) -> list[dict]:
    return [c for c in (state.get("cookies") or [])
            if WALMART_DOMAIN in (c.get("domain") or "")]


def stored_session_looks_valid(now: float | None = None) -> bool:
    """Is there a state file worth opening a browser for?

    Deliberately weak, and named for what it can actually claim. It asks three
    things: is there a capture, did it come from a run that VERIFIED itself
    against the orders page (the `captured_at` stamp is only written on that
    path), and is it younger than `SESSION_MAX_AGE_DAYS`.

    It cannot tell you the session still works. Nothing offline can — see the
    note on `CAPTURED_AT`. Callers that need to know make a request.
    """
    state = load_storage_state()
    if not state or not _walmart_cookies(state):
        return False
    stamp = state.get(CAPTURED_AT)
    if not stamp:
        # A jar with no stamp predates content verification, so it may well be
        # an anonymous browsing session that an earlier bug called a login.
        return False
    try:
        age = (time.time() if now is None else now) - datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return False
    return age < SESSION_MAX_AGE_DAYS * 86400


def load_storage_state() -> dict | None:
    """The captured state, or None if there isn't a usable one."""
    p = storage_state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def require_session() -> dict:
    """The captured state, or a message with the next action in it."""
    state = load_storage_state()
    if state is None or not _walmart_cookies(state):
        raise WalmartAuthError(
            "no saved Walmart session.\nRun `budget walmart login` to sign in "
            "through a browser window — nothing is stored but the session "
            "itself, and no password is kept.")
    return state


def save_storage_state(state: dict, *, verified: bool = False) -> int:
    """Write the state 0600. Returns the number of walmart.com cookies kept.

    Third-party cookies are dropped: they are not ours to store, they are not
    needed to read an order page, and keeping them would widen what a leaked
    file is worth.

    `verified=True` stamps `captured_at`, and ONLY the login path that has read
    a signed-in orders page may pass it. That stamp is the whole basis on which
    `stored_session_looks_valid` later trusts the file, so writing it from
    anywhere else would relaunch the bug it exists to close.
    """
    cookies = _walmart_cookies(state)
    if not cookies:
        raise WalmartAuthError(
            "the captured session has no walmart.com cookies — not signed in. "
            "Make sure you reached your account before the window closed.")
    trimmed = {
        "cookies": cookies,
        "origins": [o for o in (state.get("origins") or [])
                    if WALMART_DOMAIN in (o.get("origin") or "")],
    }
    if verified:
        trimmed[CAPTURED_AT] = datetime.now().isoformat(timespec="seconds")
    path = storage_state_path()
    path.write_text(json.dumps(trimmed), encoding="utf-8")
    path.chmod(paths.FILE_MODE)
    return len(cookies)
