"""Product title → a rough bucket, by keyword. Shared by the purchase reports.

**There is no product category in a scraped order title.** Amazon exposes none
at all; Walmart sometimes does and often does not. So both reports need the same
fallback, and it lives here rather than in either connector — one keyword table
maintained once, instead of two that drift apart and bucket the same item two
different ways in two documents.

It is kept visible and auditable rather than hidden behind a model call: a wrong
bucket should be something a reader can spot and correct, not something they
have to trust. Both reports say on their face that these groups are a reading of
the data rather than a fact in it.
"""
from __future__ import annotations

#: Keyword → bucket, first match wins. Order matters: more specific patterns
#: come first so "canvas board" lands in art rather than office supplies.
KINDS: list[tuple[str, tuple[str, ...]]] = [
    ("School & office", ("binder", "calculator", "notebook", "planner", "pencil",
                         "backpack", "lunch bag", "index card", "stapler",
                         "folder", "printer paper", "sharpie")),
    ("Sports & rec", ("pickleball", "volleyball", "basketball", "soccer", "golf",
                      "tennis", "racket", "rebounder", "elbow sleeve", "knee pad",
                      "mouthguard", "cleat", "yoga", "dumbbell", "bike")),
    ("Outdoor & patio", ("patio", "umbrella", "cantilever", "outdoor", "cooler",
                         "beach", "camping", "tent", "grill", "lawn", "garden hose",
                         "planter")),
    ("Kids & toys", ("kids", " toy", "toys", "lego", "puzzle", "stem ",
                     "activity book", "board game", "chess", "fidget", "doll",
                     "craft kit", "airplane", "hidden pictures")),
    ("Art & hobby", ("canvas", "paint", "coloring", "yarn", "sketch", "marker",
                     "bead", "sticker", "glitter", "drum", "instrument")),
    ("Pets & backyard", ("bird", "peanut", "mealworm", "seed", "feeder", "dog",
                         "cat ", "pet ", "bedding", "aquarium", "chicken",
                         "dewormer", "leash", "litter")),
    ("Personal care", ("acne", "skin", "shampoo", "lotion", "nail", "hair",
                       "razor", "toothbrush", "deodorant", "cotton round",
                       "makeup", "serum", "sunscreen", "sanitizer", "perfume",
                       "conditioner", "moisturi")),
    ("Clothing & footwear", ("shirt", "sock", "shoe", "jacket", "hat", "glove",
                             "legging", "dress", "pajama", "slipper", "boot",
                             "sandal", "vest", "hoodie", "sweater", "pants",
                             "shorts", "swimsuit", "underwear", "bra ")),
    ("Storage & organisation", ("storage", "bin", "tote", "container", "organizer",
                                "shelf", "rack", "basket", "drawer", "hanger")),
    ("Home & decor", ("curtain", "rug", "frame", "lamp", "pillow", "blanket",
                      "sheet set", "towel", "backdrop", "wall art", "candle",
                      "mirror", "vase")),
    ("Kitchen & baking", ("scoop", "whisk", "zester", "grater", "spatula",
                          "baking", "cookie", "measuring", "mixing bowl",
                          "food storage", "mug", "utensil", "cutting board",
                          "skillet", "blender")),
    ("Home & repair", ("glue", "adhesive", "cement", "screw", "tool", "battery",
                       "light bulb", "hook", "tape", "filter", "cleaner",
                       "caulk", "sandpaper", "wrench", "drill")),
    ("Tech & cables", ("cable", "charger", "usb", "hdmi", "adapter", "headphone",
                       "earbud", "mouse", "keyboard", "drive", "router",
                       "photo printer", "camera", "speaker", "tablet")),
    ("Food & drink", ("candy", "snack", "coffee", "tea ", "protein", "cereal",
                      "sauce", "spice", "granola", "chocolate", "vitamin",
                      "supplement")),
]

UNCATEGORISED = "Uncategorised"


def classify(title: str | None) -> str:
    t = (title or "").lower()
    for kind, pats in KINDS:
        if any(p in t for p in pats):
            return kind
    return UNCATEGORISED
