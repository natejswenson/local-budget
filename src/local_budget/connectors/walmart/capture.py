"""Diagnostic: dump what Walmart's order pages actually serve.

This exists because there is no upstream library to inherit assumptions from.
The Amazon connector could be written against `amazon-orders` and checked
against that project's own fixtures; here the first honest step is to look at
the real payload and write the parser against what is there — not against a
guess about where a JS app keeps its state.

What it answers, in one run:

* Does a **headless** browser reach the orders page, or does the edge challenge
  it? (Decides whether every later sync needs a visible window.)
* Where does the order data live — an embedded `__NEXT_DATA__`-style blob, some
  other inline JSON, or an XHR the page makes after load?
* Do **in-store** purchases appear in order history at all, or only online ones?
* Does an order carry per-charge payment lines, or only a single total?

Everything lands in `data/walmart/capture/`, which is gitignored: these dumps
are real order contents. Nothing here is on the sync path, and nothing it writes
is ever read by the connector — it is a window, not a stage.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ... import paths
from . import browser
from .browser_login import ORDERS_URL, blocked, signed_in
from .session import (WalmartAuthError, WalmartBlocked, capture_dir,
                      require_session)

#: Responses worth keeping. The page pulls in a lot of telemetry and imagery;
#: this is the subset that could plausibly carry order data.
INTERESTING_URL = re.compile(r"graphql|/orders|order|purchase|account", re.IGNORECASE)

#: Filenames are derived from URLs, so they get reduced to something that cannot
#: escape the capture directory.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

#: How long to let the page settle after load before snapshotting. Order data
#: arrives by XHR after first paint, so snapshotting at `domcontentloaded`
#: reliably captures an empty shell and proves nothing.
SETTLE_MS = 6000


def _safe_name(url: str, i: int) -> str:
    return f"{i:03d}-{_UNSAFE.sub('-', url.split('?')[0])[-90:].strip('-')}.json"


def _write(path: Path, text: str) -> Path:
    """Write 0600. These files hold real order contents — same posture as the
    session itself, not whatever the umask happens to be."""
    path.write_text(text, encoding="utf-8")
    path.chmod(paths.FILE_MODE)
    return path


def _inline_json(page) -> dict:
    """Every inline JSON script block on the page, keyed by id or index.

    Cast wide on purpose. Walmart may use `__NEXT_DATA__`, a redux-state blob,
    or several `application/json` islands, and the point of this command is to
    find out which rather than to assume one.
    """
    return page.evaluate(
        """() => {
            const out = {};
            document.querySelectorAll(
                'script[type="application/json"], script[id="__NEXT_DATA__"]'
            ).forEach((s, i) => { out[s.id || `script-${i}`] = s.textContent; });
            return out;
        }"""
    )


def _order_links(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll("a[href*='/orders/']"))
                      .map(a => a.href)
                      .filter(h => !/\\/orders\\/?($|\\?)/.test(h))"""
    )


def run(*, headless: bool = True, echo=print) -> dict:
    """Capture the order list and one order detail. Returns a manifest dict."""
    state = require_session()
    out = capture_dir()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest: dict = {"captured_at": stamp, "headless": headless,
                      "pages": [], "responses": [], "order_links": 0}

    with browser.context(headless=headless, storage_state=state) as (_ctx, page):
        seen: list[dict] = []

        def on_response(resp) -> None:
            try:
                if not INTERESTING_URL.search(resp.url):
                    return
                ctype = (resp.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                seen.append({"url": resp.url, "status": resp.status,
                             "body": resp.text()})
            except Exception:                              # pragma: no cover
                # A response can be gone by the time we ask for its body. A
                # capture that skips one payload is fine; one that dies is not.
                return

        page.on("response", on_response)

        def snapshot(url: str, label: str) -> None:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(SETTLE_MS)
            html = page.content()
            html_path = _write(out / f"{stamp}-{label}.html", html)
            blobs = _inline_json(page)
            json_path = _write(out / f"{stamp}-{label}-inline.json",
                               json.dumps(blobs, indent=2))
            manifest["pages"].append({
                "label": label, "requested": url, "landed": page.url,
                "signed_in": signed_in(page.url, html),
                "blocked": blocked(page.url, html),
                "html": html_path.name, "html_bytes": len(html),
                "inline": json_path.name,
                "inline_keys": {k: len(v or "") for k, v in blobs.items()},
            })

        snapshot(ORDERS_URL, "orders")
        if manifest["pages"][0].get("blocked"):
            raise WalmartBlocked(
                "Walmart served its bot challenge instead of the orders page.\n"
                "  Wait before retrying — signing in again would send MORE "
                "traffic from an address already being throttled, which is the "
                "one thing that makes this worse.\n"
                "  The saved session is probably still fine; nothing here needs "
                "fixing except the timing.")
        if not manifest["pages"][0]["signed_in"]:
            # Say it here rather than letting a later parser puzzle over a guest
            # page. Walmart serves a 200 at the right URL either way, so this is
            # the only place the difference is visible.
            raise WalmartAuthError(
                "the orders page served the guest sign-in wall — the saved "
                "session is not being honoured"
                + (" in headless mode; retry with --headed" if headless else "")
                + ". If --headed also shows the wall, run "
                  "`budget walmart login` again.")

        links = _order_links(page)
        manifest["order_links"] = len(links)
        if links:
            snapshot(links[0], "order-detail")
        else:
            echo("  ! no order links found on the page — the list may render "
                 "differently than expected; the HTML dump will show what it is")

        for i, r in enumerate(seen):
            p = _write(out / _safe_name(r["url"], i), r["body"])
            manifest["responses"].append(
                {"url": r["url"], "status": r["status"],
                 "file": p.name, "bytes": len(r["body"])})

    _write(out / f"{stamp}-MANIFEST.json", json.dumps(manifest, indent=2))
    return manifest
