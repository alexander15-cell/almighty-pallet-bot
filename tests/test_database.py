"""Core database.py behavior - pallets/items, financials (including refunds/
expenses/reversals), structured shipping addresses, buyer-data retention,
and the eBay batch lifecycle. Each test gets its own throwaway DB via the
fresh_db fixture (see conftest.py)."""
from datetime import datetime, timedelta, timezone


def test_init_db_is_safe_to_call_twice(fresh_db):
    fresh_db.init_db()  # should not raise - matches "safe to call on every startup"


def test_create_pallet_and_item(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=111, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "a widget", [], 1)
    item = fresh_db.get_item(item_id)
    assert item["pallet_id"] == pallet_id
    assert item["status"] == fresh_db.STATUS_DATA_ENTRY
    assert item["item_number"] == 1


def test_financials_track_refunds_expenses_and_reversals(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet B", category_id=222, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 100.0, actor_id=1)

    item1 = fresh_db.create_item(pallet_id, "item1", [], 1)
    fresh_db.record_item_sale(item1, 50.0, "eBay", actor_id=1)
    item2 = fresh_db.create_item(pallet_id, "item2", [], 1)
    fresh_db.record_item_sale(item2, 30.0, "Facebook Marketplace", actor_id=1)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["revenue_so_far"] == 80.0
    assert fin["net_revenue"] == 80.0

    fresh_db.record_refund(item1, 10.0, "buyer complaint", actor_id=1)
    fresh_db.record_expense(pallet_id, 5.0, "shipping tape", actor_id=1)
    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["refunds_total"] == 10.0
    assert fin["expenses_total"] == 5.0
    assert fin["net_revenue"] == 65.0

    old_price = fresh_db.reverse_sale(item2, "duplicate entry", actor_id=1)
    assert old_price == 30.0
    assert fresh_db.get_item(item2)["sale_price"] is None
    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["revenue_so_far"] == 50.0
    assert fin["net_revenue"] == 35.0

    # reversing an item with nothing to reverse is a no-op, not an error
    assert fresh_db.reverse_sale(item2, "already reversed", actor_id=1) is None

    history = fresh_db.get_finance_transactions(pallet_id)
    assert [h["type"] for h in history] == ["reversal", "expense", "refund"]


def test_structured_shipping_address(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet C", category_id=333, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.set_shipping_info(
        item_id, "Jane Doe", "123 Main St", "Apt 4", "Springfield", "IL", "62701", "US", actor_id=1,
    )
    item = fresh_db.get_item(item_id)
    assert item["address_line1"] == "123 Main St"
    assert item["city"] == "Springfield"
    assert item["postal_code"] == "62701"


def test_buyer_data_purge_candidates_respects_retention_window(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet D", category_id=444, created_by=1)

    old_item = fresh_db.create_item(pallet_id, "old item", [], 1)
    fresh_db.record_item_sale(old_item, 20.0, "Facebook Marketplace", actor_id=1)
    fresh_db.set_shipping_info(old_item, "Alice", "1 Main St", "", "Springfield", "IL", "62701", "US", actor_id=1)
    fresh_db.update_status(old_item, fresh_db.STATUS_SOLD)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET status=?, shipped_at=? WHERE id=?", (fresh_db.STATUS_SHIPPED, old_ts, old_item))

    recent_item = fresh_db.create_item(pallet_id, "recent item", [], 1)
    fresh_db.record_item_sale(recent_item, 15.0, "Facebook Marketplace", actor_id=1)
    fresh_db.set_shipping_info(recent_item, "Bob", "2 Oak St", "", "Metropolis", "NY", "10001", "US", actor_id=1)
    fresh_db.update_status(recent_item, fresh_db.STATUS_SOLD)
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET status=?, shipped_at=? WHERE id=?", (fresh_db.STATUS_SHIPPED, recent_ts, recent_item))

    candidates = fresh_db.get_buyer_data_purge_candidates(90)
    assert [c["id"] for c in candidates] == [old_item]

    fresh_db.purge_buyer_data([old_item], actor_id=1)
    after = fresh_db.get_item(old_item)
    assert after["recipient_name"] is None
    assert after["address_line1"] is None
    assert fresh_db.get_item(recent_item)["recipient_name"] == "Bob"  # untouched
    assert fresh_db.get_buyer_data_purge_candidates(90) == []


def test_ebay_batch_lifecycle(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet E", category_id=555, created_by=1)
    item_ids = []
    for i in range(3):
        item_id = fresh_db.create_item(pallet_id, f"item{i}", [], 1)
        fresh_db.save_ebay_listing_data(
            item_id, f"Title {i}", "12345", "1500", 19.99, {"Brand": "Test"}, actor_id=1,
        )
        fresh_db.update_status(item_id, fresh_db.STATUS_PENDING_EBAY_UPLOAD, actor_id=1)
        item_ids.append(item_id)

    unbatched = fresh_db.get_unbatched_pending_items()
    assert {i["id"] for i in unbatched} == set(item_ids)

    batch_id = fresh_db.create_ebay_batch(csv_filename="test.csv", exported_by=1, item_ids=item_ids)
    assert fresh_db.get_unbatched_pending_items() == []

    batch_items = fresh_db.get_ebay_batch_items(batch_id)
    assert {i["id"] for i in batch_items} == set(item_ids)

    b = fresh_db.get_ebay_batch(batch_id)
    assert b["item_count"] == 3

    fresh_db.set_ebay_item_id(item_ids[0], "110099998888")
    fresh_db.update_status(item_ids[0], fresh_db.STATUS_LISTED, actor_id=1)
    still_pending = fresh_db.get_ebay_batch_items(batch_id)
    assert len(still_pending) == 2

    listing = fresh_db.get_ebay_listing_data(item_ids[0])
    assert listing["ebay_item_id"] == "110099998888"


def test_clear_ebay_batch_id_requeues_item_for_a_fresh_export(fresh_db):
    # Used by /ebay retry-item: an item whose upload actually failed (e.g.
    # missing a config-driven required field) stays pending_ebay_upload but
    # is still stamped with the failed batch's id - clearing it must make
    # the item eligible for the *next* export again.
    pallet_id = fresh_db.create_pallet("Pallet F", category_id=666, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.save_ebay_listing_data(item_id, "Title", "12345", "1500", 19.99, {}, actor_id=1)
    fresh_db.update_status(item_id, fresh_db.STATUS_PENDING_EBAY_UPLOAD, actor_id=1)

    batch_id = fresh_db.create_ebay_batch(csv_filename="failed_batch.csv", exported_by=1, item_ids=[item_id])
    assert fresh_db.get_unbatched_pending_items() == []  # now tied to the failed batch

    fresh_db.clear_ebay_batch_id(item_id)
    assert [i["id"] for i in fresh_db.get_unbatched_pending_items()] == [item_id]
    # The old batch record itself is untouched - just the item's link to it.
    assert fresh_db.get_ebay_batch(batch_id)["item_count"] == 1


def test_retry_item_can_correct_the_category_before_requeueing(fresh_db):
    # /ebay retry-item accepts an optional corrected category_id - confirmed
    # against a real eBay error 87 ("category selected is not a leaf
    # category") where simply re-submitting the same bad category would
    # just fail again identically.
    pallet_id = fresh_db.create_pallet("Pallet G", category_id=777, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.save_ebay_listing_data(item_id, "Title", "259482", "1500", 19.99, {"Brand": "Test"}, actor_id=1)

    listing = fresh_db.get_ebay_listing_data(item_id)
    listing["category_id"] = "12345"  # the corrected leaf category
    fresh_db.save_ebay_listing_data(
        item_id, listing["ebay_title"], listing["category_id"], listing["condition_id"],
        listing["price"], listing["item_specifics"], listing_format=listing["listing_format"],
        auction_duration=listing["auction_duration"], actor_id=1,
    )

    updated = fresh_db.get_ebay_listing_data(item_id)
    assert updated["category_id"] == "12345"
    assert updated["ebay_title"] == "Title"  # everything else preserved
    assert updated["item_specifics"] == {"Brand": "Test"}


def test_ebay_listing_weight_dims_are_wiped_if_omitted_on_upsert(fresh_db):
    # save_ebay_listing_data is a full-column upsert, not a partial merge -
    # documents the exact hazard /ebay retry-item's weight/dims preservation
    # fix guards against: omitting weight/dims on a later save silently
    # nulls out a previously-saved value instead of leaving it unchanged.
    pallet_id = fresh_db.create_pallet("Pallet H", category_id=888, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, "Title", "12345", "1500", 19.99, {}, weight_lb=2.5,
        length_in=12, width_in=8, height_in=4, actor_id=1,
    )
    fresh_db.save_ebay_listing_data(item_id, "Title", "99999", "1500", 19.99, {}, actor_id=1)

    updated = fresh_db.get_ebay_listing_data(item_id)
    assert updated["category_id"] == "99999"
    assert updated["weight_lb"] is None  # the hazard, if not guarded against


def test_retry_item_style_correction_preserves_previously_saved_weight(fresh_db):
    # The actual fix in /ebay retry-item (cogs/ebay.py): always thread
    # listing.get("weight_lb")/etc. through on every corrections-triggered
    # save, so a category-only (or condition/specifics-only) retry doesn't
    # wipe a previously-saved weight/dimensions. This mirrors retry_item's
    # real save_ebay_listing_data call exactly.
    pallet_id = fresh_db.create_pallet("Pallet I", category_id=999, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, "Title", "259482", "1500", 19.99, {}, weight_lb=2.5,
        length_in=12, width_in=8, height_in=4, actor_id=1,
    )

    listing = fresh_db.get_ebay_listing_data(item_id)
    listing["category_id"] = "12345"  # the only field this retry corrects
    fresh_db.save_ebay_listing_data(
        item_id, listing["ebay_title"], listing["category_id"], listing["condition_id"],
        listing["price"], listing["item_specifics"], listing_format=listing["listing_format"],
        auction_duration=listing["auction_duration"], weight_lb=listing.get("weight_lb"),
        length_in=listing.get("length_in"), width_in=listing.get("width_in"),
        height_in=listing.get("height_in"), actor_id=1,
    )

    updated = fresh_db.get_ebay_listing_data(item_id)
    assert updated["category_id"] == "12345"
    assert updated["weight_lb"] == 2.5
    assert updated["length_in"] == 12
    assert updated["width_in"] == 8
    assert updated["height_in"] == 4


def test_pirate_ship_weight_prefers_confirmed_ebay_listing_data(fresh_db):
    # An item that went through Queue Review's EbayListingModal (human-
    # confirmed weight) but ended up sold on another platform should use
    # that confirmed value, not the AI's rougher automated-review guess.
    pallet_id = fresh_db.create_pallet("Pallet J", category_id=1001, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.save_ai_review(item_id, "AI Title", "AI desc", "[]", suggested_weight_lb=9.9,
                             suggested_length_in=9, suggested_width_in=9, suggested_height_in=9)
    fresh_db.save_ebay_listing_data(
        item_id, "Title", "12345", "1500", 19.99, {}, weight_lb=2.5,
        length_in=12, width_in=8, height_in=4, actor_id=1,
    )
    fresh_db.record_item_sale(item_id, 25.0, "Facebook Marketplace", actor_id=1)
    fresh_db.update_status(item_id, fresh_db.STATUS_SOLD)

    rows = fresh_db.get_unexported_other_platform_sales()
    row = next(r for r in rows if r["id"] == item_id)
    assert row["pirate_ship_weight_lb"] == 2.5
    assert row["pirate_ship_length_in"] == 12
    assert row["pirate_ship_width_in"] == 8
    assert row["pirate_ship_height_in"] == 4


def test_pirate_ship_weight_falls_back_to_ai_suggestion(fresh_db):
    # Most other-platform sales never go through the eBay approval flow at
    # all, so ebay_listing_data has no row for them - the AI's automated-
    # review estimate is the only weight/dims data available.
    pallet_id = fresh_db.create_pallet("Pallet K", category_id=1002, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.save_ai_review(item_id, "AI Title", "AI desc", "[]", suggested_weight_lb=4.0,
                             suggested_length_in=10, suggested_width_in=6, suggested_height_in=3)
    fresh_db.record_item_sale(item_id, 25.0, "Facebook Marketplace", actor_id=1)
    fresh_db.update_status(item_id, fresh_db.STATUS_SOLD)

    rows = fresh_db.get_unexported_other_platform_sales()
    row = next(r for r in rows if r["id"] == item_id)
    assert row["pirate_ship_weight_lb"] == 4.0
    assert row["pirate_ship_length_in"] == 10
    assert row["pirate_ship_width_in"] == 6
    assert row["pirate_ship_height_in"] == 3


def test_pirate_ship_weight_null_when_never_captured(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet L", category_id=1003, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.record_item_sale(item_id, 25.0, "Facebook Marketplace", actor_id=1)
    fresh_db.update_status(item_id, fresh_db.STATUS_SOLD)

    rows = fresh_db.get_unexported_other_platform_sales()
    row = next(r for r in rows if r["id"] == item_id)
    assert row["pirate_ship_weight_lb"] is None
