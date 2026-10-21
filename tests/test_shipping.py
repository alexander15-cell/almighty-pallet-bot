"""Per-item shipping from size + weight (shipping.py) and local pickup."""
import asyncio

import pytest

import config
import shipping
import stripe_shop
from tests.test_shop_catalog import H, _checkout, _live, _run, _setup, _sku
from tests.test_stripe_shop import _listed, shop  # noqa: F401 (fixture)


@pytest.fixture
def rates(monkeypatch):
    for key, value in {"SHIPPING_MODE": "custom", "SHIP_FIRST_LB": 6.0, "SHIP_PER_EXTRA_LB": 1.0,
                       "SHIP_HANDLING": 1.0, "SHIP_MIN": 5.99, "SHIP_MAX": 0.0, "SHIP_DEFAULT": 14.99,
                       "SHIP_DIM_DIVISOR": 166.0, "SHIP_LOCAL_PICKUP": True}.items():
        monkeypatch.setattr(config, key, value)


def test_billable_weight_uses_the_bigger_of_actual_and_size_weight():
    assert shipping.billable_lb(2.2, None) == 3
    assert shipping.billable_lb(2.0, (24, 18, 12)) == 32  # 5184 / 166 = 31.2 -> big light box
    assert shipping.billable_lb(0.3, (4, 4, 4)) == 1


def test_item_price_from_listing_then_estimate_then_default(rates):
    item = {"id": 1, "ai_suggested_weight_lb": 9.5}
    assert shipping.item_shipping(item, {"weight_lb": 3.0, "length_in": 10, "width_in": 8, "height_in": 6}) == \
        {"cents": 900, "billable_lb": 3, "source": "listing"}  # 6 + 2x1 + 1
    assert shipping.item_shipping(item, {})["source"] == "estimate" and shipping.item_shipping(item, {})["cents"] == 1600
    assert shipping.item_shipping({"id": 2}, {}) == {"cents": 1499, "billable_lb": None, "source": "default"}
    assert shipping.item_shipping({"id": 3}, {"weight_lb": 0.2})["cents"] == 700  # 6 + 1, above the 5.99 minimum


def test_order_shipping_is_the_sum_and_pickup_is_free(rates, monkeypatch):
    pairs = [({"id": 1}, {"weight_lb": 1.0}), ({"id": 2}, {"weight_lb": 4.0})]
    assert shipping.order_cents(pairs, "ship") == 700 + 1000
    assert shipping.order_cents(pairs, "pickup") == 0
    monkeypatch.setattr(config, "SHIPPING_MODE", "flat")
    monkeypatch.setattr(config, "STRIPE_SHIPPING_PRICE", 0.0)
    assert shipping.order_cents(pairs, "ship") == 0 and shipping.per_item_cents({"id": 1}, {}) is None


def test_website_checkout_charges_custom_shipping_or_pickup(shop, monkeypatch, rates):
    a, b = _listed(shop.db, price=20.0, number_of_items=2)
    shop.db.save_ebay_listing_data(a["id"], "Lamp", "1", "1500", 20.0, {}, weight_lb=3.0, length_in=10,
                                   width_in=8, height_in=6)
    _setup(shop, monkeypatch, [_live(a, 20.0, "1"), _live(b, 20.0, "2")])

    async def steps(c):
        await c.post("/shop/catalog/refresh", json={"items": [_sku(a)]}, headers=H)
        cat = {i["sku"]: i for i in (await (await c.get("/shop/catalog", headers=H)).json())["items"]}
        assert cat[_sku(a)]["shipping_cents"] == 900 and cat[_sku(b)]["shipping_cents"] == 1499
        r = await _checkout(c, {_sku(a): 2000})
        data = await r.json()
        assert r.status == 200 and data["shipping_cents"] == 900 and data["delivery"] == "ship"
        assert data["subtotal_cents"] == 2000  # items only; shipping is its own line
        r = await c.post("/shop/checkout", json={"items": [_sku(b)], "checkout_contract": "website-prices-v1",
                                                 "website_prices": {_sku(b): 2000}, "delivery": "pickup"},
                         headers=H)
        data = await r.json()
        assert r.status == 200 and data["shipping_cents"] == 0 and data["delivery"] == "pickup"
        r = await c.post("/shop/checkout", json={"items": [_sku(b)], "checkout_contract": "website-prices-v1",
                                                 "website_prices": {_sku(b): 2000}, "delivery": "drone"}, headers=H)
        assert r.status == 400
    _run(shop, steps)
    ship_params, pickup_params = shop.created
    rate = ship_params["shipping_options"][0]["shipping_rate_data"]
    assert rate["fixed_amount"]["amount"] == 900 and rate["display_name"] == "Standard shipping"
    assert rate["tax_code"] == "txcd_92010001" and rate["tax_behavior"] == "exclusive"
    assert "shipping_options" not in pickup_params and pickup_params["billing_address_collection"] == "required"
