"""
database.py's sales/sale_items tables and functions (see their own
CREATE TABLE comments in init_db) - the data layer behind /finance log-sale
(cogs/finance.py). A "sale" can cover a single item or a bundle sold
together, possibly spanning different pallets.
"""
import pytest


def test_create_sale_and_add_sale_item_round_trip(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)

    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 20.0, already_deposited=False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, allocated_price=20.0, purchase_price=5.0, cogs_amount=5.0)

    sale = fresh_db.get_sale(sale_id)
    assert sale["platform"] == "eBay"
    assert sale["total_price"] == 20.0
    assert sale["already_deposited"] == 0
    assert sale["cogs_logged_at"] is None

    items = fresh_db.get_sale_items(sale_id)
    assert len(items) == 1
    assert items[0]["item_id"] == item_id
    assert items[0]["allocated_price"] == 20.0
    assert items[0]["purchase_price"] == 5.0
    assert items[0]["cogs_amount"] == 5.0
    assert items[0]["pallet_name"] == "Pallet A"


def test_get_sale_items_spans_multiple_pallets_in_one_sale(fresh_db):
    p1 = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    p2 = fresh_db.create_pallet("Pallet B", category_id=2, created_by=1)
    item1 = fresh_db.create_item(p1, "Widget", [], 1)
    item2 = fresh_db.create_item(p2, "Gadget", [], 1)

    sale_id = fresh_db.create_sale("In Person", "2026-09-30", 30.0, already_deposited=True, created_by=1)
    fresh_db.add_sale_item(sale_id, item1, 15.0, 4.0, 4.0)
    fresh_db.add_sale_item(sale_id, item2, 15.0, 6.0, 6.0)

    items = fresh_db.get_sale_items(sale_id)
    pallet_names = {i["pallet_name"] for i in items}
    assert pallet_names == {"Pallet A", "Pallet B"}


def test_an_item_can_only_belong_to_one_sale(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 20.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 20.0, 5.0, 5.0)

    other_sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    with pytest.raises(Exception):
        fresh_db.add_sale_item(other_sale_id, item_id, 10.0, 2.0, 2.0)


def test_get_item_sale_reflects_idempotency_state(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    assert fresh_db.get_item_sale(item_id) is None

    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 20.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 20.0, 5.0, 5.0)

    found = fresh_db.get_item_sale(item_id)
    assert found is not None
    assert found["sale_id"] == sale_id


def test_set_sale_receipt_id_marks_a_sale_fully_logged(fresh_db):
    """Cash basis: the Sales Receipt is the only QuickBooks artifact a sale
    produces (no COGS Journal Entry - see cogs/finance.py's
    _post_sale_to_quickbooks) - set_sale_receipt_id alone is what
    /finance retry-sale checks to know a sale is fully logged."""
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 20.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 20.0, 5.0, 5.0)

    fresh_db.set_sale_receipt_id(sale_id, "sr-1")
    sale = fresh_db.get_sale(sale_id)
    assert sale["quickbooks_sales_receipt_id"] == "sr-1"


def test_record_sale_platform_sets_platform_without_touching_price(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)

    fresh_db.record_sale_platform(item_id, "Facebook Marketplace", actor_id=1)

    item = fresh_db.get_item(item_id)
    assert item["sale_platform"] == "Facebook Marketplace"
    assert item["sale_price"] is None


def test_get_pallet_cogs_logged_total_sums_only_this_pallets_items(fresh_db):
    p1 = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    p2 = fresh_db.create_pallet("Pallet B", category_id=2, created_by=1)
    item1 = fresh_db.create_item(p1, "Widget", [], 1)
    item2 = fresh_db.create_item(p2, "Gadget", [], 1)

    assert fresh_db.get_pallet_cogs_logged_total(p1) == 0.0

    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 20.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item1, 20.0, 5.0, 5.0)
    fresh_db.add_sale_item(sale_id, item2, 0.0, 2.0, 2.0)

    assert fresh_db.get_pallet_cogs_logged_total(p1) == 5.0
    assert fresh_db.get_pallet_cogs_logged_total(p2) == 2.0
