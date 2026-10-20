"""
Stripe website checkout (stripe_api.py, stripe_shop.py, webapp/shop.py):
the bot's own prices and holds, webhook signatures, and fulfillment into
the same Sold / sale / QuickBooks steps as everything else - never twice.
"""
import asyncio
import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import ebay_api
import quickbooks
import stripe_api
import stripe_shop

SECRET = "whsec_test"


@pytest.fixture
def shop(fresh_db, monkeypatch):
    for key, value in {"STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_ENABLED": True, "STRIPE_WEBHOOK_SECRET": SECRET,
                       "STRIPE_SHOP_ORIGINS": ("https://shop.test",), "STRIPE_SHIPPING_PRICE": 0.0,
                       "QUICKBOOKS_WEBSITE_SALES_ITEM_ID": "6", "QUICKBOOKS_SALES_TAX_PAYABLE_ACCOUNT_ID": "111",
                       "QUICKBOOKS_STRIPE_FEES_ACCOUNT_ID": "83", "QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID": "24",
                       "QUICKBOOKS_ENABLED": True, "EBAY_ENABLED": True, "WEB_BASE_URL": "http://dash.test",
                       "PRESERVE_DISCORD_HISTORY": False}.items():
        monkeypatch.setattr(config, key, value)
    created = []

    async def create(params, idempotency_key=None):
        created.append(params)
        return {"id": f"cs_test_{len(created)}", "url": f"https://checkout.stripe.com/c/pay/cs_test_{len(created)}"}
    monkeypatch.setattr(stripe_api, "create_checkout_session", create)
    monkeypatch.setattr(stripe_api, "expire_checkout_session", AsyncMock(return_value={}))
    qb_calls = []

    async def qb_request(method, path, *, params=None, json_body=None):
        qb_calls.append({"path": path, "json": json_body})
        if path == "salesreceipt":
            return {"SalesReceipt": {"Id": "SR-1", "DocNumber": "1001"}}
        return {"JournalEntry": {"Id": "JE-1", "DocNumber": "JE-1"}}
    monkeypatch.setattr(quickbooks, "_request", qb_request)
    monkeypatch.setattr(quickbooks, "is_connected", lambda: True)
    monkeypatch.setattr(ebay_api, "end_listing_for_item", AsyncMock(return_value="eBay listing ended."))
    monkeypatch.setattr("finance_utils.refresh_finance_message", AsyncMock())
    monkeypatch.setattr("finance_utils.post_to_finance_audit_log", AsyncMock())
    monkeypatch.setattr("finance_utils.update_accounting_entries", AsyncMock())
    return SimpleNamespace(db=fresh_db, created=created, qb=qb_calls)


def _listed(db, price=25.0, number_of_items=1, pallet="Pallets 1-6"):
    pallet_id = db.create_pallet(pallet, category_id=len(pallet), created_by=1)
    items = []
    for _ in range(number_of_items):
        item_id = db.create_item(pallet_id, "lamp", [], 1)
        db.save_ebay_listing_data(item_id, "Brass Lamp", "123", "1500", price, {})
        db.set_ebay_item_id(item_id, "110011")
        db.update_status(item_id, db.STATUS_LISTED, actor_id=1)
        items.append(db.get_item(item_id))
    return items


class _Flow:
    """Stands in for ItemFlow.do_mark_sold - the real one is tested elsewhere."""
    def __init__(self, db):
        self.db, self.calls = db, []

    async def do_mark_sold(self, item_id, platform, actor_id, old_card=None):
        self.calls.append((item_id, platform))
        self.db.record_sale_platform(item_id, platform, actor_id=actor_id)
        self.db.update_status(item_id, self.db.STATUS_SOLD, actor_id=actor_id)


def _bot(db):
    flow = _Flow(db)
    return SimpleNamespace(get_cog=lambda name: flow if name == "ItemFlow" else None,
                           get_channel=lambda cid: None, flow=flow)


def _paid_session(session_id, total=26.75, tax=1.75, fee=1.08, ship=True):
    s = {"id": session_id, "object": "checkout.session", "payment_status": "paid", "amount_total": round(total * 100),
         "created": 1760000000, "total_details": {"amount_tax": round(tax * 100), "amount_shipping": 0},
         "payment_intent": {"latest_charge": {"balance_transaction": {"fee": round(fee * 100)}}}}
    if ship:
        s["collected_information"] = {"shipping_details": {"name": "Jane Buyer", "address": {
            "line1": "1 Main St", "line2": None, "city": "Muncie", "state": "IN", "postal_code": "47305",
            "country": "US"}}}
    return s


# ------------------------------------------------------------- stripe_api

def test_form_encoding_and_webhook_signatures():
    pairs = stripe_api._flatten({"line_items": [{"quantity": 1, "price_data": {"unit_amount": 500}}],
                                 "automatic_tax": {"enabled": True}, "skip": None})
    assert ("line_items[0][price_data][unit_amount]", "500") in pairs
    assert ("automatic_tax[enabled]", "true") in pairs and all(k != "skip" for k, _ in pairs)

    body, now = b'{"id": "evt_1"}', int(time.time())
    sig = hmac.new(SECRET.encode(), f"{now}.".encode() + body, hashlib.sha256).hexdigest()
    assert stripe_api.verify_webhook(body, f"t={now},v1={sig}", SECRET)["id"] == "evt_1"
    for header in (f"t={now},v1=deadbeef", f"t={now - 1000},v1={sig}", "v1=" + sig, ""):
        with pytest.raises(stripe_api.StripeError):
            stripe_api.verify_webhook(body, header, SECRET)
    with pytest.raises(stripe_api.StripeError):
        stripe_api.verify_webhook(body + b" ", f"t={now},v1={sig}", SECRET)  # tampered body


# ---------------------------------------------------------------- checkout

def test_checkout_uses_the_bots_price_tax_settings_and_holds_the_item(shop):
    item = _listed(shop.db, price=24.99)[0]
    result = asyncio.run(stripe_shop.start_checkout([f"pallet-{item['pallet_id']}-item-{item['item_number']}"]))
    assert result["url"].startswith("https://checkout.stripe.com/")
    params = shop.created[0]
    line = params["line_items"][0]["price_data"]
    assert line["unit_amount"] == 2499 and line["tax_behavior"] == "exclusive"
    assert line["product_data"]["tax_code"] == "txcd_99999999"
    assert params["automatic_tax"] == {"enabled": True}
    assert params["shipping_address_collection"] == {"allowed_countries": ["US"]}
    assert params["shipping_options"][0]["shipping_rate_data"]["fixed_amount"]["amount"] == 0  # free shipping
    assert params["expires_at"] - time.time() >= 30 * 60  # Stripe's minimum
    assert "{CHECKOUT_SESSION_ID}" in params["success_url"]
    # held: a second buyer can't start a checkout for the same item
    with pytest.raises(stripe_shop.CheckoutError, match="checking out"):
        asyncio.run(stripe_shop.start_checkout([item["id"]]))


def test_checkout_accepts_the_websites_product_slug(shop):
    item = _listed(shop.db)[0]
    slug = f"ebay-318921853363-pallet-{item['pallet_id']}-item-{item['item_number']}"
    assert asyncio.run(stripe_shop.start_checkout([slug]))["url"]
    assert shop.created[0]["line_items"][0]["price_data"]["product_data"]["metadata"]["item_id"] == str(item["id"])


def test_checkout_refuses_unsellable_items(shop):
    item = _listed(shop.db)[0]
    shop.db.update_status(item["id"], shop.db.STATUS_SOLD, actor_id=1)
    with pytest.raises(stripe_shop.CheckoutError, match="just sold"):
        asyncio.run(stripe_shop.start_checkout([item["id"]]))
    with pytest.raises(stripe_shop.CheckoutError, match="isn't for sale"):
        asyncio.run(stripe_shop.start_checkout(["pallet-99-item-1"]))
    with pytest.raises(stripe_shop.CheckoutError, match="twice"):
        asyncio.run(stripe_shop.start_checkout([item["id"], item["id"]]))
    with pytest.raises(stripe_shop.CheckoutError):
        asyncio.run(stripe_shop.start_checkout([]))
    assert shop.created == []


def test_local_pickup_link_collects_billing_address_not_shipping(shop):
    item = _listed(shop.db)[0]
    asyncio.run(stripe_shop.start_checkout([item["id"]], source="link", delivery="pickup", actor_id=7))
    params = shop.created[0]
    assert "shipping_address_collection" not in params and params["billing_address_collection"] == "required"
    assert params["expires_at"] - time.time() > 20 * 3600  # a payment link stays open most of a day


def test_expired_session_releases_the_hold(shop):
    item = _listed(shop.db)[0]
    first = asyncio.run(stripe_shop.start_checkout([item["id"]]))
    event = {"id": "evt_exp", "type": "checkout.session.expired",
             "data": {"object": {"id": first["session_id"], "object": "checkout.session"}}}
    assert "released" in asyncio.run(stripe_shop.handle_event(_bot(shop.db), event))
    assert asyncio.run(stripe_shop.start_checkout([item["id"]]))["session_id"] == "cs_test_2"
    assert asyncio.run(stripe_shop.handle_event(_bot(shop.db), event)) == "duplicate"


# ------------------------------------------------------------- fulfillment

def test_paid_order_is_fulfilled_once(shop, monkeypatch):
    item = _listed(shop.db, price=25.0)[0]
    sid = asyncio.run(stripe_shop.start_checkout([item["id"]]))["session_id"]
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=_paid_session(sid)))
    shop.db.update_stripe_checkout(sid, status="paid")
    bot = _bot(shop.db)

    result = asyncio.run(stripe_shop.fulfill(bot, sid))

    assert result["ok"] and result["status"] == "fulfilled"
    assert bot.flow.calls == [(item["id"], "Website")]
    fresh = shop.db.get_item(item["id"])
    assert fresh["sale_price"] == 25.0 and fresh["sale_platform"] == "Website"
    assert fresh["recipient_name"] == "Jane Buyer" and fresh["city"] == "Muncie"  # for the Pirate Ship export
    ebay_api.end_listing_for_item.assert_awaited_once()
    receipt = next(c for c in shop.qb if c["path"] == "salesreceipt")["json"]
    detail = receipt["Line"][0]["SalesItemLineDetail"]
    assert detail["ItemRef"] == {"value": "6"} and detail["TaxCodeRef"] == {"value": "NON"}  # no QuickBooks tax on top
    assert receipt["Line"][0]["Amount"] == 25.0
    je = next(c for c in shop.qb if c["path"] == "journalentry")["json"]
    lines = sorted((l["JournalEntryLineDetail"]["PostingType"], l["JournalEntryLineDetail"]["AccountRef"]["value"],
                    l["Amount"]) for l in je["Line"])
    assert lines == [("Credit", "111", 1.75), ("Debit", "24", 0.67), ("Debit", "83", 1.08)]
    assert shop.db.active_stripe_holds([item["id"]]) == {}

    calls = len(shop.qb)
    shop.db.update_stripe_checkout(sid, status="paid")  # e.g. a resent webhook
    asyncio.run(stripe_shop.fulfill(bot, sid))
    assert len(shop.qb) == calls and bot.flow.calls == [(item["id"], "Website")]  # nothing twice


def test_item_sold_on_ebay_meanwhile_flags_a_refund_and_logs_nothing(shop, monkeypatch):
    item = _listed(shop.db)[0]
    sid = asyncio.run(stripe_shop.start_checkout([item["id"]]))["session_id"]
    shop.db.record_sale_platform(item["id"], "eBay", actor_id=1)
    shop.db.update_status(item["id"], shop.db.STATUS_SOLD, actor_id=1)  # sold on eBay while the buyer paid
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=_paid_session(sid)))
    shop.db.update_stripe_checkout(sid, status="paid")

    result = asyncio.run(stripe_shop.fulfill(_bot(shop.db), sid))

    assert not result["ok"] and "refund" in result["message"]
    checkout = shop.db.get_stripe_checkout(sid)
    assert checkout["status"] == "attention" and checkout["note"].startswith(stripe_shop.CONFLICT)
    assert checkout["sale_id"] is None and shop.qb == []
    assert asyncio.run(stripe_shop.retry_pending(_bot(shop.db))) == 0
    assert shop.qb == []


def test_missing_fee_is_booked_by_the_retry(shop, monkeypatch):
    item = _listed(shop.db)[0]
    sid = asyncio.run(stripe_shop.start_checkout([item["id"]]))["session_id"]
    no_fee = _paid_session(sid)
    no_fee["payment_intent"] = {"latest_charge": {"balance_transaction": None}}
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=no_fee))
    shop.db.update_stripe_checkout(sid, status="paid")
    bot = _bot(shop.db)
    assert asyncio.run(stripe_shop.fulfill(bot, sid))["status"] == "attention"
    assert not any(c["path"] == "journalentry" for c in shop.qb)

    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=_paid_session(sid)))
    assert asyncio.run(stripe_shop.retry_pending(bot)) == 1
    assert shop.db.get_stripe_checkout(sid)["status"] == "fulfilled"
    assert sum(c["path"] == "salesreceipt" for c in shop.qb) == 1  # the sale itself wasn't redone


# ------------------------------------------------------------- web routes

def _client(shop):
    from webapp.server import create_app
    return TestClient(TestServer(create_app(_bot(shop.db))))


def test_webhook_endpoint_checks_the_signature(shop, monkeypatch):
    item = _listed(shop.db)[0]
    sid = asyncio.run(stripe_shop.start_checkout([item["id"]]))["session_id"]
    body = json.dumps({"id": "evt_2", "type": "checkout.session.expired",
                       "data": {"object": {"id": sid, "object": "checkout.session"}}}).encode()

    async def go():
        c = _client(shop)
        await c.start_server()
        try:
            r = await c.post("/stripe/webhook", data=body, headers={"Stripe-Signature": "t=1,v1=bad"})
            assert r.status == 400
            assert shop.db.get_stripe_checkout(sid)["status"] == "open"
            now = int(time.time())
            sig = hmac.new(SECRET.encode(), f"{now}.".encode() + body, hashlib.sha256).hexdigest()
            r = await c.post("/stripe/webhook", data=body, headers={"Stripe-Signature": f"t={now},v1={sig}"})
            assert r.status == 200
            assert shop.db.get_stripe_checkout(sid)["status"] == "expired"
        finally:
            await c.close()
    asyncio.run(go())


# ------------------------------------------- website-prices-v1 contract

KEY = "k" * 40


def _priced_stripe(monkeypatch, shop, subtotal_offset=0):
    """Fake Stripe that reports its own subtotal, like the real one."""
    async def create(params, idempotency_key=None):
        shop.created.append(params)
        subtotal = sum(l["price_data"]["unit_amount"] for l in params["line_items"]) + subtotal_offset
        n = len(shop.created)
        return {"id": f"cs_live_{n}", "url": f"https://checkout.stripe.com/g/pay/cs_live_{n}", "amount_subtotal": subtotal}
    monkeypatch.setattr(stripe_api, "create_checkout_session", create)


def _contract_post(c, refs, prices, key=KEY, **extra):
    body = {"items": refs, "checkout_contract": "website-prices-v1", "website_prices": prices, **extra}
    headers = {"X-Client-IP": "9.9.9.9"}
    if key:
        headers["X-Shop-Key"] = key
    return c.post("/shop/checkout", json=body, headers=headers)


def test_a_stripe_total_that_doesnt_match_is_cancelled(shop, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SHOP_SERVER_KEY", KEY)
    _priced_stripe(monkeypatch, shop, subtotal_offset=1)
    item = _listed(shop.db)[0]
    ref = f"pallet-{item['pallet_id']}-item-{item['item_number']}"
    with pytest.raises(stripe_shop.CheckoutError, match="didn't come out right"):
        asyncio.run(stripe_shop.start_checkout([ref], website_prices={ref: 2200}))
    stripe_api.expire_checkout_session.assert_awaited_once_with("cs_live_1")
    assert shop.db.active_stripe_holds([item["id"]]) == {}


def test_session_status_for_the_website(shop, monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SHOP_SERVER_KEY", KEY)
    _priced_stripe(monkeypatch, shop)
    item = _listed(shop.db)[0]
    ref = f"pallet-{item['pallet_id']}-item-{item['item_number']}"
    sid = asyncio.run(stripe_shop.start_checkout([ref], website_prices={ref: 2200}))["session_id"]

    async def go():
        c = _client(shop)
        await c.start_server()
        try:
            assert (await c.get(f"/shop/session/{sid}")).status == 403
            r = await c.get(f"/shop/session/{sid}", headers={"X-Shop-Key": KEY})
            assert (await r.json())["state"] == "open"
            shop.db.update_stripe_checkout(sid, status="fulfilled", amount_total=23.54)
            data = await (await c.get(f"/shop/session/{sid}", headers={"X-Shop-Key": KEY})).json()
            assert data["paid"] is True and data["items"] == [ref] and data["amount_total_cents"] == 2354
            assert (await c.get("/shop/session/cs_nope", headers={"X-Shop-Key": KEY})).status == 404
        finally:
            await c.close()
    asyncio.run(go())
