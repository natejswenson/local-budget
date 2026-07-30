"""The standalone Walmart report.

Pins the same defects the Amazon report was written against — a split-shipment
double-count, a fixed output path that clobbers its own output, an all-time
footer on a scoped document, a template that must ship — plus the two claims
this page makes that Amazon's does not: a channel split, and an honest statement
of how much of the page rests on an inferred charge date.
"""
from __future__ import annotations

import pytest

from local_budget import db
from local_budget.connectors.walmart import report, store


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


def _charge(c, txn_id, dt, cents, merchant="WALMART.COM"):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, category, imported_at) "
        "VALUES (?,1,?,?,?, 'posted',?,'Shopping','x')",
        (txn_id, f"f{txn_id}", dt, cents, merchant))


def _order(c, num, dt, items, *, channel="online", charges=None):
    """items: [(product_id, title, unit_cents, qty, seller, category)]"""
    o = {"order_number": num, "order_placed_date": dt, "channel": channel,
         "detail_fetched": True, "payment_method": "Visa",
         "grand_total": f"{sum(u * q for _, _, u, q, _, _ in items) / 100:.2f}",
         "items": [{"product_id": p, "title": t, "quantity": q,
                    "unit_price": f"{u / 100:.2f}", "seller": s, "category": cat}
                   for p, t, u, q, s, cat in items],
         "charges": charges or []}
    store.store_orders(c, [o], store.start_run(c, "t"))


def _match(c, order_number, txn_id):
    cid = c.execute("SELECT walmart_charge_id i FROM walmart_charges "
                    "WHERE order_number=? ORDER BY charged_date LIMIT 1",
                    (order_number,)).fetchone()["i"]
    c.execute("INSERT INTO walmart_matches (walmart_charge_id, txn_id, "
              "confidence, method, matched_at) VALUES (?,?,'exact','t','x')",
              (cid, txn_id))
    return cid


# ── the arithmetic ───────────────────────────────────────────────────────────
def test_a_split_shipment_item_is_counted_once(conn):
    """One order, two charges, both matched. Joining items through each counts
    the same product twice and inflates every total on the page."""
    _order(conn, "O1", "2026-07-01",
           [("P1", "Patio umbrella", 14950, 1, "Walmart.com", None)],
           charges=[{"charged_date": "2026-07-01", "amount": "-100.00"},
                    {"charged_date": "2026-07-03", "amount": "-49.50"}])
    _charge(conn, 1, "2026-07-01", -10000)
    _charge(conn, 2, "2026-07-03", -4950)
    ids = [r["i"] for r in conn.execute(
        "SELECT walmart_charge_id i FROM walmart_charges ORDER BY charged_date")]
    for cid, txn in zip(ids, (1, 2)):
        conn.execute("INSERT INTO walmart_matches (walmart_charge_id, txn_id, "
                     "confidence, method, matched_at) VALUES (?,?,'exact','t','x')",
                     (cid, txn))
    d = report.gather(conn)
    assert d["items"] == 1
    assert d["line_total"] == 14950
    assert d["charge_total"] == 14950, "both charges count toward what was spent"


def test_charge_total_is_a_positive_outflow_not_a_negative(conn):
    """Rendering spend negated reads as a refund on every figure on the page."""
    _order(conn, "O", "2026-07-01", [("P", "Thing", 1000, 1, "W", None)])
    _charge(conn, 1, "2026-07-01", -1000)
    _match(conn, "O", 1)
    assert report.gather(conn)["charge_total"] == 1000


# ── the channel split ────────────────────────────────────────────────────────
def test_online_and_in_store_are_reported_separately(conn):
    """They are different spending behaviours with different explanations, and
    one bar hides that."""
    _order(conn, "ON", "2026-07-01", [("P1", "Cable", 2000, 1, "W", None)])
    _order(conn, "IN", "2026-07-02", [("P2", "Milk", 500, 1, "W", None)],
           channel="in-store")
    _charge(conn, 1, "2026-07-01", -2000)
    _charge(conn, 2, "2026-07-02", -500, merchant="WM SUPERCENTER FARGO")
    _match(conn, "ON", 1)
    _match(conn, "IN", 2)
    assert dict(report.gather(conn)["by_channel"]) == {"online": 2000, "in-store": 500}


# ── the "kind" grouping, and being honest about where it came from ───────────
def test_walmarts_own_category_is_preferred_over_the_keyword_guess(conn):
    """"Dog food" would key to Pets & backyard by keyword. Walmart said
    Grocery, and a published category beats an inferred one."""
    _order(conn, "O", "2026-07-01", [("P", "Dog food 24lb", 4299, 1, "W", "Grocery")])
    _charge(conn, 1, "2026-07-01", -4299)
    _match(conn, "O", 1)
    d = report.gather(conn)
    assert dict(d["by_kind"]) == {"Grocery": 4299}
    assert d["kinds_from_source"] == 1


def test_the_keyword_heuristic_fills_the_gap_when_walmart_publishes_nothing(conn):
    _order(conn, "O", "2026-07-01", [("P", "Dog food 24lb", 4299, 1, "W", None)])
    _charge(conn, 1, "2026-07-01", -4299)
    _match(conn, "O", 1)
    d = report.gather(conn)
    assert dict(d["by_kind"]) == {"Pets & backyard": 4299}
    assert d["kinds_from_source"] == 0


@pytest.mark.parametrize("src,total,expect", [
    (3, 3, "Walmart's own product categories"),
    (0, 3, "inferred from product titles by keyword"),
    (1, 3, "1 of 3 lines"),
])
def test_the_caption_states_the_mix_it_actually_got(src, total, expect):
    """Three different claims about the chart above. Printing the same hedge for
    all three is either a lie or a needless apology."""
    assert expect in report.kind_caption({"items": total, "kinds_from_source": src})


# ── derived charges: an inference must not read as an observation ────────────
def test_the_footer_discloses_charges_dated_from_the_order(conn):
    _order(conn, "O", "2026-07-01", [("P", "Thing", 1000, 1, "W", None)])
    _charge(conn, 1, "2026-07-01", -1000)
    _match(conn, "O", 1)
    html = report.build_html(report.gather(conn), report.brand.load_theme())
    assert "dated from the order rather than from a payment line" in html


def test_no_disclosure_when_every_charge_was_observed(conn):
    _order(conn, "O", "2026-07-01", [("P", "Thing", 1000, 1, "W", None)],
           charges=[{"charged_date": "2026-07-01", "amount": "-10.00"}])
    _charge(conn, 1, "2026-07-01", -1000)
    _match(conn, "O", 1)
    html = report.build_html(report.gather(conn), report.brand.load_theme())
    assert "dated from the order" not in html


# ── scoping ──────────────────────────────────────────────────────────────────
def test_coverage_in_the_footer_is_scoped_to_the_same_window_as_the_page(conn):
    """An all-time figure on a two-month report describes a different document
    than the one the reader is holding."""
    _order(conn, "O", "2026-07-01", [("P", "Thing", 1000, 1, "W", None)])
    _charge(conn, 1, "2026-07-01", -1000)
    _charge(conn, 2, "2026-01-15", -50000)          # outside the window
    _match(conn, "O", 1)
    d = report.gather(conn, since="2026-06-01")
    assert d["scoped_total_cents"] == 1000
    assert d["scoped_pct"] == 100.0


def test_the_horizon_line_is_omitted_from_a_scoped_report(conn):
    """It is a property of the whole dataset; on a scoped page it describes
    something else."""
    _order(conn, "O", "2026-07-01", [("P", "Thing", 1000, 1, "W", None)])
    _charge(conn, 1, "2026-07-01", -1000)
    _charge(conn, 2, "2024-01-15", -5000)
    _match(conn, "O", 1)
    scoped = report.build_html(report.gather(conn, since="2026-06-01"),
                               report.brand.load_theme())
    full = report.build_html(report.gather(conn), report.brand.load_theme())
    assert "predate any order record" not in scoped
    assert "predate any order record" in full


@pytest.mark.parametrize("evil", [
    "../../etc/passwd", "a/b", "..\\..\\win", "; rm -rf /", "a b|c",
])
def test_filename_stem_cannot_escape_the_reports_directory(evil):
    """`since`/`until` reach the filename from the command line."""
    safe = report._safe(evil)
    assert "/" not in safe and "\\" not in safe and ".." not in safe
    assert all(c.isalnum() or c in "-_" for c in safe)


def test_empty_range_refuses_rather_than_rendering_a_blank_report(conn):
    with pytest.raises(ValueError, match="no reconciled Walmart items"):
        report.render(since="2099-01-01")


# ── the template is a tracked, shipped artifact ──────────────────────────────
def test_template_css_exists_and_ships_with_the_package():
    """A missing template is a PACKAGING error. Rendering without it produces a
    silently different-looking document rather than a failure, which is exactly
    the kind of break that reaches a reader before it reaches a developer."""
    assert report.TEMPLATE_CSS.is_file(), report.TEMPLATE_CSS
    assert report.TEMPLATE_CSS.parent.name == "assets"
    assert report.template_css().strip(), "template is empty"


def test_template_is_tracked_in_git():
    """It is the report's look. An untracked template means the next checkout
    renders a different document, and nobody can review a change to it."""
    import subprocess
    root = report.TEMPLATE_CSS.parents[5]   # assets->walmart->connectors->local_budget->src->repo
    rel = report.TEMPLATE_CSS.relative_to(root)
    out = subprocess.run(["git", "ls-files", "--error-unmatch", str(rel)],
                         cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, f"{rel} is not tracked in git"


def test_a_missing_template_raises_instead_of_rendering_unstyled(monkeypatch, tmp_path):
    monkeypatch.setattr(report, "TEMPLATE_CSS", tmp_path / "gone.css")
    with pytest.raises(FileNotFoundError, match="missing report template"):
        report.template_css()


def test_template_carries_layout_only_never_colour():
    """Colour belongs to the PRESS brand, which is overridable in one place.
    A hex here would fork the palette and quietly desynchronise the two."""
    css = report.template_css()
    for literal in ("#F5F0E6", "#181510", "#E8501F", "#6E675C"):
        assert literal not in css, f"{literal} belongs in brand.py, not the template"


def test_the_rendered_page_actually_includes_the_template(conn):
    _order(conn, "O", "2026-07-01", [("P", "Thing", 1000, 1, "W", None)])
    _charge(conn, 1, "2026-07-01", -1000)
    _match(conn, "O", 1)
    html = report.build_html(report.gather(conn), report.brand.load_theme())
    # a rule that exists ONLY in the template, not in the shared stylesheet
    assert "break-after: avoid" in html


def test_titles_are_clipped_at_a_word_boundary(conn):
    """A hard slice cuts mid-word and reads as corruption rather than as an
    abbreviation."""
    assert report._clip("Mainstays 5-Shelf Bookcase, Black Oak", 20) == "Mainstays 5-Shelf…"
