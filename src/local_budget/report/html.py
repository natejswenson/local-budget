"""Report page assembly: fragments + brand theme → one self-contained page.

The PRESS frame lives here: a masthead on top (heavy ink rule, rotated accent
stamp, tracked-caps eyebrow, dim byline), the section fragments from charts.py
in the middle, and a colophon on the bottom. Fixed light theme — a static PDF
has no viewer-side toggle, and the paper is the brand.

Color tokens and `@page` come from `brand.stylesheet()`, which is the single
source of report color (see brand.py). Nothing here picks a hex.
"""
from __future__ import annotations

import html as _html

from . import brand


def assemble(*, period: str, theme: dict, sections: list[str],
             user_name: str | None = None, narrative: str | None = None,
             generated_on: str | None = None,
             provenance: str | None = None) -> str:
    """The full report page. `sections` are trusted fragments from charts.py;
    `narrative`, `user_name` and `provenance` are untrusted free text and are
    HTML-escaped.

    The narrative becomes the standfirst — the serif-italic line under the
    headline. That is the one place commentary belongs in this brand, and it
    is why the old bordered `.narrative` callout is gone.
    """
    ident = theme["identity"]
    eyebrow_bits = [ident["brand_line"], "MONTHLY REPORT"]
    if generated_on:
        eyebrow_bits.append(generated_on)
    eyebrow = " · ".join(_html.escape(b) for b in eyebrow_bits)

    standfirst = (
        f'<p class="standfirst">{_html.escape(narrative)}</p>' if narrative else "")

    colophon_bits = [b for b in (
        f"Prepared for <strong>{_html.escape(user_name)}</strong>" if user_name else None,
        _html.escape(provenance) if provenance else None,
        f"Generated {_html.escape(generated_on)}" if generated_on else None,
    ) if b]
    colophon = (
        f'<footer class="provenance">{" · ".join(colophon_bits)}</footer>'
        if colophon_bits else "")

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>Budget report — {_html.escape(period)}</title>"
        f"<style>{brand.stylesheet(theme)}</style></head><body><main>"
        '<header class="masthead"><div class="masthead-row">'
        f'<span class="stamp">{_html.escape(ident["stamp"])}</span>'
        f'<span class="eyebrow">{eyebrow}</span>'
        f'<span class="byline">{_html.escape(ident["byline"])}</span></div>'
        f"<h1>{_html.escape(period)}</h1>{standfirst}</header>"
        f'{"".join(sections)}{colophon}</main></body></html>'
    )
