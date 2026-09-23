"""
The reporting layer added for /finance pallet-summary and /finance overview:
get_pallet_by_name, get_pallet_manifest_value_estimate,
get_pallet_progress_counts, get_month_to_date_financials, and
sale_recorded_at (the timestamp record_item_sale/reverse_sale maintain
specifically for "when was this sale recorded", distinct from
items.updated_at/sold_at - see its migration comment in database.py).
"""
from datetime import datetime, timedelta, timezone


def test_get_pallet_by_name_is_case_insensitive(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet Zebra", category_id=1, created_by=1)
    found = fresh_db.get_pallet_by_name("pallet zebra")
    assert found["id"] == pallet_id


def test_get_pallet_by_name_returns_none_when_missing(fresh_db):
    assert fresh_db.get_pallet_by_name("Nonexistent") is None


def test_sale_recorded_at_set_on_record_and_cleared_on_reverse(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)

    assert fresh_db.get_item(item_id)["sale_recorded_at"] is None
    fresh_db.record_item_sale(item_id, 20.0, "eBay", actor_id=1)
    assert fresh_db.get_item(item_id)["sale_recorded_at"] is not None

    fresh_db.reverse_sale(item_id, "fell through", actor_id=1)
    assert fresh_db.get_item(item_id)["sale_recorded_at"] is None


def test_manifest_value_estimate_sums_ai_suggested_prices(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item1 = fresh_db.create_item(pallet_id, "widget 1", [], 1)
    fresh_db.save_ai_review(item1, "Widget", "desc", "[]", suggested_price=10.0)
    item2 = fresh_db.create_item(pallet_id, "widget 2", [], 1)
    fresh_db.save_ai_review(item2, "Widget", "desc", "[]", suggested_price=15.0)
    item3 = fresh_db.create_item(pallet_id, "widget 3 - no AI price yet", [], 1)

    assert fresh_db.get_pallet_manifest_value_estimate(pallet_id) == 25.0
    assert fresh_db.get_pallet_manifest_value_estimate(999999) == 0.0


def test_manifest_value_estimate_excludes_deleted_items(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item1 = fresh_db.create_item(pallet_id, "widget 1", [], 1)
    fresh_db.save_ai_review(item1, "Widget", "desc", "[]", suggested_price=10.0)
    fresh_db.update_status(item1, fresh_db.STATUS_DELETED, actor_id=1)

    assert fresh_db.get_pallet_manifest_value_estimate(pallet_id) == 0.0


def test_pallet_progress_counts_in_progress_vs_sold_out(fresh_db):
    in_progress_pallet = fresh_db.create_pallet("In Progress", category_id=1, created_by=1)
    item = fresh_db.create_item(in_progress_pallet, "widget", [], 1)
    fresh_db.update_status(item, fresh_db.STATUS_LISTED, actor_id=1)

    sold_out_pallet = fresh_db.create_pallet("Sold Out", category_id=2, created_by=1)
    item1 = fresh_db.create_item(sold_out_pallet, "widget 1", [], 1)
    fresh_db.update_status(item1, fresh_db.STATUS_SOLD, actor_id=1)
    item2 = fresh_db.create_item(sold_out_pallet, "widget 2", [], 1)
    fresh_db.update_status(item2, fresh_db.STATUS_SHIPPED, actor_id=1)

    empty_pallet = fresh_db.create_pallet("Empty", category_id=3, created_by=1)

    counts = fresh_db.get_pallet_progress_counts()
    assert counts == {"in_progress": 2, "sold_out": 1}  # in_progress_pallet + empty_pallet


def test_pallet_progress_counts_excludes_archived(fresh_db):
    pallet_id = fresh_db.create_pallet("Archived", category_id=1, created_by=1)
    fresh_db.archive_pallet(pallet_id)

    counts = fresh_db.get_pallet_progress_counts()
    assert counts == {"in_progress": 0, "sold_out": 0}


def test_pallet_progress_counts_ignores_deleted_items_when_checking_sold_out(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    sold_item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(sold_item, fresh_db.STATUS_SOLD, actor_id=1)
    deleted_item = fresh_db.create_item(pallet_id, "junk", [], 1)
    fresh_db.update_status(deleted_item, fresh_db.STATUS_DELETED, actor_id=1)

    counts = fresh_db.get_pallet_progress_counts()
    assert counts == {"in_progress": 0, "sold_out": 1}


def test_month_to_date_financials_counts_only_this_month(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.record_item_sale(item, 50.0, "eBay", actor_id=1)
    fresh_db.add_pallet_cost(pallet_id, "shipping", 5.0, source="pirateship")
    fresh_db.record_expense(pallet_id, 2.0, "tape", actor_id=1)

    mtd = fresh_db.get_month_to_date_financials()
    assert mtd["revenue"] == 50.0
    assert mtd["spend"] == 7.0


def test_month_to_date_financials_excludes_prior_months(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.record_item_sale(item, 50.0, "eBay", actor_id=1)

    old_date = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET sale_recorded_at = ? WHERE id = ?", (old_date, item))

    mtd = fresh_db.get_month_to_date_financials()
    assert mtd["revenue"] == 0.0


def test_month_to_date_financials_excludes_manual_setprice(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 500.0, actor_id=1)

    mtd = fresh_db.get_month_to_date_financials()
    assert mtd["spend"] == 0.0
