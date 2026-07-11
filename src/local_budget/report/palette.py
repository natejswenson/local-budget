"""Shared palette tokens, parsed from web/static/palette.css.

One source of color truth for the dashboard (which links the CSS directly)
and the PDF renderer (which inlines these values). Regex-parsed — the file
is a flat `:root { --name: value; }` block by contract, so no CSS parser
dependency is warranted.
"""
from __future__ import annotations

import re
from pathlib import Path

PALETTE_CSS = Path(__file__).resolve().parents[1] / "web" / "static" / "palette.css"

_TOKEN = re.compile(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;")

# Tokens the report renderer requires — a missing one is a packaging error,
# caught by tests, never a silent fallback color.
REQUIRED = (
    "report-accent", "report-good", "report-warning", "report-critical",
    "report-gridline",
)


def tokens(css_path: Path | None = None) -> dict[str, str]:
    """``{token-name: '#hex'}`` for every custom property in palette.css."""
    text = (css_path or PALETTE_CSS).read_text()
    found = {name: value for name, value in _TOKEN.findall(text)}
    missing = [t for t in REQUIRED if t not in found]
    if missing:
        raise ValueError(f"palette.css missing required tokens: {missing}")
    return found
