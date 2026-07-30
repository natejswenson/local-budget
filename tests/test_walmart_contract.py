"""The contract with Walmart's payloads — real parser, real shapes, no network.

`test_walmart_connector.py` tests our logic against dicts we wrote. This file
tests our *assumptions*, by running the actual parser over payloads whose shape
was transcribed from live captures (see fixtures/walmart/PROVENANCE.md) and
pushing the results through `store` and `match`.

The distinction is not academic. Two assumptions in this connector were wrong
and no amount of testing against our own fakes could have shown it:

* `priceInfo.linePrice` is a LINE total, not a unit price. Two of a thing is one
  line reading $14.50. The schema called it `unit_price_cents` and every reader
  multiplied by quantity.
* Only the LIST page carries `isInStore`. The detail page has no channel field,
  so a detail fetch was about to blank the channel the matcher filters on.

Both are pinned below. This tier still cannot tell us the scraper works against
Walmart today; only a live `budget walmart sync` does that.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from local_budget import db
from local_budget.connectors.walmart import match, parse, store

FIXTURES = Path(__file__).parent / "fixtures" / "walmart"


def _json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def list_payload():
    return parse.purchase_history(_json("orders-list.json"))


@pytest.fixture()
def detail_payload():
    return parse.order_detail_payload(_json("order-detail.json"))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_BUDGET_DATA_DIR", str(tmp_path))
    dbp = tmp_path / "budget.db"
    db.init_schema(dbp)
    with db.connect(dbp) as c:
        c.execute("INSERT INTO accounts (account_id, institution, acct_type) "
                  "VALUES (1,'T','csv')")
        yield c


# ── locating the payload ─────────────────────────────────────────────────────
def test_next_data_is_extracted_from_a_real_page_shape():
    html = (FIXTURES / "orders-list.html").read_text(encoding="utf-8")
    assert parse.purchase_history(parse.next_data(html)) is not None


def test_a_page_without_next_data_raises_rather_than_returning_empty():
    """A silent zero here is exactly what SyncAborted exists to catch; raising a
    layer earlier says WHICH page changed."""
    with pytest.raises(parse.WalmartParseError, match="no __NEXT_DATA__"):
        parse.next_data("<html><body>challenge</body></html>")


def test_the_two_pages_keep_their_payloads_in_different_places():
    """List and detail are both Next.js and both wrong about each other."""
    assert parse.purchase_history(_json("order-detail.json")) is None
    assert parse.order_detail_payload(_json("orders-list.json")) is None


# ── the list page ────────────────────────────────────────────────────────────
def test_list_orders_parse(list_payload):
    orders = parse.orders_from_list_payload(list_payload)
    assert [o["order_number"] for o in orders] == ["200099900000001", "200099900000002"]
    o = orders[0]
    assert o["order_placed_date"] == "2026-05-04"
    assert o["grand_total"] == "$49.74"
    assert o["item_count"] == 3
    assert o["detail_fetched"] is False


def test_the_list_page_is_where_the_channel_comes_from(list_payload):
    online, in_store = parse.orders_from_list_payload(list_payload)
    assert online["channel"] == "online"
    assert in_store["channel"] == "in-store"


def test_list_items_have_names_but_no_prices(list_payload):
    """The asymmetry the whole two-pass backfill is built around."""
    items = parse.orders_from_list_payload(list_payload)[0]["items"]
    assert [i["title"] for i in items][:1] == ["Widget Brand Blue Widgets, 4 Pack"]
    assert all(i["line_price"] is None for i in items)


def test_the_seller_is_pushed_down_from_the_group_onto_each_line(list_payload):
    items = parse.orders_from_list_payload(list_payload)[0]["items"]
    assert {i["seller"] for i in items} == {"Walmart.com", "EXAMPLE MARKET SELLER"}


def test_the_pagination_cursor_is_read(list_payload):
    assert parse.next_cursor(list_payload) == "n1700000000"


# ── the detail page ──────────────────────────────────────────────────────────
def test_detail_order_parses_with_prices(detail_payload):
    o = parse.order_from_detail(detail_payload)
    assert o["order_number"] == "200099900000001"
    assert o["grand_total"] == "$49.74"
    assert o["tax"] == "$3.80"
    assert o["detail_fetched"] is True
    assert [i["line_price"] for i in o["items"]] == ["$14.50", "$8.44", "$23.00"]


def test_line_price_is_a_line_total_not_a_unit_price(detail_payload):
    """THE correction. A quantity-2 line reads $14.50 for the pair. Storing that
    as a unit price and multiplying doubles it."""
    first = parse.order_from_detail(detail_payload)["items"][0]
    assert (first["quantity"], first["line_price"]) == (2, "$14.50")


def test_the_detail_page_carries_no_channel(detail_payload):
    """Which is why store.py coalesces it. Returning 'online' by default here
    would silently reclassify every in-store purchase on its detail fetch."""
    assert parse.order_from_detail(detail_payload)["channel"] is None


def test_the_versioned_groups_key_is_found_by_prefix(detail_payload):
    """It is `groups_2101` today. A bumped suffix must not silently yield an
    order with no items, which reads as a genuinely empty order."""
    assert len(parse.order_from_detail(detail_payload)["items"]) == 3
    broken = {**detail_payload}
    broken.pop("groups_2101")
    with pytest.raises(parse.WalmartParseError, match="no groups"):
        parse.order_from_detail(broken)


def test_the_product_id_is_walmarts_item_number(detail_payload):
    assert [i["product_id"] for i in parse.order_from_detail(detail_payload)["items"]] \
        == ["100000001", "100000002", "100000003"]


def test_walmart_publishes_no_product_category(detail_payload):
    """A group's `categories` is a fulfilment flag ([{"type": "REGULAR"}]), not a
    shelf taxonomy — so the report's keyword heuristic is load-bearing, not a
    fallback that never fires."""
    assert all(i["category"] is None
               for i in parse.order_from_detail(detail_payload)["items"])


def test_the_payment_label_names_the_card_not_walmart_cash(detail_payload):
    """An order settled in Walmart Cash never reaches the statement; labelling it
    that way would suggest a matching bank charge should exist."""
    assert parse.order_from_detail(detail_payload)["payment_method"] == \
        "VISA Ending in 0000"


# ── all the way through store and match ──────────────────────────────────────
def test_real_shapes_survive_the_whole_pipeline(conn, list_payload, detail_payload):
    """List first, then detail — the order backfill actually runs them in."""
    run = store.start_run(conn, "contract")
    store.store_orders(conn, parse.orders_from_list_payload(list_payload), run)
    store.store_orders(conn, [parse.order_from_detail(detail_payload)], run)

    o = conn.execute("SELECT * FROM walmart_orders WHERE order_number='200099900000001'").fetchone()
    assert o["grand_total_cents"] == 4974
    assert o["tax_cents"] == 380
    assert o["detail_fetched"] == 1
    assert o["channel"] == "online", "the detail pass must not blank the channel"

    items = conn.execute(
        "SELECT * FROM walmart_items WHERE order_number='200099900000001' "
        "ORDER BY line_no").fetchall()
    assert [i["line_price_cents"] for i in items] == [1450, 844, 2300]
    assert sum(i["line_price_cents"] for i in items) == 4594, "== the subtotal"


def test_a_parsed_order_matches_a_real_split_settlement(conn, list_payload,
                                                        detail_payload):
    """End to end: two bank rows summing to the parsed total."""
    for i, cents in enumerate([-3000, -1974], start=1):
        conn.execute(
            "INSERT INTO transactions (txn_id, account_id, fitid, posted_date, "
            " amount_cents, status, merchant_norm, imported_at) "
            "VALUES (?,1,?, '2026-05-05', ?, 'posted','WALMART.COM','x')",
            (i, f"f{i}", cents))
    run = store.start_run(conn, "contract")
    store.store_orders(conn, parse.orders_from_list_payload(list_payload), run)
    store.store_orders(conn, [parse.order_from_detail(detail_payload)], run)
    r = match.run(conn)
    assert r["split"] == 1
    assert match.coverage(conn)["matched_cents"] == 4974
