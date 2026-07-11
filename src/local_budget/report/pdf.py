"""HTML → PDF via headless Chrome, hardened.

Replaces the skill-prose shell-out (one hardcoded macOS path, no DPI pin,
scratch HTML left behind): browser discovery with an env override, @2x
device scale for crisp output, 0600 on the PDF, and the scratch file —
which holds a full month of financials — created inside the 0700 reports
dir and unlinked in a finally.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .. import paths


class ChromeNotFoundError(RuntimeError):
    """No usable Chrome/Chromium — the caller reports the fallback options."""


# Discovery order: env override → known app-bundle paths → PATH names.
_MAC_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
_PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium",
               "chromium-browser", "chrome")


def chrome_path() -> str:
    override = os.environ.get("LOCAL_BUDGET_CHROME")
    if override:
        if not Path(override).exists():
            raise ChromeNotFoundError(
                f"LOCAL_BUDGET_CHROME points at {override!r}, which does not exist")
        return override
    for candidate in _MAC_APPS:
        if Path(candidate).exists():
            return candidate
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    raise ChromeNotFoundError(
        "no Chrome/Chromium found — install Google Chrome, or set "
        "LOCAL_BUDGET_CHROME to a browser binary")


def render_pdf(html_text: str, out_path: Path, *, timeout: int = 60) -> Path:
    """Write `html_text` to a dot-prefixed scratch file inside the 0700 reports
    dir, print it to `out_path` at @2x, chmod 0600, and always clean up the
    scratch — success or failure."""
    chrome = chrome_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = out_path.parent / f".{out_path.stem}.scratch.html"
    try:
        scratch.write_text(html_text)
        os.chmod(scratch, paths.FILE_MODE)
        subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             "--force-device-scale-factor=2",
             f"--print-to-pdf={out_path}", f"file://{scratch}"],
            check=True, capture_output=True, timeout=timeout)
        os.chmod(out_path, paths.FILE_MODE)
        return out_path
    finally:
        scratch.unlink(missing_ok=True)
