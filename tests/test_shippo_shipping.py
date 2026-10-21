"""Live shipping quotes via Shippo (SHIPPING_MODE=shippo)."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

import config
import shipping
import shippo_api
import stripe_api
import stripe_shop
from tests.test_shop_catalog import H, _live, _run, _setup, _sku
from tests.test_stripe_shop import _bot, _listed, _paid_session, shop  # noqa: F401 (fixture)


@pytest.fixture
def shippo(monkeypatch):
    for key, value in {"SHIPPING_MODE": "shippo", "SHIPPO_API_TOKEN": "shippo_test_x", "SHIPPO_HANDLING": 1.0,
                       "SHIPPO_MARKUP_PERCENT": 10.0, "SHIPPO_QUOTE_MINUTES": 60, "SHIP_FROM_STREET1": "1 Dock Rd",
                       "SHIP_FROM_CITY": "Muncie", "SHIP_FROM_STATE": "IN", "SHIP_FROM_ZIP": "47305",
                       "SHIP_DEFAULT_PARCEL": "14x12x10x8", "SHIP_LOCAL_PICKUP": True}.items():
        monkeypatch.setattr(config, key, value)
    calls = []

    async def rates(parcel, zip_code):
        calls.append((parcel, zip_code))
        base = 8.0 + parcel["weight"]  # heavier box, pricier
        return [{"amount": base, "provider": "USPS", "service": "Ground Advantage", "days": 3, "id": "r1"},
                {"amount": base + 5, "provider": "UPS", "service": "Ground", "days": 2, "id": "r2"}]
    monkeypatch.setattr(shippo_api, "get_rates", rates)
    return calls


def _quote(c, skus, zip_code="90210"):
    return c.post("/shop/shipping/quote", json={"items": skus, "zip": zip_code}, headers=H)


def _checkout(c, prices, quote_id=None, **extra):
    body = {"items": list(prices), "checkout_contract": "website-prices-v1", "website_prices": prices, **extra}
    if quote_id:
        body["shipping_quote_id"] = quote_id
    return c.post("/shop/checkout", json=body, headers=H)


def test_quote_then_checkout_charges_exactly_the_quote(shop, monkeypatch, shippo):
    a, b = _listed(shop.db, price=20.0, number_of_items=2)
    shop.db.save_ebay_listing_data(a["id"], "Lamp", "1", "1500", 20.0, {}, weight_lb=2.0, length_in=10,
                                   width_in=8, height_in=6)
    _setup(shop, monkeypatch, [_live(a, 20.0, "1"), _live(b, 20.0, "2")])

    async def steps(c):
        assert (await _quote(c, [_sku(a)], "abc")).status == 400
        r = await _quote(c, [_sku(a), _sku(b)])
        q = await r.json()
        # a: (8 + 2) * 1.10 + 1 = 12.00 ; b has no weight -> default box 8 lb: (8 + 8) * 1.10 + 1 = 18.60
        assert r.status == 200 and q["zip"] == "90210" and q["shipping_cents"] == 1200 + 1860
        assert [(i["sku"], i["shipping_cents"], i["carrier"], i["parcel"]) for i in q["items"]] == \
            [(_sku(a), 1200, "USPS", "listing"), (_sku(b), 1860, "USPS", "default")]
        r = await _checkout(c, {_sku(a): 2000, _sku(b): 2000})  # no quote
        assert r.status == 409 and (await r.json())["code"] == "shipping_quote_required"
        r = await _checkout(c, {_sku(a): 2000}, q["quote_id"])  # quote is for a different cart
        assert (await r.json())["code"] == "shipping_quote_required"
        r = await _checkout(c, {_sku(a): 2000, _sku(b): 2000}, q["quote_id"])
        data = await r.json()
        assert r.status == 200 and data["shipping_cents"] == 3060 and data["subtotal_cents"] == 4000
    _run(shop, steps)
    assert shippo[0][1] == "90210" and shippo[0][0]["length"] == 10
    rate = shop.created[0]["shipping_options"][0]["shipping_rate_data"]
    assert rate["fixed_amount"]["amount"] == 3060


def test_pickup_needs_no_quote_and_expired_quotes_are_refused(shop, monkeypatch, shippo):
    item = _listed(shop.db, price=20.0)[0]
    _setup(shop, monkeypatch, [_live(item, 20.0)])
    shop.db.save_shipping_quote("shq_old", [item["id"]], "46201", 900, [],
                                (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())

    async def steps(c):
        r = await _checkout(c, {_sku(item): 2000}, "shq_old")
        assert r.status == 409 and "expired" in (await r.json())["error"]
        r = await _checkout(c, {_sku(item): 2000}, delivery="pickup")
        assert r.status == 200 and (await r.json())["shipping_cents"] == 0
    _run(shop, steps)


def test_quote_route_off_unless_shippo_mode_and_needs_the_key(shop, monkeypatch, shippo):
    item = _listed(shop.db)[0]
    _setup(shop, monkeypatch, [])

    async def steps(c):
        r = await c.post("/shop/shipping/quote", json={"items": [_sku(item)], "zip": "46201"})
        assert r.status == 403
        monkeypatch.setattr(config, "SHIPPING_MODE", "flat")
        assert (await _quote(c, [_sku(item)])).status == 404
    _run(shop, steps)
    assert shippo == []


def test_shippo_failure_is_a_503_not_a_wrong_price(shop, monkeypatch, shippo):
    item = _listed(shop.db)[0]
    _setup(shop, monkeypatch, [])
    monkeypatch.setattr(shippo_api, "get_rates", AsyncMock(side_effect=shippo_api.ShippoError("down")))

    async def steps(c):
        r = await _quote(c, [_sku(item)])
        assert r.status == 503 and (await r.json())["code"] == "shipping_unavailable"
    _run(shop, steps)


def test_paid_order_to_a_different_zip_is_flagged(shop, monkeypatch, shippo):
    item = _listed(shop.db, price=20.0)[0]
    _setup(shop, monkeypatch, [])
    q = asyncio.run(shipping.quote([(item, {})], "90210"))
    sid = asyncio.run(stripe_shop.start_checkout([item["id"]], shipping_quote_id=q["quote_id"]))["session_id"]
    shop.db.update_stripe_checkout(sid, status="paid")
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=_paid_session(sid)))
    notices = []
    monkeypatch.setattr(stripe_shop, "_notify", AsyncMock(side_effect=lambda *a, **k: notices.append(a[6])))
    asyncio.run(stripe_shop.fulfill(_bot(shop.db), sid))
    assert any("quoted to ZIP 90210" in n and "47305" in n for n in notices[0])  # _paid_session ships to 47305


def test_rates_are_parsed_filtered_and_sorted(monkeypatch):
    for key, value in {"SHIPPO_API_TOKEN": "shippo_test_x", "SHIP_FROM_STREET1": "1 Dock Rd", "SHIP_FROM_CITY": "Muncie",
                       "SHIP_FROM_STATE": "IN", "SHIP_FROM_ZIP": "47305", "SHIPPO_CARRIERS": ("USPS", "UPS")}.items():
        monkeypatch.setattr(config, key, value)
    sent = {}

    class _Resp:
        status = 201

        async def json(self, content_type=None):
            return {"rates": [
                {"amount": "14.10", "currency": "USD", "provider": "UPS", "servicelevel": {"name": "Ground"}},
                {"amount": "9.35", "currency": "USD", "provider": "USPS", "servicelevel": {"name": "Ground Advantage"},
                 "estimated_days": 4, "object_id": "rate_1"},
                {"amount": "7.00", "currency": "USD", "provider": "FedEx", "servicelevel": {"name": "Home"}}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            sent.update(url=url, json=json, headers=headers)
            return _Resp()
    monkeypatch.setattr(shippo_api.aiohttp, "ClientSession", _Session)
    rates = asyncio.run(shippo_api.get_rates({"length": 10, "width": 8, "height": 6, "weight": 2}, "90210"))
    assert [r["provider"] for r in rates] == ["USPS", "UPS"]  # FedEx filtered out, cheapest first
    assert rates[0] == {"amount": 9.35, "provider": "USPS", "service": "Ground Advantage", "days": 4, "id": "rate_1"}
    assert sent["headers"]["Authorization"] == "ShippoToken shippo_test_x"
    assert sent["json"]["address_to"] == {"zip": "90210", "country": "US"}
    assert sent["json"]["address_from"]["zip"] == "47305" and sent["json"]["parcels"][0]["weight"] == "2.00"
    monkeypatch.setattr(config, "SHIPPO_API_TOKEN", "")
    with pytest.raises(shippo_api.ShippoError, match="SHIPPO_API_TOKEN"):
        asyncio.run(shippo_api.get_rates({"length": 1, "width": 1, "height": 1, "weight": 1}, "90210"))
