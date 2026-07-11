"""Shared palette contract — one source of color truth for dashboard + PDF.

Guards the drift the old prose-recipe path suffered from: the dashboard and
the report each hardcoding their own copy of the same colors.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from local_budget.report import palette

STATIC = Path(palette.PALETTE_CSS).parent

_HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def test_palette_tokens_parse_and_are_hex():
    toks = palette.tokens()
    for name in palette.REQUIRED:
        assert name in toks
    for name, value in toks.items():
        assert _HEX.match(value), f"--{name}: {value} is not a hex color"


def test_dashboard_links_palette_and_does_not_redefine_tokens():
    html = (STATIC / "index.html").read_text()
    assert 'href="palette.css"' in html
    for name in palette.tokens():
        assert f"--{name}:" not in html.replace(" ", ""), (
            f"index.html redefines --{name} — palette.css is the single source")


def test_missing_required_token_raises(tmp_path):
    bad = tmp_path / "palette.css"
    bad.write_text(":root { --green: #4ade80; }")
    with pytest.raises(ValueError, match="missing required"):
        palette.tokens(bad)
