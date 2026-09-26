"""
get_pallet_financials' pending_sale_value/items_pending_sale - the sum of
listing prices (ebay_listing_data.price, captured for every approved item
regardless of eventual platform) across every item that's been priced but
not yet sold: awaiting_listing, pending_ebay_upload,
pending_fb_marketplace_upload, and listed - shown on the pinned card and
/finance pallet-summary as "Pending Sale Value". Deliberately broad (an
item counts the moment Queue Review approves it, not just once it's
actually live somewhere) so "what's the value of everything I've priced so
far" has one answer regardless of exactly which pre-sale stage it's
sitting in. Deliberately distinct from revenue_so_far (realized, from
actual recorded sale_price).
"""


def _priced_item(db, pallet_id, price, status, item_number_note="widget"):
    item_id = db.create_item(pallet_id, item_number_note, [], 1)
    db.save_ebay_listing_data(
        item_id, ebay_title="Widget", category_id="123", condition_id="1500",
        price=price, item_specifics={}, weight_lb=1.0, length_in=5, width_in=5, height_in=5,
    )
    db.update_status(item_id, status, actor_id=1)
    return item_id


def test_pending_sale_value_sums_listed_items(fresh_db):
    pallet_id = fresh_db.create_pallet("Pending Pallet", category_id=1, created_by=1)
    _priced_item(fresh_db, pallet_id, 25.0, fresh_db.STATUS_LISTED)
    _priced_item(fresh_db, pallet_id, 40.0, fresh_db.STATUS_LISTED)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 2
    assert fin["pending_sale_value"] == 65.0


def test_pending_sale_value_includes_every_pre_sale_stage(fresh_db):
    # The scenario that prompted this: items just approved out of Queue
    # Review into Awaiting Listing, not yet added to any batch or listed
    # anywhere - still expected to count.
    pallet_id = fresh_db.create_pallet("Pending Pallet 5", category_id=6, created_by=1)
    _priced_item(fresh_db, pallet_id, 10.0, fresh_db.STATUS_AWAITING_LISTING)
    _priced_item(fresh_db, pallet_id, 20.0, fresh_db.STATUS_PENDING_EBAY_UPLOAD)
    _priced_item(fresh_db, pallet_id, 30.0, fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD)
    _priced_item(fresh_db, pallet_id, 40.0, fresh_db.STATUS_LISTED)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 4
    assert fin["pending_sale_value"] == 100.0


def test_pending_sale_value_includes_items_on_hold(fresh_db):
    # A hold is a pause, not a change in whether an item is still genuinely
    # priced and pending a sale - shouldn't disappear from this figure just
    # because something's temporarily blocking it.
    pallet_id = fresh_db.create_pallet("Pending Pallet 7", category_id=8, created_by=1)
    _priced_item(fresh_db, pallet_id, 15.0, fresh_db.STATUS_ON_HOLD)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 1
    assert fin["pending_sale_value"] == 15.0


def test_pending_sale_value_excludes_sold_items(fresh_db):
    pallet_id = fresh_db.create_pallet("Pending Pallet 2", category_id=2, created_by=1)
    item_id = _priced_item(fresh_db, pallet_id, 30.0, fresh_db.STATUS_LISTED)
    fresh_db.update_status(item_id, fresh_db.STATUS_SOLD, actor_id=1)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 0
    assert fin["pending_sale_value"] == 0.0


def test_pending_sale_value_excludes_items_without_listing_data(fresh_db):
    pallet_id = fresh_db.create_pallet("Pending Pallet 3", category_id=3, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "no listing data", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_AWAITING_LISTING, actor_id=1)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 0
    assert fin["pending_sale_value"] == 0.0


def test_pending_sale_value_zero_for_pallet_with_no_items(fresh_db):
    pallet_id = fresh_db.create_pallet("Empty Pallet", category_id=4, created_by=1)
    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 0
    assert fin["pending_sale_value"] == 0.0


def test_pending_sale_value_is_separate_from_revenue(fresh_db):
    pallet_id = fresh_db.create_pallet("Pending Pallet 4", category_id=5, created_by=1)
    _priced_item(fresh_db, pallet_id, 50.0, fresh_db.STATUS_LISTED)
    sold_item = fresh_db.create_item(pallet_id, "sold widget", [], 1)
    fresh_db.record_item_sale(sold_item, 20.0, "eBay", actor_id=1)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["pending_sale_value"] == 50.0
    assert fin["revenue_so_far"] == 20.0


def test_pending_sale_value_excludes_rejected_and_deleted(fresh_db):
    pallet_id = fresh_db.create_pallet("Pending Pallet 6", category_id=7, created_by=1)
    _priced_item(fresh_db, pallet_id, 15.0, fresh_db.STATUS_REJECTED)
    _priced_item(fresh_db, pallet_id, 25.0, fresh_db.STATUS_DELETED)

    fin = fresh_db.get_pallet_financials(pallet_id)
    assert fin["items_pending_sale"] == 0
    assert fin["pending_sale_value"] == 0.0
