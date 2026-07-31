"""The shared item classifier — its buckets, its ordering, and its coverage.

Three reports group by this one table, so its cases live here rather than in any
of them. The tests that matter most are not "does this title land in this
bucket" but the two structural guarantees underneath:

* **the vocabulary is the ledger's**, so item spend can be read against a budget
  line rather than against a taxonomy invented for a PDF; and
* **non-food outranks food**, which is the entire reason the table is ordered.
"""
from __future__ import annotations

import pytest

from local_budget.connectors import kinds
from local_budget.connectors.kinds import KINDS, classify, unhoused


# ── the vocabulary is the budget's, not the report's ─────────────────────────
def test_every_bucket_is_a_real_budget_category():
    """A bucket the budget has no category for cannot be shown against a budget
    line, which is the whole point of classifying into these names."""
    from local_budget import categories
    valid = categories.all_categories()
    for name, _ in KINDS:
        assert name in valid, f"{name!r} is not an assignable category"
    assert kinds.UNCATEGORISED in valid


def test_the_fallback_is_uncategorized_and_never_random():
    """Random is a junk drawer Nate is shrinking; defaulting into it defeats the
    point of categorising at all."""
    assert classify("Something With No Keyword At All") == "Uncategorized"
    assert classify(None) == "Uncategorized"
    assert classify("") == "Uncategorized"
    assert "Random" not in {k for k, _ in KINDS}


# ── ordering: the design, not an accident ────────────────────────────────────
def test_non_food_outranks_the_food_net():
    """The food net is broad enough to swallow a grocery run, so anything it
    must NOT swallow has to be declared above it."""
    names = [k for k, _ in KINDS]
    assert names[-1] == "Groceries", "the food net must be declared last"
    for specific in ("Personal Care", "Health", "Home Improvement",
                     "Kid Activities", "Entertainment", "Shopping"):
        assert names.index(specific) < names.index("Groceries")


@pytest.mark.parametrize("title,bucket", [
    # These four are the ordering guarantee, stated as behaviour.
    ("Cascade Complete Dishwasher Detergent Liquid Gel", "Home Improvement"),
    ("Great Value Vanilla Flavored Baking Chips, 24 oz Bag", "Groceries"),
    ("Wilton Non-Stick Baking Sheet, 10x15 in", "Home Improvement"),
    ("Head & Shoulders Anti-Dandruff 2in1 Shampoo", "Personal Care"),
    # A representative sweep of the rest.
    ("Nature Made Vitamin D3 2000 IU Softgels", "Health"),
    ("Angel Soft 2-Ply Toilet Paper, 24 Rolls", "Home Improvement"),
    ("Case-it Mighty Zip Tab School Zipper Binder", "Kid Activities"),
    ("UGG Women's Tasman II Slipper", "Shopping"),
    ("Anker USB-C Charger Cable 6ft", "Shopping"),
    ("All Natural* 80% Lean Ground Beef Chuck, 1 lb Tray", "Groceries"),
    ("Great Value Large White Eggs, 18 Count", "Groceries"),
    ("Fresh Gala Apple, Each", "Groceries"),
    ("Diet Dr Pepper Soda Pop, 12 fl oz, 12 Pack Cans", "Groceries"),
])
def test_classifier_buckets(title, bucket):
    assert classify(title) == bucket


def test_a_word_boundary_pattern_does_not_match_mid_word():
    """Patterns anchor on a leading space, so " dvd" cannot match inside another
    word. Without it the table produces bafflingly wrong buckets that are very
    hard to trace back to a keyword."""
    assert classify("Advdisor Brand Mystery Item") == "Uncategorized"


# ── the clusters the budget has no home for ──────────────────────────────────
def test_pet_supplies_are_flagged_as_unhoused():
    assert unhoused("Kaytee In Shell Peanuts Wild Bird Feed, 5 Pounds") == "Pets"
    assert unhoused("Elanco Chewable Quad Dewormer for Large Dogs") == "Pets"
    assert unhoused("Extreme Dog Fence Dog Collar") == "Pets"


def test_hot_dogs_are_not_pet_supplies():
    """The failure a bare "dog" pattern would cause: recommending a Pets budget
    on the strength of a pack of buns."""
    assert unhoused("Ball Park 100% Beef Hot Dogs, 8 Count") is None
    assert unhoused("Great Value Corn Dogs, Frozen") is None
    assert classify("Ball Park 100% Beef Hot Dogs, 8 Count") == "Groceries"


def test_unhoused_is_independent_of_classify():
    """An item can be Shopping — where the ledger can show it today — AND a Pets
    candidate, where it belongs. The report adds up by one and recommends by
    the other."""
    title = "PetSafe Nylon Dog Leash, 6 ft"
    assert unhoused(title) == "Pets"
    assert classify(title) != "Pets"


# ── coverage, asserted against the real shape of the data ────────────────────
#: Titles drawn to mirror the real corpus's proportions — mostly groceries, a
#: tail of household and personal care. Invented, not copied: a fixture is read
#: by strangers, and these are somebody's actual purchases.
CORPUS = [
    "Great Value 1% Low-Fat Milk, Gallon",
    "Great Value Large White Eggs, 18 Count",
    "Fresh Banana, Each",
    "Marketside Caesar Salad Kit, 14.55 oz Bag",
    "Kellogg's Raisin Bran Crunch Breakfast Cereal",
    "Ball Park 100% Beef Hot Dogs, 15 oz",
    "Great Value Concord Grape Jelly, 30 oz",
    "Doritos Tortilla Chips Cool Ranch, 9.25 oz",
    "Fresh Seedless Watermelon, Each",
    "Great Value Spaghetti, 32 oz",
    "Prima Della Cracked Pepper Turkey Breast, Deli-Sliced",
    "Diet Mountain Dew Citrus Soda Pop, 2 Liter Bottle",
    "Angel Soft 2-Ply Toilet Paper, 24 Rolls",
    "Cascade Complete Dishwasher Detergent Liquid Gel",
    "Great Value Ultra Strong Paper Towels, 12 Double Rolls",
    "Suave Moroccan Oil Infusion Shampoo & Conditioner",
    "Nature Made Vitamin D3 2000 IU Softgels",
    "Crayola Ultra Clean Washable Markers, 10 Count",
    "Anker USB-C to USB-C Charger Cable, 6 ft",
    "NICETOWN Blackout Window Curtain Panels, 2 Pack",
]


def test_the_table_classifies_the_overwhelming_majority_of_a_realistic_basket():
    """A regression gate on coverage, not on any single bucket.

    The table this replaced left 59% of real lines unclassified — the report
    built on it was mostly one bar labelled "Uncategorised". An edit that
    silently guts coverage again should fail here rather than be discovered in
    a rendered PDF.
    """
    missed = [t for t in CORPUS if classify(t) == "Uncategorized"]
    assert len(missed) / len(CORPUS) <= 0.10, f"unclassified: {missed}"


# ── the agent read boundary (lives here as the shared connector-level guard) ──
def test_pii_columns_the_connectors_hold_are_denied_to_the_agent():
    """Card last-4 and the imported filename must not reach the agent.

    `sanitize.redact_account_numbers` masks runs of 7+ digits, so a 4-digit
    card fragment survives every downstream scrub — the deny list is the
    control. The filename rule already existed for `import_runs.source_name`
    and `inbox_files.filename`; a download is routinely named after its owner
    and account, and the connectors must not reintroduce the same leak under a
    different column.
    """
    from local_budget.db import _AGENT_READ_DENY
    for pair in (("walmart_orders", "payment_method"),
                 ("amazon_orders", "payment_method"),
                 ("amazon_transactions", "payment_method"),
                 ("walmart_sync_runs", "scope")):
        assert pair in _AGENT_READ_DENY, f"{pair} is readable by the agent"
