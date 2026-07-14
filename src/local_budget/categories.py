"""Controlled category vocabulary (frozen set — the whitelist pattern from
local-fitness). Splits real-world spend categories from the three structural
ones the engine needs.

The vocabulary is intentionally broad enough to cover real bank-statement
spending — a tiny list collapses everything into `Random` and makes reports
useless. It includes `Investments` (a savings/brokerage/529 contribution)
alongside a standard personal-finance taxonomy.
"""
from __future__ import annotations

import json

# Real-world spend categories.
SPEND_CATEGORIES: frozenset[str] = frozenset({
    # standard personal-finance taxonomy
    "Groceries",
    "Dining Out",
    "Transportation",      # gas, parking, transit, rideshare
    "Shopping",            # retail, Amazon, general merchandise
    "Utilities",           # catch-all for utility-type bills not covered by a
                           # dedicated category (see Phone, Electricity,
                           # Gas/Propane, Internet, Sewer/Water/Trash)
    "Housing",             # rent, mortgage, HOA
    "Health",              # medical, pharmacy, dental, fitness
    "Entertainment",       # events, hobbies, games, streaming-as-fun
    "Subscriptions",       # recurring software/services
    "Travel",              # flights, hotels, lodging
    "Personal Care",       # salon, barber, cosmetics
    "Insurance",
    "Education",
    "Fees",                # bank/ATM/late fees, interest
    "Gifts & Donations",
    "Cash",                # ATM withdrawals
    "Investments",         # savings/brokerage/529 contribution (spend even on a transfer)
    # catch-all (true last resort only)
    "Random",
})

# Structural categories the system needs.
INCOME = "Income"
TRANSFER = "Transfer"
UNCATEGORIZED = "Uncategorized"

STRUCTURAL_CATEGORIES: frozenset[str] = frozenset({INCOME, TRANSFER, UNCATEGORIZED})

# Everything the DB / rules / LLM may assign.
CATEGORIES: frozenset[str] = SPEND_CATEGORIES | STRUCTURAL_CATEGORIES

# Excluded from spend totals (design §4.3 / I11). Investments is NOT here — it
# counts as spend even when TRNTYPE=XFER (§3 override).
NON_SPEND_CATEGORIES: frozenset[str] = frozenset({INCOME, TRANSFER, UNCATEGORIZED})

# The LLM fallback may auto-confirm ONLY ordinary spend categories (F4).
# Transfer and Income are never LLM-assigned.
LLM_ASSIGNABLE: frozenset[str] = SPEND_CATEGORIES

# Categories that can NEVER be hidden/removed: the three structural ones plus the
# `Random` catch-all (the LLM coerces low-confidence to it and the review queue keys
# off it — removing it would silently degrade categorization).
PROTECTED: frozenset[str] = STRUCTURAL_CATEGORIES | {"Random"}


def is_spend(category: str | None) -> bool:
    """True if this category counts toward spend totals.

    Any category that is not a structural one (Income/Transfer/Uncategorized) is
    spend — including user-added custom categories, which need no special-casing.
    """
    return category is not None and category not in NON_SPEND_CATEGORIES


# ── user-extensible categories (persisted in settings) ───────────────────────
def custom_categories(conn=None) -> frozenset[str]:
    from . import db
    raw = db.get_setting("custom_categories", conn=conn)
    return frozenset(json.loads(raw)) if raw else frozenset()


def hidden_categories(conn=None) -> frozenset[str]:
    """Categories suppressed from the vocabulary (lets a *builtin* be 'removed' without
    mutating the frozen SPEND_CATEGORIES). `conn` lets callers already inside a
    transaction (e.g. `seed_builtin_rules`) read on their own connection."""
    from . import db
    raw = db.get_setting("hidden_categories", conn=conn)
    return frozenset(json.loads(raw)) if raw else frozenset()


# Conn-aware vocabulary mutators (prospector F-1): categories.py is the single owner
# of the custom/hidden settings-blob encoding. `conn` lets a caller already inside a
# transaction (remove_category) reuse it instead of re-implementing json.loads/dumps.
def mark_hidden(name: str, conn=None) -> None:
    """Suppress `name` from the vocabulary (add to the hidden set). Used by the merge
    in `remove_category`; there is no standalone user 'hide' op (use remove_category)."""
    from . import db
    cur = set(hidden_categories(conn=conn))
    cur.add(name)
    db.set_setting("hidden_categories", json.dumps(sorted(cur)), conn=conn)


def remove_custom(name: str, conn=None) -> None:
    """Drop `name` from the custom-category set (exact match); no-op if not custom."""
    from . import db
    cur = set(custom_categories(conn=conn))
    if name in cur:
        cur.discard(name)
        db.set_setting("custom_categories", json.dumps(sorted(cur)), conn=conn)


def unhide_category(name: str, conn=None) -> None:
    """Un-suppress a category — case-insensitive discard so re-adding 'dining out'
    restores a hidden 'Dining Out'."""
    from . import db
    name = " ".join(name.split()).strip()
    cur = {c for c in hidden_categories(conn=conn) if c.lower() != name.lower()}
    db.set_setting("hidden_categories", json.dumps(sorted(cur)), conn=conn)


def add_custom_category(name: str, conn=None) -> str:
    """Add a user-defined spend category (persisted). Returns the stored name.

    Threads `conn` through ALL THREE of its writes (`unhide_category`,
    `custom_categories`, `set_setting`) so a write tool can run it under one
    `agent_connect(write=True)` connection — a partially-threaded version would
    self-block on its own uncommitted write lock (design-gate S1)."""
    from . import db
    name = " ".join(name.split()).strip()
    if not name:
        raise ValueError("category name is required")
    # Server-side choke point (CB-1): a custom category name is the one untrusted
    # string that flows into the category-bar / insight-label DOM sinks. Reject
    # HTML metacharacters at creation so no later render is fed a name carrying
    # `< > " &`. Frontend escaping remains as defense-in-depth.
    if any(c in name for c in '<>"&'):
        raise ValueError("category name may not contain < > \" or &")
    # Un-hide UNCONDITIONALLY first (before the dedup short-circuit): re-adding a
    # previously-removed builtin (e.g. "Dining Out") must restore it even though the
    # dedup below sees it as already-existing and skips the custom insert.
    unhide_category(name, conn=conn)
    cur = set(custom_categories(conn=conn))
    # Case-insensitive dedup against builtin + existing custom.
    lower = {c.lower() for c in (SPEND_CATEGORIES | STRUCTURAL_CATEGORIES | frozenset(cur))}
    if name.lower() not in lower:
        cur.add(name)
        db.set_setting("custom_categories", json.dumps(sorted(cur)), conn=conn)
    return name


# ── floor-type (more-is-better) category marking ─────────────────────────────
def floor_categories(conn=None) -> frozenset[str]:
    """Categories where MORE spend is good (e.g. Investments), the opposite of
    every other (ceiling-type) category. Persisted the same way as
    `hidden_categories` — a JSON array under a settings key — so marking a
    category as floor-type is a live, no-deploy operation."""
    from . import db
    raw = db.get_setting("floor_categories", conn=conn)
    return frozenset(json.loads(raw)) if raw else frozenset()


def mark_floor_category(name: str, conn=None) -> None:
    """Mark `name` as floor-type (more spend is good)."""
    from . import db
    cur = set(floor_categories(conn=conn))
    cur.add(name)
    db.set_setting("floor_categories", json.dumps(sorted(cur)), conn=conn)


def unmark_floor_category(name: str, conn=None) -> None:
    """Un-mark `name` as floor-type — reverts it to ordinary ceiling semantics."""
    from . import db
    cur = {c for c in floor_categories(conn=conn) if c != name}
    db.set_setting("floor_categories", json.dumps(sorted(cur)), conn=conn)


def is_floor(category: str, conn=None, floor_set: frozenset[str] | None = None) -> bool:
    """True if `category` is marked floor-type (more spend is good). Pass an
    already-fetched `floor_set` (from `floor_categories()`) to skip the DB read
    when checking many categories in a loop/request — every consumer that
    previously re-read `floor_categories` once per row should fetch it once
    up front and thread it through instead."""
    if floor_set is not None:
        return category in floor_set
    return category in floor_categories(conn=conn)


def off_track_delta(category: str, actual_cents: int, limit_cents: int, conn=None,
                    floor_set: frozenset[str] | None = None) -> int:
    """Direction-aware, sign-normalized "how far off track" — positive always
    means "bad", regardless of whether `category` is ceiling- or floor-type.

    Ceiling category (the default): `actual_cents - limit_cents` — positive
    when spend exceeds the limit.
    Floor category: `limit_cents - actual_cents` — positive when spend falls
    short of the target.

    This is the one place the sign gets normalized; every consumer (the
    `over`/`over_cents` boolean/magnitude, the "$X over/under target" display)
    derives from this value rather than re-deriving `actual > limit` locally.
    """
    if is_floor(category, conn=conn, floor_set=floor_set):
        return limit_cents - actual_cents
    return actual_cents - limit_cents


def is_off_track(category: str, actual_cents: int, limit_cents: int, conn=None,
                 floor_set: frozenset[str] | None = None) -> bool:
    """Convenience wrapper: True if `off_track_delta(...) > 0` — i.e. "bad"."""
    return off_track_delta(category, actual_cents, limit_cents, conn=conn, floor_set=floor_set) > 0


def is_savings(category: str | None, conn=None, floor_set: frozenset[str] | None = None) -> bool:
    """True if `category` should be reported as savings rather than spend: a
    spend category that's also floor-marked (e.g. Investments) — money that
    left checking but is still the user's, just relocated. Excluded from
    spend totals the same way `Transfer` is; see `reports.py::_month_summary`."""
    return is_spend(category) and is_floor(category, conn=conn, floor_set=floor_set)


def off_track_label(category: str, *, conn=None, floor_set: frozenset[str] | None = None,
                    under: str = "UNDER", over: str = "OVER") -> str:
    """Direction-aware label for a category currently flagged off-track:
    `under` (short of a floor target) or `over` (past a ceiling limit) by
    default — pass different literal strings (e.g. insights()'s
    'under_target'/'over_budget' kinds) as needed. The single place this
    is_floor-driven label choice is made, so callers never re-derive it
    locally with their own ternary."""
    return under if is_floor(category, conn=conn, floor_set=floor_set) else over


def spend_categories() -> frozenset[str]:
    """Builtin + user-added spend categories, minus hidden (removed)."""
    return (SPEND_CATEGORIES | custom_categories()) - hidden_categories()


def all_categories() -> frozenset[str]:
    """Everything assignable: builtin spend + structural + custom, minus hidden."""
    return (CATEGORIES | custom_categories()) - hidden_categories()


def llm_assignable() -> frozenset[str]:
    """Spend categories the LLM may assign (builtin + custom, minus hidden)."""
    return spend_categories()
