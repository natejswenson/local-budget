"""PRESS brand contract for the PDF report.

Mirrors city-report's tests/test_presentation.py, because the two renderers
are ports of the same theme and drifting apart is exactly the failure this
suite exists to prevent. Also keeps the dashboard half of the old palette
test: palette.css is still the dashboard's single source of color, it just no
longer serves the PDF.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from local_budget.report import brand, charts, html

STATIC = Path(__file__).resolve().parents[1] / "src" / "local_budget" / "web" / "static"

_HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def test_default_theme_is_press():
    theme = brand.load_theme()
    assert theme["name"] == "press"
    assert theme["colors"]["paper"] == "#F5F0E6"
    assert theme["colors"]["ink"] == "#181510"
    assert theme["colors"]["dim"] == "#6E675C"
    assert theme["colors"]["accent"] == "#E8501F"


def test_press_tokens_match_the_other_skills():
    """The four tokens are byte-identical across local-fitness, city-report,
    devlog, ghostwriter and the résumé theme. That shared value IS the brand —
    a local 'improvement' here silently unmatches five other publications."""
    c = brand.DEFAULT_THEME["colors"]
    assert (c["paper"], c["ink"], c["dim"], c["accent"]) == (
        "#F5F0E6", "#181510", "#6E675C", "#E8501F")


def test_stylesheet_has_no_rounded_corners_shadows_or_gradients():
    css = brand.stylesheet(brand.load_theme())
    assert "border-radius" not in css
    assert "box-shadow" not in css
    assert "gradient" not in css


def test_stylesheet_declares_every_token_charts_references():
    """charts.py emits var(--…) names; the stylesheet must declare each one on
    :root. A typo'd token is invisible — the property just doesn't apply and
    the bar renders transparent."""
    css = brand.stylesheet(brand.load_theme())
    fragments = "".join([
        charts.stat_row({"spend_total_cents": 1000, "income_cents": 2000}),
        charts.spend_vs_budget({"month": "2026-06", "categories": [
            {"category": "X", "spent_cents": 100, "budget_cents": 200,
             "over": False, "floor": False, "pct": 50}]}),
        charts.trend_chart([{"month": "2026-01", "spend_cents": 1,
                             "income_cents": 2}]),
    ])
    for token in set(re.findall(r"var\((--[a-z-]+)\)", fragments)):
        assert f"{token}:" in css, f"{token} referenced but never declared"


def test_theme_override_deep_merges(tmp_path, monkeypatch):
    f = tmp_path / "brand.json"
    f.write_text(json.dumps({"colors": {"accent": "#00AA00"}}))
    monkeypatch.setenv("BUDGET_BRAND_FILE", str(f))
    theme = brand.load_theme()
    assert theme["colors"]["accent"] == "#00AA00"
    assert theme["colors"]["paper"] == "#F5F0E6"      # untouched keys survive
    assert theme["identity"]["stamp"] == "NS"


def test_broken_or_missing_brand_file_falls_back_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDGET_BRAND_FILE", str(tmp_path / "nope.json"))
    assert brand.load_theme()["colors"]["accent"] == "#E8501F"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("BUDGET_BRAND_FILE", str(bad))
    assert brand.load_theme()["colors"]["accent"] == "#E8501F"


def test_no_brand_file_env_uses_default(monkeypatch):
    monkeypatch.delenv("BUDGET_BRAND_FILE", raising=False)
    assert brand.load_theme() == brand.DEFAULT_THEME


# ── the accent law ────────────────────────────────────────────────────────────
def _page(**kw):
    defaults = dict(
        period="2026-06", theme=brand.load_theme(),
        sections=[
            charts.stat_row({"spend_total_cents": 300000, "income_cents": 100000}),
            charts.spend_vs_budget({"month": "2026-06", "categories": [
                {"category": "Dining Out", "spent_cents": 21000, "budget_cents": 20000,
                 "over": True, "floor": False, "pct": 105}]}),
        ],
        generated_on="2026-07-11")
    return html.assemble(**{**defaults, **kw})


#: Every selector allowed to spend the accent, and why it earns it. The orange
#: is the brand's scarcest resource — this set IS the budget, and a new entry
#: is a deliberate design decision, not an implementation detail.
ACCENT_BUDGET = {
    "span.stamp":            "the masthead monogram — part of the frame",
    "div.stat.focal .value": "the one headline figure (Spent)",
    "span.sb-over":          "the overspend segment — the orange MEASURES it",
    "span.warn":             "the ⚠ on an over-budget row",
    "text.axis.now":         "the report's own month in the trend series",
}


def test_accent_is_declared_once_and_spent_only_by_the_budgeted_selectors():
    """The orange hex is declared exactly once (on :root) and spent only by the
    selectors in ACCENT_BUDGET. Pinning the selector SET rather than a count is
    what makes an unplanned fifth use fail loudly instead of quietly diluting
    the one thing the eye is supposed to land on."""
    page = _page()
    assert page.count("#E8501F") == 1                    # declared once, on :root

    css = brand.stylesheet(brand.load_theme())
    users = {
        block.split("{")[0].strip().splitlines()[-1].strip()
        for block in css.split("}")
        if "var(--accent)" in block and "{" in block
    }
    assert users == set(ACCENT_BUDGET), users


def test_accent_uses_are_all_data_bearing_not_decorative():
    """Every accent use past the frame must be attached to a specific datum —
    a figure, a magnitude, or a row that broke its budget. None may be a
    background, a border on a container, or a heading."""
    css = brand.stylesheet(brand.load_theme())
    for block in css.split("}"):
        if "var(--accent)" not in block or "{" not in block:
            continue
        selector = block.split("{")[0].strip().splitlines()[-1].strip()
        if selector == "span.stamp":                      # the frame is exempt
            continue
        assert "background" not in block or selector == "span.sb-over", (
            f"{selector} fills with accent but is not the overspend bar")


def test_exactly_one_focal_figure_and_charts_never_reach_for_the_accent():
    page = _page()
    assert page.count('class="stat focal"') == 1
    fragments = charts.spend_vs_budget({"month": "2026-06", "categories": [
        {"category": "X", "spent_cents": 100, "budget_cents": 50,
         "over": True, "floor": False, "pct": 200}]})
    fragments += charts.trend_chart([{"month": "2026-01", "spend_cents": 1,
                                      "income_cents": 2}])
    assert "--accent" not in fragments


def test_over_budget_and_negative_net_use_the_mark_not_a_color():
    page = _page()
    assert "Dining Out" in page and brand.WARN in page
    # the old traffic light is gone entirely
    for dead in ("--report-good", "--report-warning", "--report-critical",
                 "#0ca30c", "#fab219", "#d03b3b", "#2a78d6"):
        assert dead not in page


def test_warn_mark_is_forced_to_text_presentation():
    """Bare U+26A0 renders as a colored emoji glyph in Chromium — a second loud
    color on a strictly one-accent page, invisible in the HTML and wrong in the
    PDF. Every ⚠ must carry U+FE0E."""
    assert brand.WARN == "⚠︎"
    page = _page()
    assert "⚠" in page
    for i, ch in enumerate(page):
        if ch == "⚠":
            assert page[i + 1] == "︎", "bare U+26A0 without the VS15 selector"


def test_page_carries_the_press_frame():
    page = _page(user_name="Sam", narrative="Dining Out ran hot.")
    assert 'class="masthead"' in page and 'class="stamp"' in page
    assert "LOCAL BUDGET · MONTHLY REPORT · 2026-07-11" in page
    assert 'class="standfirst"' in page and "Dining Out ran hot." in page
    assert 'class="provenance"' in page and "Sam" in page
    assert "@page {{" not in page and "@page { size: letter" in page


def test_assemble_escapes_untrusted_text():
    page = _page(user_name="Sam <script>alert(1)</script>",
                 narrative="Spending & saving — <b>not bold</b>",
                 provenance="<i>7</i> posted transactions")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;b&gt;not bold&lt;/b&gt;" in page
    assert "<i>7</i>" not in page


# ── the dashboard half of the old palette test ────────────────────────────────
def test_dashboard_palette_is_hex_and_not_redefined_in_index():
    css = (STATIC / "palette.css").read_text()
    tokens = dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", css))
    assert tokens, "palette.css declared no tokens"
    for name, value in tokens.items():
        assert _HEX.match(value), f"--{name}: {value} is not a hex color"
    index = (STATIC / "index.html").read_text()
    assert 'href="palette.css"' in index
    for name in tokens:
        assert f"--{name}:" not in index.replace(" ", ""), (
            f"index.html redefines --{name} — palette.css is the single source")


def test_palette_css_no_longer_carries_report_tokens():
    """The PDF owns its color in brand.py now. A --report-* token reappearing
    here means someone re-split the source of truth."""
    css = (STATIC / "palette.css").read_text()
    # A *declaration*, not the substring — the file's own comment explains why
    # the block was removed and legitimately says "--report-*".
    assert not re.search(r"--report-[a-z-]+\s*:", css)
