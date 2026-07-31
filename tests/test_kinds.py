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
    line, which is the whole point of classifying into these names.

    Checked against the BUILTIN vocabulary plus an explicit list of buckets that
    need a custom category, rather than against `all_categories()` — that reads
    the `settings` table, so the assertion would silently become "whatever this
    machine's database happens to contain". It passed locally against a real
    ledger and failed in CI, which is the same bug in both directions.
    """
    from local_budget import categories
    allowed = categories.CATEGORIES | kinds.REQUIRES_CUSTOM_CATEGORY
    for name, _ in KINDS:
        assert name in allowed, f"{name!r} is not an assignable category"
    assert kinds.UNCATEGORISED in categories.CATEGORIES


def test_the_custom_category_dependency_is_declared_and_accurate():
    """`REQUIRES_CUSTOM_CATEGORY` must list exactly the non-builtin buckets.

    Stale either way is a trap: a bucket missing from it fails the check above
    for the wrong reason, and a stale entry claims a dependency that no longer
    exists once the category is promoted to builtin.
    """
    from local_budget import categories
    non_builtin = {n for n, _ in KINDS if n not in categories.CATEGORIES}
    assert non_builtin == kinds.REQUIRES_CUSTOM_CATEGORY


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
    ("Automatic Dishwasher Detergent Liquid Gel, 90 oz", "Home Improvement"),
    ("Vanilla Flavored Baking Chips, 24 oz Bag", "Groceries"),
    ("Non-Stick Baking Sheet, 10x15 in", "Home Improvement"),
    ("Anti-Dandruff 2-in-1 Shampoo, 28 oz", "Personal Care"),
    # A representative sweep of the rest.
    ("Vitamin D3 Softgels, 2000 IU", "Health"),
    ("2-Ply Toilet Paper, 24 Rolls", "Home Improvement"),
    ("Zip Tab School Zipper Binder", "Kid Activities"),
    ("Women's Sheepskin Slipper, Size 8", "Shopping"),
    ("USB-C Charger Cable, 6 ft", "Shopping"),
    ("80% Lean Ground Beef Chuck, 1 lb Tray", "Groceries"),
    ("Large White Eggs, 18 Count", "Groceries"),
    ("Fresh Gala Apple, Each", "Groceries"),
    ("Diet Cola Soda Pop, 12 fl oz, 12 Pack Cans", "Groceries"),
])
def test_classifier_buckets(title, bucket):
    assert classify(title) == bucket


def test_a_word_boundary_pattern_does_not_match_mid_word():
    """Patterns anchor on a leading space, so " dvd" cannot match inside another
    word. Without it the table produces bafflingly wrong buckets that are very
    hard to trace back to a keyword."""
    assert classify("Advdisor Brand Mystery Item") == "Uncategorized"


# ── pets, which the budget now has a category for ────────────────────────────
@pytest.mark.parametrize("title", [
    "In-Shell Peanuts Wild Bird Feed, 5 lb",
    "Chewable Dewormer for Large Dogs",
    "Extreme Dog Fence Dog Collar",
    "Daily Blend Nutrition Diet for Hamsters and Gerbils",
    "Nylon Dog Leash, 6 ft",
])
def test_pet_supplies_classify_as_pets(title):
    assert classify(title) == "Pets"


@pytest.mark.parametrize("title,bucket", [
    # Each of these reads as a DIFFERENT bucket by keyword, which is why Pets
    # has to lead the table rather than sit among the other non-food rules.
    ("Oatmeal Dog Shampoo, 16 oz", "Pets"),        # else Personal Care
    ("Chicken & Rice Dog Food, 16.5 lb", "Pets"),   # else Groceries
    ("Power Chew Toy for Large Dogs", "Pets"),        # else Kid Activities
])
def test_pets_outranks_the_buckets_a_pet_item_would_otherwise_hit(title, bucket):
    assert classify(title) == bucket


def test_hot_dogs_are_not_pet_supplies():
    """The failure a bare "dog" pattern would cause — and it matters more now
    that Pets leads the table, because a false positive here would take a
    grocery line out of the food bucket entirely."""
    assert classify("Beef Hot Dogs, 8 Count") == "Groceries"
    assert classify("Frozen Corn Dogs, 16 Count") == "Groceries"
    assert classify("Hot Dog Buns, 8 Count") == "Groceries"


def test_nothing_is_unhoused_now_that_pets_has_a_category():
    """`NO_LEDGER_HOME` is empty, which is the mechanism having worked: Pets was
    its one entry, the category was added, and it graduated into KINDS. The
    machinery stays so the next gap surfaces the same way."""
    assert kinds.NO_LEDGER_HOME == {}
    assert unhoused("Wild Bird Feed, 5 lb") is None
    assert "Pets" in {name for name, _ in KINDS}


# ── coverage, asserted against a realistic basket ────────────────────────────
#: Generic product descriptions, written for this test — NOT lifted from anyone's
#: order history. A fixture is the artifact most likely to be read by strangers,
#: so it must not carry what a household actually bought. The proportions mirror
#: a real basket (mostly food, a tail of household and personal care); the items
#: do not.
CORPUS = [
    "Store Brand 1% Low-Fat Milk, Gallon",
    "Store Brand Large White Eggs, 18 Count",
    "Fresh Bananas, per lb",
    "Bagged Caesar Salad Kit, 12 oz",
    "Bran Flakes Breakfast Cereal, Family Size",
    "Beef Hot Dogs, 8 Count",
    "Store Brand Grape Jelly, 30 oz",
    "Tortilla Chips, Party Size Bag",
    "Fresh Seedless Watermelon, Each",
    "Store Brand Spaghetti, 32 oz",
    "Sliced Deli Turkey Breast, per lb",
    "Diet Lemon-Lime Soda, 2 Liter Bottle",
    "2-Ply Toilet Paper, 24 Rolls",
    "Automatic Dishwasher Detergent Gel, 90 oz",
    "Ultra Strong Paper Towels, 12 Rolls",
    "Daily Shampoo and Conditioner Value Pack",
    "Vitamin D3 Softgels, 2000 IU",
    "Washable Markers, 10 Count",
    "USB-C to USB-C Charger Cable, 6 ft",
    "Blackout Window Curtain Panels, 2 Pack",
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
