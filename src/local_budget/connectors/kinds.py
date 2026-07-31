"""Product title → a budget category, by keyword. Shared by the purchase reports.

**There is no product category in a scraped order title.** Amazon exposes none at
all; Walmart sometimes does and often does not. So every report needs the same
fallback, and it lives here rather than in any one connector — one keyword table
maintained once, instead of three that drift apart and bucket the same item three
different ways in three documents.

**The buckets are the ledger's own category names, not a taxonomy of their own.**
That is the whole point of this table. The ledger pins merchants, not items:
`WALMART.COM → Groceries` and `AMAZON → Shopping` describe every charge from
those merchants, toilet paper and vitamins included. Grouping items into the same
vocabulary the budget is set in is what lets a report say *how much of the
grocery bill was not food* — a question a parallel set of product-shaped buckets
could pose but never answer.

**Order is the design, not an accident.** Non-food rules run FIRST and the food
net runs LAST, because the food net has to be broad enough to swallow a grocery
run and a broad net catches everything. `Cascade Dishwasher Detergent` reaches
Home Improvement before any food keyword sees it; `Great Value Baking Chips`
finds no non-food rule and falls through to Groceries. Reversing the two would
put the detergent in the groceries.

**Food is deliberately ONE bucket.** Breaking a grocery run into produce, dairy
and snacks is detail no budget decision turns on — the food line is the food
line. What matters is what was in the basket that ISN'T food, which is why the
non-food rules are the specific ones and carry all the maintenance.

It is kept visible and auditable rather than hidden behind a model call: a wrong
bucket should be something a reader can spot and correct, not something they have
to trust. **Nothing here writes to the ledger.** These groupings are a reading of
product titles; assigning a category to a transaction stays a judgment the agent
states explicitly and a human confirms. Every report that renders these says so
on its face.
"""
from __future__ import annotations

#: The ledger's spelling for "we could not tell". Deliberately NOT `Random`:
#: that is a junk drawer Nate is actively shrinking, and defaulting into it
#: defeats the point of categorising. An honest blank beats a wrong home.
UNCATEGORISED = "Uncategorized"

#: Buckets below that are NOT builtin categories — they exist only if the user
#: added them (`categories.add_custom_category`).
#:
#: Declared rather than discovered, because the failure is silent otherwise: on
#: a ledger without these, the reports still render a bar labelled "Home
#: Improvement" that corresponds to no budget line, and the premise that item
#: spend can be read against something you budget quietly stops holding. Naming
#: them here keeps the dependency visible and lets a test pin it without
#: reaching for a database.
REQUIRES_CUSTOM_CATEGORY = frozenset({"Home Improvement", "Kid Activities"})

#: Keyword → budget category, first match wins. Every bucket name here must be a
#: real category from `categories.list_categories`, or a report would group spend
#: under a line the budget cannot show it against.
#:
#: Patterns are matched against the title padded with a leading space, so a
#: pattern can anchor a word start (`" dog "`) instead of also matching "hot dog".
KINDS: list[tuple[str, tuple[str, ...]]] = [
    # ── non-food first: specific, and they must outrank the food net ────────
    ("Personal Care", (
        "shampoo", "conditioner", "body wash", "deodorant", "razor", "shave",
        "toothpaste", "toothbrush", "floss", "mouthwash", "lotion", "moisturi",
        "facial", "face wash", "acne", "serum", "sunscreen", "makeup", "mascara",
        "lipstick", "nail polish", "nail clipper", "hair color", "hair spray",
        "hairspray", "hairbrush", "perfume", "cologne", "cotton round",
        "cotton ball", "q-tip", "tampon", "feminine", "diaper", "wipes",
        "body spray", "hand soap", "bar soap", "styling")),
    ("Health", (
        "vitamin", "supplement", "ibuprofen", "acetaminophen", "tylenol",
        "advil", "aspirin", "allergy", "claritin", "zyrtec", "benadryl",
        "cough", "cold & flu", "bandage", "band-aid", "first aid", "gauze",
        "thermometer", "medicine", "probiotic", "melatonin", "omega",
        "fish oil", "collagen", "electrolyte", "antacid", "tums", "pepto",
        "prescription", "knee brace", "elbow sleeve", "compression",
        "massager", "heating pad", "contact lens", "reading glasses",
        "blood pressure", "pill organizer", "mouthguard")),
    ("Home Improvement", (
        # cleaning and consumable household
        "paper towel", "toilet paper", "detergent", "dish soap", "dishwasher",
        "cleaner", "cleaning", "bleach", "disinfect", "lysol", "clorox",
        "trash bag", "garbage bag", "freezer bag", "storage bag", "sandwich bag",
        "aluminum foil", "plastic wrap", "parchment", "sponge", "scrub",
        "mop", "broom", "vacuum", "air freshener", "fabric softener",
        "dryer sheet", "laundry", "stain remover",
        # repair and hardware
        "air filter", "furnace", "light bulb", "battery", "batteries",
        "screw", "nail gun", "hammer", "tool", "drill", "wrench", "pliers",
        "duct tape", "masking tape", "glue", "adhesive", "caulk", "sandpaper",
        "extension cord", "power strip", "smoke detector", "thermostat",
        # furnishing, decor and storage
        "curtain", "rug", "lamp", "pillow", "blanket", "sheet set", "comforter",
        "bedding", "mattress", "furniture", "desk", "bookcase", "shelf",
        "shelving", "storage bin", "storage tote", "organizer", "hanger",
        "mirror", "picture frame", "wall art", "wall decor", "candle", "vase",
        "doormat", "shower curtain", "hand towel", "bath towel", "trash can",
        "laundry basket", "hamper", "chair", "stool", "ottoman", "floor fan",
        "ceiling fan", "box fan", "space heater", "humidifier",
        # cookware, listed HERE and not under the food net: a baking sheet is a
        # pan, not an ingredient, and "baking" is a food keyword.
        "baking sheet", "baking pan", "cake pan", "steam pan", "skillet",
        "saucepan", "stock pot", "cookware", "bakeware", "utensil",
        "cutting board", "mixing bowl", "measuring cup", "spatula", "whisk",
        "colander", "food storage container", "tumbler", "travel mug")),
    ("Kid Activities", (
        " toy", "toys", "lego", "puzzle", "doll", "board game", "fidget",
        "craft kit", "coloring", "crayon", "play-doh", "playset",
        "stuffed animal", "kids", "children", "toddler", "youth", "baby gate",
        "car seat", "stroller", "softball", "baseball bat", "volleyball",
        "basketball", "soccer", "pickleball", "kayak", "scooter", "sled",
        "backpack", "lunch bag", "lunch box", "binder", "notebook", "pencil",
        "crayola", "glue stick", "index card", "school", "stem toy",
        "activity book", "flash card")),
    ("Entertainment", (
        "kindle", " dvd", "blu-ray", "video game", "playstation", "xbox",
        "nintendo", "card game", "chess", "guitar", "ukulele", "drum",
        "percussion", "instrument", "paperback", "hardcover", "audiobook",
        # "book" is safe this far down the list: "notebook" and "lunch box" are
        # already claimed by Kid Activities, and "bookcase" by Home Improvement.
        "book", "bible", "novel", "journal")),
    ("Shopping", (
        # clothing and footwear
        "shirt", "sock", "shoe", "sneaker", "jacket", "glove", "legging",
        "dress", "pajama", "slipper", "boot", "sandal", "croc", "vest",
        "hoodie", "sweater", "pants", "shorts", "swimsuit", "underwear",
        "belt", "scarf", "beanie", "cap,",
        # tech and accessories
        "cable", "charger", "usb", "hdmi", "adapter", "headphone", "earbud",
        "mouse", "keyboard", "router", "camera", "speaker", "tablet",
        "phone case", "screen protector", "printer", "monitor", "webcam",
        "smart watch", "airtag", "sd card", "flash drive",
        # personal effects and outdoor gear
        "jewelry", "necklace", "earring", "bracelet", "purse", "handbag",
        "wallet", "luggage", "suitcase", "sunglasses", "watch band",
        "patio", "umbrella", "cooler", "camping", "tent", "grill", "lawn",
        "planter", "garden", "air mattress", "inflatable", "pool", "float")),

    # ── the food net, LAST and deliberately broad ────────────────────────────
    # It only ever sees titles no non-food rule claimed, so it can afford to be
    # generous. Anything edible or drinkable is one bucket by design.
    ("Groceries", (
        "cereal", "bread", "milk", "egg", "cheese", "yogurt", "butter",
        "meat", "beef", "chicken", "pork", "turkey", "bacon", "sausage",
        "ham,", "fish", "salmon", "shrimp", "tuna", "fresh ", "produce",
        "fruit", "vegetable", "lettuce", "salad", "tomato", "potato", "onion",
        "banana", "apple", "berr", "grape", "melon", "corn", "bean", "carrot",
        "pasta", "spaghetti", "noodle", "rice", "flour", "sugar", "salt",
        "spice", "seasoning", "sauce", "soup", "broth", "gravy", "dressing",
        "salsa", "dip,", "syrup", "honey", "vinegar", "olive oil", "cooking oil",
        "snack", "chip", "cracker", "cookie", "candy", "chocolate", "pretzel",
        "popcorn", "granola", "oat", "almond", "peanut butter", "jelly", "jam,",
        "ice cream", "frozen", "pizza", "waffle", "pancake", "muffin", "cake",
        "pie,", "donut", "brownie", "pudding", "jello", "whipped",
        "juice", "soda", "cola", "coffee", "tea,", "water,", "drink",
        "lemonade", "sparkling", "seltzer", "creamer", "half and half",
        "tortilla", "bun,", "roll", "bagel", "biscuit", "croissant",
        "cheez", "dorito", "oreo", "ritz", "goldfish", "hot dog", "burger",
        "deli", "lunch meat", "macaroni", "mac & cheese", "baking", "yeast",
        "gum,", "mint", "food")),
]

#: Clusters the budget has NO category for. Reported as a recommendation rather
#: than forced into the nearest bucket — pet supplies are real, recurring spend,
#: and calling them "Shopping" hides a line worth budgeting.
#:
#: A suggestion table, not a classification one: `classify` never returns these
#: names, because a report must not group spend under a category the budget
#: cannot show it against. Only `unhoused` reads it.
#: Every pattern is qualified — "dog collar", never a bare "dog". A grocery run
#: is full of hot dogs, and a bare pattern would recommend a Pets budget on the
#: strength of a pack of buns.
NO_LEDGER_HOME: dict[str, tuple[str, ...]] = {
    "Pets": ("mealworm", "bird seed", "birdseed", "bird feed", "wild bird",
             "suet", "feeder", "for dogs", "for cats", "dog food", "dog treat",
             "dog collar", "dog fence", "dog training", "dog brush",
             "dog slicker", "dog bed", "dog toy", "dog chew", "puppy",
             "cat food", "cat litter", "cat treat", "kitty", "pet food",
             "pet rabbit", "aquarium", "hamster", "guinea pig", "gerbil",
             "chicken feed", "leash", "dewormer", "chew toy"),
}


def _haystack(title: str | None) -> str:
    """Lowercased title with a leading space, so a pattern can anchor a word
    start (`" dog "`) instead of also matching the middle of another word."""
    return " " + (title or "").lower()


def classify(title: str | None) -> str:
    """A product title → a budget category name, or `Uncategorized`."""
    t = _haystack(title)
    for kind, pats in KINDS:
        if any(p in t for p in pats):
            return kind
    return UNCATEGORISED


def unhoused(title: str | None) -> str | None:
    """The candidate category this item wants but the budget does not have.

    Independent of `classify` on purpose: an item can be both `Shopping` — where
    the ledger can actually show it today — and a Pets candidate, where it
    belongs. A report adds up by the first and recommends by the second.
    """
    t = _haystack(title)
    for cluster, pats in NO_LEDGER_HOME.items():
        if any(p in t for p in pats):
            return cluster
    return None
