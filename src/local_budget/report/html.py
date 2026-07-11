"""Report page assembly: fragments + palette tokens → one self-contained page.

Fixed light theme (a static PDF has no viewer-side toggle). Tokens go on
`:root`, never a wrapper div — a token declared only on a wrapper is invisible
to body-level color rules (DOM-ancestry, not theming; see budget-visualizer).
`@page` pins the sheet size so headless-Chrome output is stable across
machines/displays.
"""
from __future__ import annotations

import html as _html


def _css(tokens: dict[str, str]) -> str:
    root_tokens = "".join(f"--{k}:{v};" for k, v in sorted(tokens.items()))
    return f"""
:root{{{root_tokens}
  --page:#ffffff; --text-primary:#1b1f27; --text-muted:#6a7280;
  --hairline:var(--report-gridline);}}
@page{{size:letter;margin:14mm;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--page);color:var(--text-primary);
  font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}}
main{{max-width:760px;margin:0 auto;padding:8px 0 24px;}}
h1{{font-size:22px;margin:0 0 2px;}}
h3{{font-size:14px;margin:18px 0 8px;}}
.subtitle{{color:var(--text-muted);margin:0 0 16px;font-size:12px;}}
.stat-row{{display:flex;gap:12px;margin:14px 0;}}
.stat{{flex:1;border:1px solid var(--hairline);border-radius:8px;padding:10px 12px;}}
.stat .label{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--text-muted);}}
.stat .value{{font-size:20px;font-weight:700;margin-top:2px;}}
.sb-row{{display:grid;grid-template-columns:170px 1fr 150px;gap:10px;
  align-items:center;margin:6px 0;}}
.sb-label{{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
.sb-track{{position:relative;height:12px;background:var(--report-gridline);
  border-radius:6px;overflow:visible;}}
.sb-fill{{position:absolute;left:0;top:0;bottom:0;border-radius:6px;display:block;}}
.tick{{position:absolute;top:-3px;bottom:-3px;width:2px;
  background:var(--text-primary);display:block;}}
.sb-value{{font-size:12px;color:var(--text-muted);text-align:right;
  font-variant-numeric:tabular-nums;}}
table{{border-collapse:collapse;width:100%;font-size:13px;}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--hairline);}}
th.num,td.num{{text-align:right;font-variant-numeric:tabular-nums;}}
.caption,.empty{{color:var(--text-muted);font-size:12px;}}
.narrative{{border-left:3px solid var(--report-accent);padding:2px 12px;
  margin:12px 0;color:var(--text-primary);}}
.trend svg{{width:100%;height:auto;}}
.legend{{display:flex;gap:14px;font-size:12px;color:var(--text-muted);margin:2px 0 6px;}}
.key{{display:inline-flex;align-items:center;gap:5px;}}
.swatch{{width:10px;height:10px;border-radius:2px;display:inline-block;}}
.axis{{font-size:9px;fill:var(--text-muted);}}
section{{break-inside:avoid;}}
"""


def assemble(*, period: str, tokens: dict[str, str], sections: list[str],
             user_name: str | None = None, narrative: str | None = None,
             generated_on: str | None = None) -> str:
    """The full report page. `sections` are trusted fragments from charts.py;
    `narrative` and `user_name` are untrusted free text and are HTML-escaped."""
    subtitle_bits = [b for b in (
        f"for {_html.escape(user_name)}" if user_name else None,
        f"generated {_html.escape(generated_on)}" if generated_on else None,
    ) if b]
    subtitle = f'<p class="subtitle">{" · ".join(subtitle_bits)}</p>' if subtitle_bits else ""
    narrative_html = (
        f'<div class="narrative">{_html.escape(narrative)}</div>' if narrative else "")
    body = "".join(sections)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Budget report — {_html.escape(period)}</title>"
        f"<style>{_css(tokens)}</style></head><body><main>"
        f"<h1>Budget report — {_html.escape(period)}</h1>{subtitle}"
        f"{narrative_html}{body}</main></body></html>"
    )
