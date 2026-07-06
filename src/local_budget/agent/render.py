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


def table(
    rows: list[dict],
    cols: list[tuple[str, str]],
    *,
    numbered: bool = False,
    drill_hint: str | None = None,
) -> str:
    """GitHub-flavored markdown table. ``cols`` = ``[(key, header), ...]``.
    A column is right-aligned iff every populated cell looks numeric. ``None``
    renders as ``—``. Empty ``rows`` → a header-only table.

    ``numbered=True`` prepends a 1-indexed ``Row`` column (never ``#`` — some
    callers already use ``#`` for an unrelated count column; see design doc
    2026-07-05-conversational-numbered-drilldown-design.md).

    ``drill_hint``, when non-empty AND ``rows`` is non-empty, appends a
    trailing italic hint line. Suppressed unconditionally on empty ``rows``
    regardless of what the caller passed — centralizing this here keeps it a
    deterministic property of the tool output rather than each call site
    needing its own empty check (see design doc
    2026-07-05-drilldown-tabular-and-followup-design.md)."""
    keys = [k for k, _ in cols]
    headers = [h for _, h in cols]
    if numbered:
        keys = ["__row__", *keys]
        headers = ["Row", *headers]
        rows = [{"__row__": i + 1, **r} for i, r in enumerate(rows)]
    body = [[_cell(r.get(k)) for k in keys] for r in rows]

    aligns: list[bool] = []
    for ci in range(len(keys)):
        vals = [body[ri][ci] for ri in range(len(body))]
        populated = [v for v in vals if v != "—"]
        aligns.append(bool(populated) and all(_NUM_RE.match(v) for v in populated))

    sep = " | ".join("---:" if a else "---" for a in aligns)
    lines = ["| " + " | ".join(headers) + " |", "| " + sep + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    if drill_hint and rows:
        lines += ["", f"_{drill_hint}_"]
    return "\n".join(lines)


def bars(
    items: list[tuple[str, int]],
    *,
    width: int = 20,
    numbered: bool = False,
    drill_hint: str | None = None,
) -> str:
    """Horizontal share bars: ``label  ▇▇▇…  $amount (pct%)``, longest = ``width``.
    Percent is each item's share of the total. Empty → ``""``.

    ``numbered=True`` prefixes each line with a 1-indexed ``N. `` — there are no
    column headers here, so there's no collision to check (unlike ``table()``).

    ``drill_hint``, when non-empty, appends a trailing italic hint line.
    Never appended when ``items`` is empty (this function's existing
    empty → ``""`` contract already suppresses it, consistent with
    ``table()``'s empty-suppression rule above)."""
    if not items:
        return ""
    mx = max((abs(v) for _, v in items), default=0) or 1
    total = sum(abs(v) for _, v in items) or 1
    lines = []
    for i, (label, v) in enumerate(items):
        filled = round(abs(v) / mx * width)
        pct = round(abs(v) / total * 100)
        prefix = f"{i + 1}. " if numbered else ""
        lines.append(f"{prefix}{label}  {'▇' * filled}  {money(v)} ({pct}%)")
    if drill_hint:
        lines += ["", f"_{drill_hint}_"]
    return "\n".join(lines)
