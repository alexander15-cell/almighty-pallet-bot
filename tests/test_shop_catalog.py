"""Website-server API: ebay-catalog-v1 catalog + refresh, and the
website-prices-v1 checkout verified against live eBay (webapp/shop.py,
ebay_catalog.py)."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

import config
import ebay_api
import stripe_api
from tests.test_stripe_shop import _bot, _listed, _priced_stripe, shop  # noqa: F401 (fixture)

KEY = "s" * 40


def _sku(item):
    return f"pallet-{item['pallet_id']}-item-{item['item_number']}"


def _live(item, price, listing_id="900000000001", quantity=1, listing_type="FixedPriceItem"):
    return {"listing_id": listing_id, "sku": _sku(item), "title": "Lamp", "price": price, "quantity": quantity,
            "listing_type": listing_type, "category_id": "1", "condition_id": "3000"}


def _setup(shop, monkeypatch, listings):
    from webapp import shop as routes
    monkeypatch.setattr(config, "STRIPE_SHOP_SERVER_KEY", KEY)
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_live_x")
    routes._recent.clear()
    fetch = AsyncMock(return_value=listings)
    monkeypatch.setattr(ebay_api, "get_active_listings", fetch)
    _priced_stripe(monkeypatch, shop)
    return fetch


def _run(shop, steps):
    async def go():
        from webapp.server import create_app
        c = TestClient(TestServer(create_app(_bot(shop.db))))
        await c.start_server()
        try:
            await steps(c)
        finally:
            await c.close()
    asyncio.run(go())


H = {"X-Shop-Key": KEY, "X-Client-IP": "7.7.7.7"}


def _checkout(c, prices, headers=H):
    return c.post("/shop/checkout", json={"items": list(prices), "checkout_contract": "website-prices-v1",
                                          "website_prices": prices}, headers=headers)


def test_every_route_needs_the_key_before_doing_any_work(shop, monkeypatch):
    item = _listed(shop.db)[0]
    fetch = _setup(shop, monkeypatch, [_live(item, 22.0)])

    async def steps(c):
        for headers in ({}, {"X-Shop-Key": "wrong"}, {"X-Shop-Key": KEY[:-1]}):
            assert (await c.get("/shop/catalog", headers=headers)).status == 403
            assert (await c.post("/shop/catalog/refresh", json={"items": [_sku(item)]}, headers=headers)).status == 403
            assert (await _checkout(c, {_sku(item): 2200}, headers=headers)).status == 403
        monkeypatch.setattr(config, "STRIPE_SHOP_SERVER_KEY", "")  # not configured = closed
        assert (await c.get("/shop/catalog", headers={"X-Shop-Key": ""})).status == 403
        assert (await _checkout(c, {_sku(item): 2200})).status == 403
    _run(shop, steps)
    fetch.assert_not_awaited()
    assert shop.created == []


def test_catalog_reports_ebay_truth_and_bot_state(shop, monkeypatch):
    listed, relisted, sold, held, ended, multi, auction = _listed(shop.db, price=15.0, number_of_items=7)
    shop.db.update_status(sold["id"], shop.db.STATUS_SOLD, actor_id=1)
    shop.db.update_status(held["id"], shop.db.STATUS_ON_HOLD, actor_id=1)
    _setup(shop, monkeypatch, [
        _live(listed, 22.0, "860012952535"), _live(relisted, 50.0, "860000000002"), _live(sold, 9.0, "3"),
        _live(held, 9.0, "4"), _live(multi, 36.39, "5", quantity=4), _live(auction, 3.0, "6", listing_type="Chinese"),
        {**_live(listed, 1.0, "7"), "sku": "not-a-label"}])

    async def steps(c):
        assert (await c.get("/shop/catalog", headers=H)).status == 503  # never checked yet
        assert (await c.post("/shop/catalog/refresh", json={"items": [_sku(listed)]}, headers=H)).status == 200
        r = await c.get("/shop/catalog", headers=H)
        assert r.status == 200
        data = await r.json()
        assert data["catalog_contract"] == "ebay-catalog-v1" and data["complete"] is True
        rec = {i["sku"]: i for i in data["items"]}
        assert rec[_sku(listed)] == {"sku": _sku(listed), "ebay_listing_id": "860012952535", "ebay_active": True,
                                     "price_cents": 2200, "currency": "usd", "available_quantity": 1,
                                     "bot_status": "listed", "ebay_checked_at": rec[_sku(listed)]["ebay_checked_at"]}
        # matched by Custom label, not the old ID the bot had on file ("110011")
        assert rec[_sku(relisted)]["ebay_listing_id"] == "860000000002" and rec[_sku(relisted)]["price_cents"] == 5000
        assert rec[_sku(sold)]["available_quantity"] == 0 and rec[_sku(sold)]["bot_status"] == "sold"
        assert rec[_sku(held)]["available_quantity"] == 0 and rec[_sku(held)]["bot_status"] == "on_hold"
        assert rec[_sku(ended)] == {**rec[_sku(ended)], "ebay_active": False, "ebay_listing_id": None,
                                    "price_cents": None, "available_quantity": 0}
        assert rec[_sku(multi)]["available_quantity"] == 4
        assert rec[_sku(auction)]["price_cents"] is None and rec[_sku(auction)]["available_quantity"] == 0
    _run(shop, steps)


def test_catalog_refuses_an_old_ebay_check(shop, monkeypatch):
    item = _listed(shop.db)[0]
    _setup(shop, monkeypatch, [])
    old = (datetime.now(timezone.utc) - timedelta(minutes=config.EBAY_CATALOG_MAX_AGE_MINUTES + 1)).isoformat()
    shop.db.record_active_snapshot([{**_live(item, 22.0), "sku": _sku(item)}], old)

    async def steps(c):
        r = await c.get("/shop/catalog", headers=H)
        assert r.status == 503 and (await r.json())["code"] == "ebay_unavailable"
    _run(shop, steps)


def test_refresh_returns_one_record_per_sku_and_fails_closed(shop, monkeypatch):
    item = _listed(shop.db, price=15.0)[0]
    fetch = _setup(shop, monkeypatch, [_live(item, 22.0)])

    async def steps(c):
        for bad in ([], [_sku(item)] * 2, ["x"], [f"pallet-1-item-{n}" for n in range(21)], "pallet-1-item-1"):
            assert (await c.post("/shop/catalog/refresh", json={"items": bad}, headers=H)).status == 400, bad
        r = await c.post("/shop/catalog/refresh", json={"items": [_sku(item), "pallet-999-item-1"]}, headers=H)
        data = await r.json()
        assert data["scope"] == "requested_items" and [i["sku"] for i in data["items"]] == [_sku(item), "pallet-999-item-1"]
        unknown = data["items"][1]
        assert unknown["bot_status"] == "unavailable" and unknown["ebay_checked_at"] is None
        assert unknown["available_quantity"] == 0 and unknown["price_cents"] is None
        fetch.side_effect = ebay_api.EbayAPIError("down")
        monkeypatch.setattr("ebay_catalog.REFRESH_REUSE_SECONDS", 0)
        r = await c.post("/shop/catalog/refresh", json={"items": [_sku(item)]}, headers=H)
        assert r.status == 503 and "down" not in await r.text()
    _run(shop, steps)
    assert shop.db.get_ebay_listing_data(item["id"])["price"] == 22.0  # bot follows eBay
    assert shop.created == []  # refresh never starts a checkout


def test_checkout_uses_verified_ebay_prices(shop, monkeypatch):
    a, b, sold, multi = _listed(shop.db, price=15.0, number_of_items=4)
    shop.db.update_status(sold["id"], shop.db.STATUS_SOLD, actor_id=1)
    _setup(shop, monkeypatch, [_live(a, 22.0, "1"), _live(b, 50.0, "2"), _live(sold, 9.0, "3"),
                               _live(multi, 30.0, "4", quantity=3)])

    async def steps(c):
        r = await _checkout(c, {_sku(a): 8652, _sku(b): 5000})  # stale website price for a
        body = await r.json()
        assert r.status == 409 and body["code"] == "price_changed"
        assert body["price_changes"] == [{"sku": _sku(a), "website_cents": 8652, "current_cents": 2200}]
        assert body["current_prices"] == {_sku(a): 2200, _sku(b): 5000}
        r = await _checkout(c, {_sku(a): 2200, _sku(sold): 900, _sku(multi): 3000})
        body = await r.json()
        assert r.status == 409 and body["code"] == "not_available"
        assert body["unavailable_items"] == [_sku(sold), _sku(multi)]
        assert shop.db.get_item(a["id"])["status"] == shop.db.STATUS_LISTED  # a price/stock conflict sells nothing
        assert (await c.post("/shop/checkout", json={"items": [_sku(a)], "website_prices": {_sku(a): 2200}},
                             headers=H)).status == 400  # no contract
        for prices in ({_sku(a): 22.0}, {_sku(a): 0}, {_sku(a): True}, ["x"], {_sku(a): 2200, "pallet-9-item-9": 5}):
            r = await c.post("/shop/checkout", json={"items": [_sku(a)], "checkout_contract": "website-prices-v1",
                                                     "website_prices": prices}, headers=H)
            assert r.status == 400, prices
        r = await _checkout(c, {_sku(a): 2200, _sku(b): 5000})
        data = await r.json()
        assert r.status == 200 and data["subtotal_cents"] == 7200
        assert data["website_prices"] == {_sku(a): 2200, _sku(b): 5000} and data["currency"] == "usd"
        r = await _checkout(c, {_sku(a): 2200})  # now held by that checkout
        body = await r.json()
        assert r.status == 409 and body["code"] == "not_available" and body["unavailable_items"] == [_sku(a)]
    _run(shop, steps)
    assert len(shop.created) == 1
    assert [l["price_data"]["unit_amount"] for l in shop.created[0]["line_items"]] == [2200, 5000]


def test_checkout_503s_when_ebay_cannot_be_checked(shop, monkeypatch):
    item = _listed(shop.db)[0]
    fetch = _setup(shop, monkeypatch, [])
    fetch.side_effect = ebay_api.EbayAPIError("down")

    async def steps(c):
        r = await _checkout(c, {_sku(item): 2500})
        assert r.status == 503 and (await r.json())["code"] == "ebay_unavailable"
    _run(shop, steps)
    assert shop.created == []


def test_each_shopper_has_their_own_rate_limit(shop, monkeypatch):
    from webapp import shop as routes
    item = _listed(shop.db)[0]
    _setup(shop, monkeypatch, [_live(item, 22.0)])
    monkeypatch.setattr(routes, "RATE_LIMIT", 2)

    async def steps(c):
        def post(ip):
            return c.post("/shop/catalog/refresh", json={"items": [_sku(item)]},
                          headers={"X-Shop-Key": KEY, "X-Client-IP": ip})
        assert [(await post("1.1.1.1")).status for _ in range(3)] == [200, 200, 429]
        assert (await post("2.2.2.2")).status == 200
    _run(shop, steps)


# ------------------------------------------------ disposable checkout test item

def _test_item(shop, monkeypatch, keys="sk_test_x"):
    import shop_test_item
    fetch = _setup(shop, monkeypatch, [])
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", keys)
    live_create = stripe_api.create_checkout_session

    async def test_create(params, idempotency_key=None):  # Stripe reports test sessions as livemode=false
        session = await live_create(params, idempotency_key)
        return {**session, "id": session["id"].replace("cs_live_", "cs_test_"), "livemode": False}
    monkeypatch.setattr(stripe_api, "create_checkout_session", test_create)
    item = shop_test_item.create(actor_id=1)
    return item, fetch


def test_test_item_is_isolated_on_test_keys(shop, monkeypatch):
    real = _listed(shop.db, price=15.0)[0]
    item, fetch = _test_item(shop, monkeypatch)
    fetch.return_value = [_live(real, 22.0)]
    sku = _sku(item)
    assert shop.db.get_pallet(item["pallet_id"])["archived"] == 1  # hidden from pallet lists

    async def steps(c):
        await c.post("/shop/catalog/refresh", json={"items": [_sku(real)]}, headers=H)
        full = await (await c.get("/shop/catalog", headers=H)).json()
        assert sku not in [i["sku"] for i in full["items"]]  # never in the normal feed
        test = await (await c.get("/shop/catalog?scope=test", headers=H)).json()
        assert test["scope"] == "test" and [i["sku"] for i in test["items"]] == [sku]
        rec = test["items"][0]
        assert rec["ebay_listing_id"] == f"TEST-{sku}" and rec["price_cents"] == 100 and rec["available_quantity"] == 1
        calls = fetch.await_count
        r = await c.post("/shop/catalog/refresh", json={"items": [sku]}, headers=H)
        assert (await r.json())["items"][0]["bot_status"] == "listed"
        assert fetch.await_count == calls  # the test item never needs eBay
        # on test keys a REAL item can't be bought
        r = await _checkout(c, {_sku(real): 2200})
        assert r.status == 409 and (await r.json())["unavailable_items"] == [_sku(real)]
        r = await _checkout(c, {sku: 500})
        assert (await r.json())["code"] == "price_changed"
        r = await _checkout(c, {sku: 100})
        data = await r.json()
        assert r.status == 200 and data["subtotal_cents"] == 100 and data["livemode"] is False
        r = await _checkout(c, {sku: 100})
        assert (await r.json())["code"] == "not_available"  # held by the first
    _run(shop, steps)
    assert shop.created[0]["line_items"][0]["price_data"]["unit_amount"] == 100


def test_test_item_is_unknown_on_live_keys(shop, monkeypatch):
    item, _ = _test_item(shop, monkeypatch, keys="sk_live_x")
    sku = _sku(item)

    async def steps(c):
        assert (await c.get("/shop/catalog?scope=test", headers=H)).status == 404
        rec = (await (await c.post("/shop/catalog/refresh", json={"items": [sku]}, headers=H)).json())["items"][0]
        assert rec["bot_status"] == "unavailable" and rec["ebay_checked_at"] is None and rec["price_cents"] is None
        assert (await (await _checkout(c, {sku: 100})).json())["code"] == "not_available"
    _run(shop, steps)
    assert shop.created == []


def test_test_payment_only_flips_the_test_item_and_reset_relists_it(shop, monkeypatch):
    import shop_test_item
    import stripe_shop
    from tests.test_stripe_shop import _paid_session
    item, _ = _test_item(shop, monkeypatch)
    real = _listed(shop.db)[0]
    sid = asyncio.run(stripe_shop.start_checkout([_sku(item)]))["session_id"]
    shop.db.update_stripe_checkout(sid, status="paid")
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session",
                        AsyncMock(return_value={**_paid_session(sid), "livemode": False}))
    bot = _bot(shop.db)
    result = asyncio.run(stripe_shop.fulfill(bot, sid))
    assert result["test_mode"] is True and shop.db.get_item(item["id"])["status"] == shop.db.STATUS_SOLD
    assert bot.flow.calls == [] and shop.qb == []  # no cards, no sale, no QuickBooks
    assert shop.db.get_stripe_checkout(sid)["sale_id"] is None

    # a test payment for a real item never touches it
    sid2 = asyncio.run(stripe_shop.start_checkout([real["id"]]))["session_id"]
    shop.db.update_stripe_checkout(sid2, status="paid")
    stripe_api.retrieve_checkout_session.return_value = {**_paid_session(sid2), "livemode": False}
    assert asyncio.run(stripe_shop.fulfill(bot, sid2))["ok"] is False
    assert shop.db.get_item(real["id"])["status"] == shop.db.STATUS_LISTED

    # reset: listed again, an open test checkout cancelled
    lines = asyncio.run(shop_test_item.reset(actor_id=1))
    assert shop.db.get_item(item["id"])["status"] == shop.db.STATUS_LISTED and "Listed again" in lines[0]
    sid3 = asyncio.run(stripe_shop.start_checkout([_sku(item)]))["session_id"]
    asyncio.run(shop_test_item.reset(actor_id=1))
    assert shop.db.get_stripe_checkout(sid3)["status"] == "expired"
    assert shop.db.active_stripe_holds([item["id"]]) == {}
    assert shop_test_item.create(actor_id=1)["id"] == item["id"]  # create is idempotent
    assert shop_test_item.retire(actor_id=1) == [_sku(item)] and shop_test_item.test_skus() == {}
