"""The standalone Amazon report.

Written after six real defects were found by looking at rendered pages rather
than at code — a double-count, a sign inversion, orphaned headings, hard-sliced
titles, a fixed output path that clobbered its own output, and an all-time
footer on a scoped document. Each of those is pinned here, because the next one
will not be caught by eye once the novelty wears off.
"""
from __future__ import annotations

from datetime import date

import pytest

from local_budget import db
from local_budget.connectors.amazon import report, store


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


def _charge(c, txn_id, dt, cents):
    c.execute(
        "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
        " amount_cents, status, merchant_norm, category, imported_at) "
        "VALUES (?,1,?,?,?, 'posted','AMAZON MKTPL AMZN.COM','Shopping','x')",
        (txn_id, f"f{txn_id}", dt, cents))


def _order(c, num, dt, items):
    """items: [(asin, title, unit_cents, qty, seller)]"""
    c.execute("INSERT INTO amazon_orders (order_number, order_placed_date, "
              "grand_total_cents, cancelled, fetched_at) VALUES (?,?,?,0,'x')",
              (num, dt, sum(u * q for _, _, u, q, _ in items)))
    for i, (asin, title, unit, qty, seller) in enumerate(items):
        c.execute("INSERT INTO amazon_items (order_number, line_no, asin, title, "
                  "quantity, unit_price_cents, seller) VALUES (?,?,?,?,?,?,?)",
                  (num, i, asin, title, qty, unit, seller))


def _az_txn(c, dt, cents, order):
    store.store_transactions(
        c, [type("T", (), {"completed_date": date.fromisoformat(dt),
                           "grand_total": cents / 100, "is_refund": cents > 0,
                           "order_number": order, "payment_method": "Visa",
                           "seller": "Amazon"})()],
        store.start_run(c, "t"))
    return c.execute("SELECT MAX(amazon_txn_id) i FROM amazon_transactions").fetchone()["i"]


def _match(c, az_id, txn_id):
    c.execute("INSERT INTO amazon_matches (amazon_txn_id, txn_id, confidence, "
              "matched_at) VALUES (?,?,'exact','x')", (az_id, txn_id))


# ── the double-count ─────────────────────────────────────────────────────────
def test_a_split_shipment_order_counts_its_items_once(conn):
    """One order shipping in two boxes matches two charges. Joining items
    through every match counted each product once PER SHIPMENT — every total
    on the page overstated, silently."""
    _order(conn, "SPLIT", "2026-07-01",
           [("A1", "Dog Bed", 4000, 1, "S"), ("A2", "Bird Feeder", 2000, 1, "S")])
    _charge(conn, 1, "2026-07-02", -4000)
    _charge(conn, 2, "2026-07-05", -2000)
    _match(conn, _az_txn(conn, "2026-07-02", -4000, "SPLIT"), 1)
    _match(conn, _az_txn(conn, "2026-07-05", -2000, "SPLIT"), 2)

    d = report.gather(conn)
    assert d["items"] == 2, "two products, however many shipments they arrived in"
    assert d["orders"] == 1
    assert d["line_total"] == 6000


# ── the sign ─────────────────────────────────────────────────────────────────
def test_every_figure_renders_positive(conn):
    """Item prices are positive magnitudes — what a thing cost. Negating them
    printed the entire report as if it were refunds."""
    _order(conn, "O1", "2026-07-01", [("A", "Widget", 2599, 1, "Acme")])
    _charge(conn, 1, "2026-07-02", -2599)
    _match(conn, _az_txn(conn, "2026-07-02", -2599, "O1"), 1)

    html = report.build_html(report.gather(conn), report.brand.load_theme())
    assert "$25.99" in html
    assert "-$25.99" not in html, "a cost is not a refund"


# ── scoped coverage ──────────────────────────────────────────────────────────
def test_a_scoped_report_reports_the_scoped_window_not_all_time(conn):
    """The all-time figure on a two-month page describes a different document
    than the one the reader is holding."""
    _order(conn, "OLD", "2024-01-05", [("A", "Old Thing", 1000, 1, "S")])
    _charge(conn, 1, "2024-01-06", -1000)
    _match(conn, _az_txn(conn, "2024-01-06", -1000, "OLD"), 1)
    _order(conn, "NEW", "2026-07-01", [("B", "New Thing", 5000, 1, "S")])
    _charge(conn, 2, "2026-07-02", -5000)
    _match(conn, _az_txn(conn, "2026-07-02", -5000, "NEW"), 2)
    _charge(conn, 3, "2026-07-03", -2500)          # unmatched, in window

    d = report.gather(conn, since="2026-06-01")
    assert d["is_scoped"] is True
    assert d["scoped_charges"] == 2, "only the in-window charges"
    assert d["scoped_total_cents"] == 7500
    assert d["scoped_pct"] == pytest.approx(66.7, abs=0.1)

    html = report.build_html(d, report.brand.load_theme())
    assert "2 Amazon charges in this period" in html
    # the horizon is a whole-dataset property; on a scoped page it misleads
    assert "reaches back to" not in html


def test_an_all_history_report_does_show_the_horizon(conn):
    _order(conn, "O", "2026-07-01", [("A", "Thing", 1000, 1, "S")])
    _charge(conn, 1, "2026-07-02", -1000)
    _match(conn, _az_txn(conn, "2026-07-02", -1000, "O"), 1)
    _charge(conn, 2, "2024-01-01", -9999)          # older than any az txn
    d = report.gather(conn)
    html = report.build_html(d, report.brand.load_theme())
    assert d["is_scoped"] is False
    assert "reaches back to" in html and "predate any transaction record" in html


# ── classification ───────────────────────────────────────────────────────────
# The keyword table itself is tested in tests/test_kinds.py — it is shared by
# three reports now, so its cases do not belong to any one of them. What this
# file still owns is that the Amazon report reaches it at all.
def test_the_report_reexports_the_shared_classifier():
    from local_budget.connectors import kinds
    assert report.classify is kinds.classify
    assert report.KINDS is kinds.KINDS


# ── presentation details that read as corruption when wrong ──────────────────
def test_titles_truncate_on_a_word_boundary():
    long = "BIRDROCK HOME Adjustable Memory Foam Floor Chair Ideal for Reading"
    out = report._clip(long, 40)
    assert out.endswith("…") and len(out) <= 41
    assert not out.rstrip("…").endswith(" ")
    assert " ".join(out.rstrip("…").split()) in long, "no mid-word cut"


def test_short_titles_are_left_alone():
    assert report._clip("Dog Bed", 40) == "Dog Bed"
    assert report._clip(None, 40) == "—"


def test_empty_section_says_so_instead_of_an_empty_table():
    out = report._table(["A"], [], set(), empty="nothing repeated")
    assert "nothing repeated" in out and "<table" not in out


# ── the output path ──────────────────────────────────────────────────────────
def test_filename_carries_the_scope_so_a_scoped_run_cannot_clobber_all_history():
    assert report._safe("amazon-2026-06-01_2026-07-20") == "amazon-2026-06-01_2026-07-20"


@pytest.mark.parametrize("evil", [
    "../../etc/passwd", "a/b", "..\\..\\win", "; rm -rf /", "a b|c",
])
def test_filename_stem_cannot_escape_the_reports_directory(evil):
    """`since`/`until` reach the filename from the command line."""
    safe = report._safe(evil)
    assert "/" not in safe and "\\" not in safe and ".." not in safe
    assert all(c.isalnum() or c in "-_" for c in safe)


def test_empty_range_refuses_rather_than_rendering_a_blank_report(conn):
    with pytest.raises(ValueError, match="no reconciled Amazon items"):
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
    root = report.TEMPLATE_CSS.parents[5]        # assets->amazon->connectors->local_budget->src->repo
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
    assert "#" not in css.split("*/")[-1] or "var(--" in css
    for literal in ("#F5F0E6", "#181510", "#E8501F", "#6E675C"):
        assert literal not in css, f"{literal} belongs in brand.py, not the template"


def test_the_rendered_page_actually_includes_the_template(conn):
    _order(conn, "O", "2026-07-01", [("A", "Thing", 1000, 1, "S")])
    _charge(conn, 1, "2026-07-02", -1000)
    _match(conn, _az_txn(conn, "2026-07-02", -1000, "O"), 1)
    html = report.build_html(report.gather(conn), report.brand.load_theme())
    # a rule that exists ONLY in the template, not in the shared stylesheet
    assert "break-after: avoid" in html
