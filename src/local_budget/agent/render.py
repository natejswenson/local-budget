"""Deterministic markdown rendering for the MCP tool surface (design §3).

The read tools return BOTH a structured ``data`` payload and a ``rendered``
markdown block; skills print ``rendered`` verbatim. Putting the formatting here
(pure, unit-tested) makes "clean & beautiful" a regression-guarded property
rather than a model whim. Money is always signed integer cents → ``$X,XXX.XX``
(never float — reuses ``money.dollars``).
"""
from __future__ import annotations

import re

from ..money import dollars

# A cell that "looks numeric" (money / count / percent) — right-aligned in tables.
_NUM_RE = re.compile(r"^-?\$?[\d,]+(\.\d+)?%?$")


def money(cents: int) -> str:
    """Signed integer cents → ``$1,234.56`` / ``-$1,234.56`` / ``$0.00``."""
    return dollars(cents)


def _cell(v: object) -> str:
    return "—" if v is None else str(v)


def table(rows: list[dict], cols: list[tuple[str, str]]) -> str:
    """GitHub-flavored markdown table. ``cols`` = ``[(key, header), ...]``.
    A column is right-aligned iff every populated cell looks numeric. ``None``
    renders as ``—``. Empty ``rows`` → a header-only table."""
    keys = [k for k, _ in cols]
    headers = [h for _, h in cols]
    body = [[_cell(r.get(k)) for k in keys] for r in rows]

    aligns: list[bool] = []
    for ci in range(len(keys)):
        vals = [body[ri][ci] for ri in range(len(body))]
        populated = [v for v in vals if v != "—"]
        aligns.append(bool(populated) and all(_NUM_RE.match(v) for v in populated))

    sep = " | ".join("---:" if a else "---" for a in aligns)
    lines = ["| " + " | ".join(headers) + " |", "| " + sep + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def bars(items: list[tuple[str, int]], *, width: int = 20) -> str:
    """Horizontal share bars: ``label  ▇▇▇…  $amount (pct%)``, longest = ``width``.
    Percent is each item's share of the total. Empty → ``""``."""
    if not items:
        return ""
    mx = max((abs(v) for _, v in items), default=0) or 1
    total = sum(abs(v) for _, v in items) or 1
    lines = []
    for label, v in items:
        filled = round(abs(v) / mx * width)
        pct = round(abs(v) / total * 100)
        lines.append(f"{label}  {'▇' * filled}  {money(v)} ({pct}%)")
    return "\n".join(lines)
