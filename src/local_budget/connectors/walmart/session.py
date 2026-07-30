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
from pathlib import Path

from ... import paths


class WalmartAuthError(RuntimeError):
    """No captured session, or the captured one is no longer honoured."""


#: Cookie names that only exist on a signed-in walmart.com session. Used for a
#: CHEAP OFFLINE heuristic — "is it even worth trying" — never as proof. Whether
#: Walmart still honours the session is knowable only by making a request, which
#: `fetch` reports on. Any one of these present is enough; requiring all three
#: would turn a harmless cookie rename into "you are logged out".
AUTH_COOKIE_CANDIDATES = ("CID", "customer", "type", "hasCID")

WALMART_DOMAIN = "walmart.com"


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
    """Does the stored state hold an unexpired walmart.com auth cookie?

    Offline and cheap. Expiry IS checked — unlike the Amazon equivalent, which
    only tests for a cookie's presence — because Playwright records it and a
    session that has demonstrably lapsed should say so before opening a browser
    and walking into a login wall.
    """
    p = storage_state_path()
    if not p.exists():
        return False
    try:
        state = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    now = time.time() if now is None else now
    for c in _walmart_cookies(state):
        if c.get("name") not in AUTH_COOKIE_CANDIDATES:
            continue
        exp = c.get("expires")
        # -1 (or absent) marks a session cookie: no expiry to fail, and it is
        # live for as long as the state is. Only a real past timestamp is stale.
        if exp is None or exp == -1 or float(exp) > now:
            return True
    return False


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


def save_storage_state(state: dict) -> int:
    """Write the state 0600. Returns the number of walmart.com cookies kept.

    Third-party cookies are dropped: they are not ours to store, they are not
    needed to read an order page, and keeping them would widen what a leaked
    file is worth.
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
    path = storage_state_path()
    path.write_text(json.dumps(trimmed), encoding="utf-8")
    path.chmod(paths.FILE_MODE)
    return len(cookies)
