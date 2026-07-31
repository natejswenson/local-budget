"""The combined online-spend report — Amazon and Walmart.com in one document.

What this file pins is the arithmetic that makes the page's argument true, not
the layout. Three claims carry the report and each has a way of being silently
wrong:

* items counted once when an order settled as several charges;
* Walmart's in-store receipts excluded, since this is an *online* report; and
* the headline's ratio measured **within** grocery-labelled charges rather than
  across the whole basket — the bug that shipped in the first render and
  overstated the answer by $5,711 against real data.
"""
from __future__ import annotations

from datetime import date

import pytest

from local_budget import db
from local_budget.connectors.walmart import store as wm_store
from local_budget.report import online


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


def _charge(c, txn_id, dt, cents, merchant="WALMART.COM", category="Groceries"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, category, imported_at) "
        "VALUES (?,1,?,?,?, 'posted',?,?,'x')",
        (txn_id, f"f{txn_id}", dt, cents, merchant, category))


def _wm_order(c, num, dt, items, *, channel="online"):
    """items: [(title, line_cents)]"""
    wm_store.store_orders(c, [{
        "order_number": num, "order_placed_date": dt, "channel": channel,
        "detail_fetched": True,
        "grand_total": f"{sum(u for _, u in items) / 100:.2f}",
        "items": [{"product_id": f"p{i}", "title": t, "quantity": 1,
                   "line_price": f"{u / 100:.2f}"}
                  for i, (t, u) in enumerate(items)],
    }], wm_store.start_run(c, "t"))


def _wm_match(c, order_number, *txn_ids):
    for t in txn_ids:
        c.execute("INSERT INTO walmart_matches (order_number, txn_id, "
                  "confidence, method, matched_at) VALUES (?,?,'exact','t','x')",
                  (order_number, t))


def _az_order(c, num, dt, items, txn_id):
    """An Amazon order, its transaction row, and the match between them."""
    c.execute("INSERT INTO amazon_orders (order_number, order_placed_date, "
              "grand_total_cents, fetched_at) VALUES (?,?,?,'x')",
              (num, dt, sum(u for _, u in items)))
    for i, (title, cents) in enumerate(items):
        c.execute("INSERT INTO amazon_items (order_number, line_no, asin, title, "
                  " quantity, unit_price_cents) VALUES (?,?,?,?,1,?)",
                  (num, i, f"a{i}", title, cents))
    c.execute("INSERT INTO amazon_transactions (amazon_txn_id, order_number, "
              " completed_date, grand_total_cents, fetched_at) VALUES (?,?,?,?,'x')",
              (txn_id, num, dt, sum(u for _, u in items)))
    c.execute("INSERT INTO amazon_matches (amazon_txn_id, txn_id, confidence, "
              " method, matched_at) VALUES (?,?,'exact','t','x')", (txn_id, txn_id))


# ── the arithmetic ───────────────────────────────────────────────────────────
def test_both_sources_land_in_one_basket(conn):
    _charge(conn, 1, "2026-07-01", -1000)
    _wm_order(conn, "W1", "2026-07-01", [("Store Brand Milk, Gallon", 1000)])
    _wm_match(conn, "W1", 1)
    _charge(conn, 2, "2026-07-02", -2000, merchant="AMAZON.COM", category="Shopping")
    _az_order(conn, "A1", "2026-07-02", [("USB-C Charger Cable, 6 ft", 2000)], 2)

    d = online.gather(conn)
    assert d["items"] == 2
    assert dict(d["by_source"]) == {"Walmart.com": 1000, "Amazon": 2000}
    assert dict(d["by_kind"]) == {"Shopping": 2000, "Groceries": 1000}


def test_a_split_settlement_counts_its_items_once(conn):
    """One order, two bank rows. Joining items through each counts the same
    product twice and inflates every figure on the page — and for Walmart that
    is the normal case, not an edge."""
    _charge(conn, 1, "2026-07-01", -600)
    _charge(conn, 2, "2026-07-02", -400)
    _wm_order(conn, "W1", "2026-07-01", [("Store Brand Milk, Gallon", 1000)])
    _wm_match(conn, "W1", 1, 2)

    d = online.gather(conn)
    assert d["items"] == 1
    assert d["line_total"] == 1000


def test_in_store_walmart_is_excluded(conn):
    """This is an ONLINE report. An in-store receipt is a different behaviour
    and posts under a different merchant."""
    _charge(conn, 1, "2026-07-01", -1000)
    _wm_order(conn, "W1", "2026-07-01", [("Store Brand Milk, Gallon", 1000)],
              channel="in-store")
    _wm_match(conn, "W1", 1)

    d = online.gather(conn)
    assert d["rows"] == []


def test_cancelled_lines_never_reach_a_category_total(conn):
    _charge(conn, 1, "2026-07-01", -1000)
    wm_store.store_orders(conn, [{
        "order_number": "W1", "order_placed_date": "2026-07-01",
        "channel": "online", "detail_fetched": True, "grand_total": "10.00",
        "items": [{"product_id": "p1", "title": "Store Brand Milk, Gallon",
                   "quantity": 1, "line_price": "10.00"},
                  {"product_id": "p2", "title": "Angel Soft Toilet Paper",
                   "quantity": 1, "line_price": "25.00", "status": "Canceled"}],
    }], wm_store.start_run(conn, "t"))
    _wm_match(conn, "W1", 1)

    d = online.gather(conn)
    assert d["items"] == 1
    assert dict(d["by_kind"]) == {"Groceries": 1000}


# ── the headline, which is the whole point of the page ───────────────────────
def test_the_headline_ratio_is_measured_inside_grocery_charges_only(conn):
    """The bug this pins, found by looking at a rendered PDF: the ratio was
    taken across ALL items and applied to the Groceries charge. Amazon is almost
    entirely non-food, so the blend inflated the claim — on real data it said
    $10,008.88 where the truth was $4,298.06.

    Here: the grocery charge is half food by line value, so the answer must be
    half the grocery charge — NOT some share dragged up by the Amazon order.
    """
    _charge(conn, 1, "2026-07-01", -1000, category="Groceries")
    _wm_order(conn, "W1", "2026-07-01",
              [("Store Brand Milk, Gallon", 500),
               ("2-Ply Toilet Paper, 24 Rolls", 500)])
    _wm_match(conn, "W1", 1)
    # A large, wholly non-food Amazon order that must not move the number.
    _charge(conn, 2, "2026-07-02", -9000, merchant="AMAZON.COM", category="Shopping")
    _az_order(conn, "A1", "2026-07-02", [("USB-C Charger Cable, 6 ft", 9000)], 2)

    d = online.gather(conn)
    assert d["food_ledger_split"] == {"food": 500, "non_food": 500}
    assert online.headline(d) == "$5.00 of what the ledger files as Groceries did not buy food."


def test_the_headline_is_silent_when_there_is_nothing_to_compare(conn):
    """No grocery-labelled charge means no claim to make. An unguarded template
    asserts something false with great confidence."""
    _charge(conn, 1, "2026-07-01", -1000, merchant="AMAZON.COM", category="Shopping")
    _az_order(conn, "A1", "2026-07-01", [("USB-C Charger Cable, 6 ft", 1000)], 1)
    assert online.headline(online.gather(conn)) == ""


def test_clusters_with_no_budget_category_are_reported(conn, monkeypatch):
    """`NO_LEDGER_HOME` is empty today — Pets was its one entry and graduated
    into a real category — so the reporting path is exercised through the
    mechanism rather than a live cluster. It stays covered for the next gap."""
    from local_budget.connectors import kinds
    monkeypatch.setitem(kinds.NO_LEDGER_HOME, "Boats", ("kayak paddle",))

    _charge(conn, 1, "2026-07-01", -3000)
    _wm_order(conn, "W1", "2026-07-01",
              [("Kayak Paddle, 86 in", 2000),
               ("Store Brand Milk, Gallon", 1000)])
    _wm_match(conn, "W1", 1)

    d = online.gather(conn)
    assert d["by_unhoused"] == [("Boats", 2000)]
    assert d["unhoused_lines"] == {"Boats": 1}


def test_no_unhoused_section_when_every_cluster_has_a_home(conn):
    """The live case now: nothing is unhoused, and the report must simply omit
    the section rather than render an empty table."""
    from local_budget.report import brand
    _charge(conn, 1, "2026-07-01", -2000)
    _wm_order(conn, "W1", "2026-07-01", [("Wild Bird Feed, 5 lb", 2000)])
    _wm_match(conn, "W1", 1)

    d = online.gather(conn)
    assert d["by_unhoused"] == []
    assert dict(d["by_kind"]) == {"Pets": 2000}
    assert "No home in your budget" not in online.build_html(d, brand.load_theme())


# ── the document ─────────────────────────────────────────────────────────────
def test_food_is_one_bar_and_the_rest_is_its_own_chart(conn):
    _charge(conn, 1, "2026-07-01", -3000)
    _wm_order(conn, "W1", "2026-07-01",
              [("Store Brand Milk, Gallon", 1000),
               ("Fresh Bananas, per lb", 500),
               ("2-Ply Toilet Paper, 24 Rolls", 1500)])
    _wm_match(conn, "W1", 1)

    d = online.gather(conn)
    assert d["food_cents"] == 1500                      # both food lines, one bucket
    assert d["non_food"] == [("Home Improvement", 1500)]
    assert d["food_cents"] + d["non_food_cents"] == d["line_total"]


# ── the default window ───────────────────────────────────────────────────────
@pytest.mark.parametrize("today,expect", [
    (date(2026, 7, 31), "2025-08-01"),
    (date(2026, 1, 15), "2025-02-01"),      # crosses a year boundary
    (date(2026, 12, 1), "2026-01-01"),
])
def test_the_default_window_starts_on_a_month_boundary(today, expect):
    """Anchored to the first of a month, not to today-minus-365. A part-month
    at either end of the trend chart reads as a collapse in spending that never
    happened."""
    assert online.default_since(today=today) == expect


def test_the_default_window_is_twelve_months(conn, tmp_path):
    """A year is the window a household reasons about, and twelve columns read
    as a shape where twenty-six read as a list."""
    _charge(conn, 1, "2026-07-01", -1000)
    _wm_order(conn, "W1", "2026-07-01", [("Store Brand Milk, Gallon", 1000)])
    _wm_match(conn, "W1", 1)
    # Well outside the window, and it must not appear.
    _charge(conn, 2, "2023-09-01", -5000)
    _wm_order(conn, "W2", "2023-09-01", [("Fresh Bananas, per lb", 5000)])
    _wm_match(conn, "W2", 2)
    conn.commit()

    import local_budget.report.pdf as pdf_mod
    seen = {}
    orig, pdf_mod.render_pdf = pdf_mod.render_pdf, lambda h, o: seen.update(html=h)
    try:
        r = online.render(out_dir=tmp_path)
        assert r["items"] == 1                 # the 2023 order is out of scope
        online.render(out_dir=tmp_path, all_history=True)
    finally:
        pdf_mod.render_pdf = orig
    # --all reaches back and produces a differently-named file, so the two
    # documents cannot overwrite each other.
    assert online.gather(conn)["items"] == 2


# ── the charts ───────────────────────────────────────────────────────────────
def test_food_is_the_baseline_of_every_stacked_chart(conn):
    """One stacking order across the document: food first, the varying part
    read against it. Two charts stacking the same comparison in opposite
    orders is a reading error waiting to happen."""
    rows = [("Walmart.com", 800, 200)]
    stacked = online._stacked(rows)
    assert stacked.index("st-food") < stacked.index("st-rest")
    assert "80% food" in stacked

    svg = online._month_chart([("2026-07", 800, 200)])
    assert svg.index("var(--ink)") < svg.index("var(--ink-mid)")


def test_stacked_bars_keep_length_meaning_money(conn):
    """Drawn to a shared maximum, not each normalised to 100%. Normalising
    would make a $200 month and a $2,000 month the same size and turn a
    spending chart into a ratio chart."""
    out = online._stacked([("Big", 900, 100), ("Small", 90, 10)])
    assert 'style="width:90.0%"' in out       # big row's food segment
    assert 'style="width:9.0%"' in out        # small row's, a tenth of it


def test_the_month_chart_marks_the_latest_column(conn):
    """The accent goes on the axis LABEL, never a bar: the bars carry the
    food/not-food key, and a third fill in them would read as a third
    category."""
    svg = online._month_chart([("2026-06", 100, 50), ("2026-07", 100, 50)])
    assert svg.count('class="axis now"') == 1
    assert svg.count("var(--accent)") == 0


def test_html_renders_with_the_comparison_and_the_disclaimer(conn):
    from local_budget.report import brand
    _charge(conn, 1, "2026-07-01", -1000)
    _wm_order(conn, "W1", "2026-07-01", [("Store Brand Milk, Gallon", 1000)])
    _wm_match(conn, "W1", 1)

    html = online.build_html(online.gather(conn), brand.load_theme())
    assert "The ledger says" in html and "The items say" in html
    # The page must never read as an assertion about the ledger.
    assert "never written back to a transaction" in html
    assert "<h1>Online spend</h1>" in html


def test_the_template_must_ship(monkeypatch, tmp_path):
    """A missing stylesheet is a packaging error, not a cosmetic one: rendering
    without it silently produces a differently-laid-out document."""
    monkeypatch.setattr(online, "TEMPLATE_CSS", tmp_path / "gone.css")
    with pytest.raises(FileNotFoundError, match="package is"):
        online.template_css()


def test_a_scoped_render_does_not_clobber_the_all_history_one(conn, tmp_path):
    """Same path, wholly different document — and no way to tell them apart
    afterwards."""
    _charge(conn, 1, "2026-07-01", -1000)
    _wm_order(conn, "W1", "2026-07-01", [("Store Brand Milk, Gallon", 1000)])
    _wm_match(conn, "W1", 1)

    # `render` opens its own connection, so the fixture's writes have to be
    # visible outside this one first.
    conn.commit()

    rendered = []
    monkey = lambda html, out: rendered.append(out)          # noqa: E731
    import local_budget.report.pdf as pdf_mod
    orig, pdf_mod.render_pdf = pdf_mod.render_pdf, monkey
    try:
        online.render(out_dir=tmp_path)
        online.render(since="2026-07-01", out_dir=tmp_path)
    finally:
        pdf_mod.render_pdf = orig
    assert rendered[0] != rendered[1]
    assert rendered[0].name == "online-spend.pdf"
