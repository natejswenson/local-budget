"""PRESS brand theme — the warm-paper editorial system shared across Nate's work.

Ported from ``city-report``'s ``scripts/brand.py``, itself ported from
``local-fitness``'s ``agent/branding.py``, so a monthly budget report, a city
profile and a morning brief read as the same publication. The canonical rules,
kept even under color overrides:

* flat cream paper — never gradiented, never textured;
* ink rules carry all structure — **no rounded corners, no shadows, no fills**;
* display sans set 800–900 with tight tracking for structure, serif italic for
  commentary, mono for data and labels;
* **one** loud accent, used once or twice in a document — on the single most
  notable figure, never as decoration.

Like the others, the theme is local-overridable: point ``BUDGET_BRAND_FILE`` at
a JSON file and its keys deep-merge over the default. A missing or broken brand
file must never break a render — any load error falls back to the default
silently, because a report that fails to generate is worse than one in the
wrong colors.

**On color in this report specifically.** A budget report's job is to show
which categories ran over, and the obvious way to do that is a green/amber/red
traffic light. This theme deliberately does not: over-budget is carried by a
``⚠`` text mark and the budget tick, and every bar is ink. The accent is spent
exactly twice per document — the masthead stamp and the single headline figure
— which is the whole accent law. Semantics that would otherwise want a hue are
carried by marks and labels, which survive greyscale printing and every form of
color vision besides.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

_BRAND_FILE_ENV = "BUDGET_BRAND_FILE"

#: Forces U+26A0 to text presentation. Bare ``⚠`` renders as a *colored emoji*
#: glyph in Chromium, which would put a second loud color on the page and break
#: the accent law silently — the mark looks fine in the HTML and wrong in the
#: PDF. tests/test_report_brand.py pins this.
WARN = "⚠︎"

DEFAULT_THEME: dict = {
    "name": "press",
    "colors": {
        # Warm cream paper — flat, never gradiented or textured.
        "paper": "#F5F0E6",
        # Near-black ink: text, headlines, every structural rule, every bar.
        "ink": "#181510",
        # Muted secondary text (the serif "commentary" voice's color).
        "dim": "#6E675C",
        # THE one loud color. The stamp, and the single most notable figure.
        "accent": "#E8501F",
        # Rules are ink — named separately so an override can soften them
        # without touching text color.
        "rule": "#181510",
        # Mid ink step: the second series in a two-series chart, and the
        # track a bar is drawn on.
        "ink_mid": "#4A423A",
    },
    "fonts": {
        "display_stack": "-apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif",
        "serif_stack": "'New York', ui-serif, Georgia, 'Times New Roman', serif",
        "mono_stack": "ui-monospace, 'SF Mono', Menlo, monospace",
    },
    "identity": {
        # Typographic stamp (rotated square, accent border + initials).
        "stamp": "NS",
        # Masthead eyebrow, tracked caps: "{brand_line} · MONTHLY REPORT · date".
        "brand_line": "LOCAL BUDGET",
        # Right-aligned dim byline in the masthead.
        "byline": "local-budget · generated offline",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``.

    Non-dict values replace; unknown keys are kept, so a brand file written
    against a newer default still loads against an older one.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_theme() -> dict:
    """The active theme: ``DEFAULT_THEME`` merged with ``BUDGET_BRAND_FILE``.

    Read per render rather than cached, so editing the brand file takes effect
    on the next report without restarting anything.
    """
    theme = copy.deepcopy(DEFAULT_THEME)
    brand_file = os.environ.get(_BRAND_FILE_ENV)
    if brand_file:
        try:
            override = json.loads(Path(brand_file).expanduser().read_text(encoding="utf-8"))
            if isinstance(override, dict):
                theme = _deep_merge(theme, override)
        except (OSError, ValueError):
            pass
    return theme


def stylesheet(theme: dict) -> str:
    """The report stylesheet.

    Structure comes from ink rules and whitespace — there is not a single
    ``border-radius``, ``box-shadow`` or gradient in here, and that is the
    brand, not an oversight. Tones and emphasis are typographic: weight,
    italic, tracking and the dim/ink split do the work that color does
    elsewhere.

    ``@page`` pins the sheet so headless-Chrome output is stable across
    machines and displays. The margin is even on all four sides: Chromium
    never paints a background into the page margin, so the cream stops at the
    margin edge and the document reads as a card on a mat (the same trade the
    résumé's ``press`` theme makes).
    """
    c = theme["colors"]
    f = theme["fonts"]
    paper, ink, dim, accent, rule, ink_mid = (
        c["paper"], c["ink"], c["dim"], c["accent"], c["rule"], c["ink_mid"])
    return f"""
/* Tokens on :root, never a wrapper div — a token declared on a wrapper is
   invisible to body-level rules (DOM ancestry, not theming). charts.py emits
   var(--ink) / var(--ink-mid) so its fragments stay theme-independent and the
   golden snapshots don't bake in one palette's hex. */
:root {{
  color-scheme: light;
  --paper: {paper};
  --ink: {ink};
  --dim: {dim};
  --accent: {accent};
  --rule: {rule};
  --ink-mid: {ink_mid};
}}
@page {{ size: letter; margin: 14mm; }}
* {{ box-sizing: border-box; }}
html {{ background: var(--paper); }}
body {{
  font-family: {f["display_stack"]};
  color: var(--ink);
  background: var(--paper);
  font-size: 13.5px;
  line-height: 1.45;
  margin: 0;
}}
main {{ max-width: 60rem; margin: 0 auto; }}

/* Masthead — heavy ink rule, rotated accent stamp, tracked-caps mono eyebrow,
   dim byline right. The editorial opening: no banner fills, no pills. */
header.masthead {{ border-top: 8px solid var(--rule); padding-top: 0.6rem; margin-bottom: 1.4rem; }}
div.masthead-row {{ display: flex; align-items: center; gap: 0.9rem; flex-wrap: wrap; }}
span.stamp {{
  display: inline-block;
  border: 2.5px solid var(--accent);
  color: var(--accent);
  font-weight: 900;
  font-size: 0.8rem;
  letter-spacing: 0.02em;
  padding: 0.22em 0.34em;
  transform: rotate(-4deg);
}}
span.eyebrow {{
  font-family: {f["mono_stack"]};
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}}
span.byline {{
  margin-left: auto;
  font-family: {f["mono_stack"]};
  font-size: 0.68rem;
  letter-spacing: 0.05em;
  color: var(--dim);
}}
header.masthead h1 {{
  font-size: clamp(2rem, 6vw, 3.1rem);
  font-weight: 900;
  letter-spacing: -0.03em;
  line-height: 1.02;
  margin: 0.5rem 0 0.2rem;
}}
p.standfirst {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: var(--dim);
  font-size: 1.02rem;
  margin: 0.2rem 0 0;
  max-width: 46rem;
}}

/* Stat strip — PRESS numerals: big 900 ink figures over tiny tracked-caps dim
   labels, ruled above and below. No tiles, no fills, no radii. `.focal` is the
   report's one accent figure; there is never more than one. */
section.stat-strip {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: 0 1.4rem;
  border-top: 2px solid var(--rule);
  border-bottom: 2px solid var(--rule);
  margin: 1.5rem 0 2rem;
  padding: 0.9rem 0 1rem;
  break-inside: avoid;
}}
div.stat .value {{
  font-size: clamp(1.5rem, 3.4vw, 2.1rem);
  font-weight: 900;
  letter-spacing: -0.03em;
  line-height: 1.1;
  display: block;
  font-variant-numeric: tabular-nums;
}}
div.stat.focal .value {{ color: var(--accent); }}
div.stat .label {{
  display: block;
  margin-top: 0.25rem;
  font-family: {f["mono_stack"]};
  font-size: 0.6rem;
  letter-spacing: 0.11em;
  text-transform: uppercase;
  color: var(--dim);
}}

/* Section headings — mono, tracked caps, hairline rule. No emoji: PRESS has
   none anywhere in it. */
h3.block-title {{
  font-family: {f["mono_stack"]};
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ink);
  border-bottom: 1px solid var(--dim);
  padding-bottom: 0.3rem;
  margin: 2rem 0 0.9rem;
}}
section {{ break-inside: avoid; }}

/* Spend vs budget — every bar is ink; over-budget is a mark, not a color.
   Square track, square fill, no radii. The tick is the budget position. */
div.sb-row {{
  /* The value column is sized for the longest string it can hold —
     "$00,000.00 of $0,000.00 · 000%" — because wrapping it puts the percentage
     on its own ragged second line and breaks the row rhythm. */
  display: grid;
  grid-template-columns: 10rem 1fr 14.5rem;
  gap: 0 0.9rem;
  align-items: center;
  margin: 0.34rem 0;
}}
div.sb-label {{
  font-size: 0.82rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
div.sb-label span.warn {{ color: var(--ink); font-weight: 700; }}
div.sb-track {{ position: relative; height: 0.62rem; background: var(--paper);
  border-bottom: 1px solid var(--dim); }}
span.sb-fill {{ position: absolute; left: 0; top: 0; bottom: 0; background: var(--ink);
  display: block; }}
span.tick {{ position: absolute; top: -3px; bottom: -3px; width: 2px;
  background: var(--ink); display: block; }}
div.sb-value {{
  font-family: {f["mono_stack"]};
  font-size: 0.68rem;
  color: var(--dim);
  text-align: right;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.02em;
}}

/* Data tables — mono voice, ink rules, never zebra fills. */
table.data {{
  width: 100%;
  border-collapse: collapse;
  font-family: {f["mono_stack"]};
  font-size: 0.75rem;
  margin: 0.5rem 0 0;
}}
table.data th {{
  text-align: left;
  font-size: 0.6rem;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  border-bottom: 2px solid var(--rule);
  padding: 0.3em 0.6em 0.3em 0;
}}
table.data td {{ border-bottom: 1px solid var(--dim); padding: 0.35em 0.6em 0.35em 0; }}
/* A right-aligned column needs a LEFT gutter, or its digits butt against the
   previous cell's text — an amount and a date run together into one token. Only the
   last column may drop its right padding, so the figures align to the rule. */
table.data td.num, table.data th.num {{ text-align: right; padding-left: 1.6em;
  font-variant-numeric: tabular-nums; }}
table.data td.num:last-child, table.data th.num:last-child {{ padding-right: 0; }}
table.data tr:last-child td {{ border-bottom: none; }}

/* Legend: the swatch carries the series color, the text stays ink. Present on
   every two-series chart — identity must never rest on recalling a hue. */
div.legend {{ display: flex; flex-wrap: wrap; gap: 0 1rem; margin: 0.1rem 0 0.5rem; }}
div.legend .key {{
  display: inline-flex;
  align-items: center;
  gap: 0.35em;
  font-family: {f["mono_stack"]};
  font-size: 0.63rem;
  letter-spacing: 0.05em;
  color: var(--ink);
}}
div.legend .key i {{ width: 0.7rem; height: 0.7rem; display: inline-block; }}

svg {{ display: block; max-width: 100%; height: auto; }}
text.axis {{ font-family: {f["mono_stack"]}; font-size: 9px; fill: var(--dim); }}

p.caption {{
  font-family: {f["serif_stack"]};
  font-style: italic;
  color: var(--dim);
  font-size: 0.85rem;
  margin: 0.5rem 0 0;
}}
p.empty {{
  font-family: {f["mono_stack"]};
  font-size: 0.7rem;
  letter-spacing: 0.04em;
  color: var(--dim);
  border-left: 2px solid var(--dim);
  padding-left: 0.7rem;
  margin: 0.6rem 0 0;
}}

footer.provenance {{
  border-top: 2px solid var(--rule);
  padding-top: 0.8rem;
  margin-top: 2.6rem;
  font-family: {f["mono_stack"]};
  font-size: 0.64rem;
  line-height: 1.7;
  color: var(--dim);
  letter-spacing: 0.03em;
}}
footer.provenance strong {{ color: var(--ink); font-weight: 700; }}

@media print {{ body {{ font-size: 10.5pt; }} }}
"""
