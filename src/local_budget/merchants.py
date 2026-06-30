"""Canonical vendor identity — collapse a vendor's many bank-statement spellings.

A bank renders the same merchant several ways ("ANTHROPIC", "ANTHROPIC CLAUDE",
"CLAUDE ANTHROPIC", "PURCHASE ANTHROPIC" are all Anthropic; "HLU HULU"/"HULU" are
Hulu; "AUDIBLE AMZN" is Audible). `canonical_merchant` resolves a stored
`merchant_norm` to ONE canonical display name via `merchant_aliases` (built-in brand
rules + LLM/manual-added, cached). Deterministic and PII-free (brand names +
already-sanitized merchant_norm tokens — never raw payee/account).
"""
from __future__ import annotations

import re
import sqlite3

from . import db

# Built-in brand token -> canonical display name. Patterns are UPPERCASE tokens
# matched against the tokens of a merchant_norm; the LONGEST matching pattern wins,
# so a specific service token (AUDIBLE) outranks a processor token (AMZN).
BUILTIN_ALIASES: dict[str, str] = {
    "ANTHROPIC": "Anthropic", "CLAUDE": "Anthropic",
    "OPENAI": "OpenAI", "CHATGPT": "OpenAI",
    "HULU": "Hulu", "NETFLIX": "Netflix", "DISNEYPLUS": "Disney+", "DISNEY": "Disney+",
    "SPOTIFY": "Spotify", "AUDIBLE": "Audible", "YOUTUBEPREMIUM": "YouTube", "YOUTUBE": "YouTube",
    "APPLE": "Apple", "ITUNES": "Apple", "MICROSOFT": "Microsoft", "MSFT": "Microsoft",
    "ADOBE": "Adobe", "GITHUB": "GitHub", "VERCEL": "Vercel", "NOTION": "Notion",
    "AMAZON": "Amazon", "AMZN": "Amazon", "GOOGLE": "Google", "GOOGL": "Google",
    "DROPBOX": "Dropbox", "SLACK": "Slack", "PATREON": "Patreon", "OPENROUTER": "OpenRouter",
}
_TOKEN = re.compile(r"[A-Z0-9]+")
MIN_PATTERN_LEN = 3   # blocks ultra-short patterns only; token-precise matching (see canonical_alias) prevents the broader over-collapse


def seed_builtin_aliases(conn: sqlite3.Connection) -> None:
    """Idempotently insert the built-in brand aliases (source='builtin'). User/LLM
    aliases (source manual|llm) are never overwritten — INSERT OR IGNORE on the
    UNIQUE(pattern)."""
    for pattern, canonical in BUILTIN_ALIASES.items():
        conn.execute(
            "INSERT OR IGNORE INTO merchant_aliases (pattern, canonical, source, created_at) "
            "VALUES (?, ?, 'builtin', ?)", (pattern, canonical, db.now_iso()))


def active_aliases(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(pattern, canonical, source) for every alias, LONGEST pattern first
    (most-specific brand token wins on a multi-brand string). `source` is one of
    'builtin' | 'manual' | 'llm' and drives the source-aware matching precision in
    `canonical_alias`."""
    rows = conn.execute("SELECT pattern, canonical, source FROM merchant_aliases").fetchall()
    return sorted(((r["pattern"], r["canonical"], r["source"]) for r in rows),
                  key=lambda pcs: (-len(pcs[0]), pcs[0]))


def canonical_alias(merchant_norm: str | None, aliases: list[tuple[str, str, str]]) -> str | None:
    """The canonical vendor IF a real alias collapses this merchant_norm, else None.
    Deterministic: the first alias (longest-first) that matches wins. Matching is
    SOURCE-AWARE so a built-in brand token stays broad while a user/LLM-added alias
    (a SPECIFIC observed spelling) matches precisely:

      - builtin: a single-token pattern matches a whole TOKEN of merchant_norm
        (token-anywhere — so ANTHROPIC collapses 'PURCHASE ANTHROPIC'); a multi-word
        pattern matches by token-subset containment (none built-in today).
      - manual/llm: a MULTI-token pattern matches by token-subset containment (so
        'HOME DEPOT' still collapses 'THE HOME DEPOT', 'QORP A' will NOT collapse
        'QORP ABC INC'); a SINGLE-token pattern matches ONLY when it is the merchant_norm's
        FULL token set (exact — so a confirmed 'ACME' resolves a bare 'ACME' row but no
        longer collapses the unrelated 'ACME GYM MEMBERSHIP').

    This is what is STORED in transactions.canonical_merchant and drives merchant
    grouping — so an un-aliased merchant keeps its own display (None ⇒ fall back to
    merchant_norm)."""
    mu = (merchant_norm or "").upper()
    toks = set(_TOKEN.findall(mu))
    for pattern, canonical, source in aliases:
        ptoks = set(_TOKEN.findall(pattern.upper()))
        if " " in pattern:
            # Multi-token pattern: token-subset containment (all sources).
            if ptoks and ptoks <= toks:
                return canonical
        elif source == "builtin":
            # Single built-in brand token: token-anywhere (designed broad match).
            if pattern in toks:
                return canonical
        # Single manual/llm token: exact — only a merchant_norm that IS just that token.
        elif ptoks and ptoks == toks:
            return canonical
    return None


def canonical_merchant(merchant_norm: str | None, aliases: list[tuple[str, str, str]]) -> str:
    """A DISPLAY vendor name for a merchant_norm: the alias canonical if one matches,
    otherwise the existing friendly title-case name. Used for the Subscriptions
    subcategory label (never NULL)."""
    alias = canonical_alias(merchant_norm, aliases)
    if alias is not None:
        return alias
    from .categorize.manual import friendly_name
    return friendly_name(merchant_norm or "")


def add_alias(pattern: str, canonical: str, source: str = "manual") -> None:
    """Add/replace a user- or LLM-confirmed alias (pattern -> canonical)."""
    pattern = (pattern or "").strip().upper()
    canonical = (canonical or "").strip()
    if len(pattern) < MIN_PATTERN_LEN:
        raise ValueError(f"alias pattern must be >= {MIN_PATTERN_LEN} characters (avoid over-broad merges)")
    if not canonical:
        raise ValueError("canonical name required")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO merchant_aliases (pattern, canonical, source, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pattern) DO UPDATE SET canonical = excluded.canonical, source = excluded.source",
            (pattern, canonical, source, db.now_iso()))


def clear_alias(pattern: str) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM merchant_aliases WHERE pattern = ?",
                     ((pattern or "").strip().upper(),))
