"""Paid website orders -> Shippo orders -> label bought -> Shipped (SHIPPO_ORDERS)."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import shippo_api
import shippo_orders
import stripe_api
import stripe_shop
from tests.test_stripe_shop import _bot, _listed, _paid_session, shop  # noqa: F401 (fixture)


@pytest.fixture
def shippo_on(monkeypatch):
    for key, value in {"SHIPPO_ORDERS": True, "SHIPPO_API_TOKEN": "shippo_test_x", "SHIP_FROM_STREET1": "1 Dock Rd",
                       "SHIP_FROM_CITY": "Muncie", "SHIP_FROM_STATE": "IN", "SHIP_FROM_ZIP": "47305",
                       "SHIPPING_MODE": "flat", "STRIPE_SHIPPING_PRICE": 0.0}.items():
        monkeypatch.setattr(config, key, value)
    created = []

    async def create_order(order):
        created.append(order)
        return {"object_id": f"ord_{len(created)}"}
    monkeypatch.setattr(shippo_api, "create_order", create_order)
    return created


class _Flow:
    def __init__(self, db):
        self.db, self.sold, self.shipped = db, [], []

    async def do_mark_sold(self, item_id, platform, actor_id, old_card=None):
        self.sold.append(item_id)
        self.db.record_sale_platform(item_id, platform, actor_id=actor_id)
        self.db.update_status(item_id, self.db.STATUS_SOLD, actor_id=actor_id)

    async def do_mark_shipped(self, item_id, actor_id, old_card=None):
        self.shipped.append(item_id)
        self.db.update_status(item_id, self.db.STATUS_SHIPPED, actor_id=actor_id)


def _paid(shop, monkeypatch, items, ship=True):
    sid = asyncio.run(stripe_shop.start_checkout([i["id"] for i in items],
                                                 delivery="ship" if ship else "pickup"))["session_id"]
    shop.db.update_stripe_checkout(sid, status="paid")
    session = {**_paid_session(sid, ship=ship), "customer_details": {"email": "buyer@example.com"},
               "amount_subtotal": 2500}
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=session))
    return sid


def _bot_with(flow):
    bot = _bot(flow.db)
    bot.get_cog = lambda name: flow if name == "ItemFlow" else None
    return bot


def test_paid_shipped_order_goes_to_shippo_once(shop, monkeypatch, shippo_on):
    item = _listed(shop.db, price=25.0)[0]
    shop.db.save_ebay_listing_data(item["id"], "Brass Lamp", "1", "1500", 25.0, {}, weight_lb=3.0, length_in=10,
                                   width_in=8, height_in=6)
    sid = _paid(shop, monkeypatch, [item])
    flow = _Flow(shop.db)
    asyncio.run(stripe_shop.fulfill(_bot_with(flow), sid))
    asyncio.run(stripe_shop.fulfill(_bot_with(flow), sid))  # re-run: no second Shippo order

    assert len(shippo_on) == 1
    order = shippo_on[0]
    assert order["to_address"]["zip"] == "47305" and order["to_address"]["email"] == "buyer@example.com"
    assert order["order_status"] == "PAID" and order["from_address"]["zip"] == "47305"
    line = order["line_items"][0]
    assert line["sku"] == f"pallet-{item['pallet_id']}-item-{item['item_number']}"
    assert "box 10x8x6 in, 3 lb" in line["title"] and line["weight"] == "3.00"
    assert shop.db.get_shippo_order(sid)["status"] == "open"


def test_pickup_and_turned_off_never_go_to_shippo(shop, monkeypatch, shippo_on):
    a, b = _listed(shop.db, number_of_items=2)
    sid = _paid(shop, monkeypatch, [a], ship=False)
    asyncio.run(stripe_shop.fulfill(_bot_with(_Flow(shop.db)), sid))
    monkeypatch.setattr(config, "SHIPPO_ORDERS", False)
    sid2 = _paid(shop, monkeypatch, [b])
    asyncio.run(stripe_shop.fulfill(_bot_with(_Flow(shop.db)), sid2))
    assert shippo_on == [] and shop.db.get_shippo_order(sid2) is None


def test_failed_send_is_retried(shop, monkeypatch, shippo_on):
    item = _listed(shop.db)[0]
    sid = _paid(shop, monkeypatch, [item])
    monkeypatch.setattr(shippo_api, "create_order", AsyncMock(side_effect=shippo_api.ShippoError("down")))
    flow = _Flow(shop.db)
    asyncio.run(stripe_shop.fulfill(_bot_with(flow), sid))
    assert shop.db.get_shippo_order(sid)["status"] == "failed"

    monkeypatch.setattr(shippo_api, "create_order", AsyncMock(return_value={"object_id": "ord_9"}))
    assert asyncio.run(shippo_orders.retry_failed(_bot_with(flow))) == 1
    row = shop.db.get_shippo_order(sid)
    assert row["status"] == "open" and row["order_id"] == "ord_9" and row["attempts"] == 2


def test_bought_label_marks_shipped_saves_tracking_and_charges_the_pallet(shop, monkeypatch, shippo_on):
    a, b = _listed(shop.db, price=25.0, number_of_items=2)
    sid = _paid(shop, monkeypatch, [a, b])
    flow = _Flow(shop.db)
    asyncio.run(stripe_shop.fulfill(_bot_with(flow), sid))
    for i in (a, b):
        shop.db.record_item_sale(i["id"], 30.0 if i is a else 10.0, "Website", actor_id=1)

    monkeypatch.setattr(shippo_api, "get_order", AsyncMock(return_value={"transactions": []}))
    assert asyncio.run(shippo_orders.sync_labels(_bot_with(flow))) == 0  # no label yet

    monkeypatch.setattr(shippo_api, "get_order", AsyncMock(return_value={"transactions": [{"object_id": "t1"}]}))
    monkeypatch.setattr(shippo_api, "get_transaction", AsyncMock(return_value={
        "status": "SUCCESS", "tracking_number": "9400100000000000000000", "tracking_url_provider": "https://t/1",
        "rate": "rate_1"}))
    monkeypatch.setattr(shippo_api, "get_rate", AsyncMock(return_value={
        "amount": "12.00", "provider": "USPS", "servicelevel": {"name": "Ground Advantage"}}))
    assert asyncio.run(shippo_orders.sync_labels(_bot_with(flow))) == 1

    assert sorted(flow.shipped) == sorted([a["id"], b["id"]])
    row = shop.db.get_shippo_order(sid)
    assert row["status"] == "labelled" and row["tracking"] == "9400100000000000000000" and row["label_cost"] == 12.0
    costs = [c for c in shop.db.get_pallet_costs(a["pallet_id"]) if c["source"] == "shippo"]
    assert sorted(c["amount"] for c in costs) == [3.0, 9.0]  # split 30:10
    assert asyncio.run(shippo_orders.sync_labels(_bot_with(flow))) == 0  # done once only


def test_in_person_sale_goes_to_shippo_once_its_address_is_saved(shop, monkeypatch, shippo_on):
    item = _listed(shop.db, price=40.0)[0]
    shop.db.record_sale_platform(item["id"], "In Person", actor_id=1)
    shop.db.update_status(item["id"], shop.db.STATUS_SOLD, actor_id=1)
    shop.db.record_item_sale(item["id"], 40.0, "In Person", actor_id=1)

    note = asyncio.run(shippo_orders.send_for_item(shop.db.get_item(item["id"])))
    assert "incomplete" in note and shippo_on == []  # no address yet

    shop.db.set_shipping_info(item["id"], "Pat Buyer", "9 Elm St", "", "Muncie", "IN", "47304", "US", actor_id=1)
    note = asyncio.run(shippo_orders.send_for_item(shop.db.get_item(item["id"])))
    assert note.startswith("Sent to Shippo") and len(shippo_on) == 1
    order = shippo_on[0]
    assert order["to_address"] == {"name": "Pat Buyer", "street1": "9 Elm St", "street2": "", "city": "Muncie",
                                   "state": "IN", "zip": "47304", "country": "US"}
    assert order["line_items"][0]["total_price"] == "40.00" and order["order_number"].endswith(
        f"pallet-{item['pallet_id']}-item-{item['item_number']}")
    assert asyncio.run(shippo_orders.send_for_item(shop.db.get_item(item["id"]))) == ""  # only once
    assert shop.db.get_shippo_order(f"item-{item['id']}")["status"] == "open"


def test_ebay_and_unsold_items_never_go_to_shippo(shop, monkeypatch, shippo_on):
    listed, ebay_sold = _listed(shop.db, number_of_items=2)
    shop.db.record_sale_platform(ebay_sold["id"], "eBay", actor_id=1)
    shop.db.update_status(ebay_sold["id"], shop.db.STATUS_SOLD, actor_id=1)
    for item in (listed, ebay_sold):
        shop.db.set_shipping_info(item["id"], "Pat", "9 Elm St", "", "Muncie", "IN", "47304", "US", actor_id=1)
        assert asyncio.run(shippo_orders.send_for_item(shop.db.get_item(item["id"]))) == ""
    assert shippo_on == []
