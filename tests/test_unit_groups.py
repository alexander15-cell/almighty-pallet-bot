"""Multi-unit listings (unit_groups.py): one bot item per unit, website
checkout of one unit at a time, eBay quantity kept in step - the checks from
Jacob's multi-unit handoff, against a simulated eBay listing."""
import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import ebay_api
import stripe_api
import stripe_shop
import unit_groups
from tests.test_shop_catalog import H, _live, _run, _setup, _sku
from tests.test_stripe_shop import _bot, _listed, _paid_session, shop  # noqa: F401 (fixture)

LISTING = "860000000043"


def _checkout(c, prices, delivery="pickup"):
    return c.post("/shop/checkout", json={"items": list(prices), "checkout_contract": "website-prices-v1",
                                          "website_prices": prices, "delivery": delivery}, headers=H)


class FakeEbay:
    """One hand-made listing: Quantity (total) and QuantitySold, like GetItem."""

    def __init__(self, monkeypatch, total=3, sold=0):
        self.total, self.sold, self.status, self.writes, self.listing = total, sold, "Active", [], LISTING
        monkeypatch.setattr(ebay_api, "get_listing_quantity", AsyncMock(side_effect=self.read))
        monkeypatch.setattr(ebay_api, "set_available_quantity", AsyncMock(side_effect=self.write))
        monkeypatch.setattr(ebay_api, "end_fixed_price_listing", AsyncMock(side_effect=self.end))
        monkeypatch.setattr(ebay_api, "relist_listing_for_item", AsyncMock(side_effect=self.relist))
        monkeypatch.setattr(ebay_api, "is_inventory_model_listing", AsyncMock(return_value=False))

    @property
    def available(self):
        return max(0, self.total - self.sold)

    async def read(self, listing_id):
        return {"total": self.total, "sold": self.sold, "available": self.available, "status": self.status}

    async def write(self, listing_id, quantity):
        self.writes.append(quantity)
        self.total = self.sold + quantity

    async def end(self, listing_id):
        self.status = "Completed"
        return "eBay listing ended."

    async def relist(self, item, listing_id=None):
        self.listing, self.status, self.total, self.sold = "860000000999", "Active", self.available or 1, 0
        return self.listing

    def ebay_sale(self, n=1):
        self.sold += n


@pytest.fixture
def group(shop, monkeypatch):
    """A listing with 3 units on eBay, set up as a unit group (anchor + 2)."""
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    anchor = _listed(shop.db, price=22.0)[0]
    shop.db.set_ebay_item_id(anchor["id"], LISTING)
    fetch = _setup(shop, monkeypatch, [_live(anchor, 22.0, LISTING, quantity=3)])
    ebay = FakeEbay(monkeypatch, total=3)
    p = unit_groups.plan({"quantity": 3}, anchor)
    assert p["add"] == 2 and not p["grouped"]
    asyncio.run(unit_groups.apply_plan(p, actor_id=1))
    sku = _sku(anchor)
    return type("G", (), {"sku": sku, "anchor": anchor, "ebay": ebay, "fetch": fetch, "db": shop.db})


def _refresh(c, sku):
    return c.post("/shop/catalog/refresh", json={"items": [sku]}, headers=H)


async def _qty(c, sku):
    return (await (await _refresh(c, sku)).json())["items"][0]


def _session_of(response_json):
    return response_json["session_id"]


def test_reserve_one_of_three_then_pay_leaves_two_listed(shop, group, monkeypatch):
    sku = group.sku
    seen = {}

    async def steps(c):
        rec = await _qty(c, sku)
        assert (rec["bot_status"], rec["available_quantity"], rec["ebay_listing_id"]) == ("listed", 3, LISTING)
        r = await _checkout(c, {sku: 2200}, delivery="pickup")
        data = await r.json()
        assert r.status == 200 and data["website_prices"] == {sku: 2200} and data["subtotal_cents"] == 2200
        seen["sid"] = data["session_id"]
        rec = await _qty(c, sku)
        assert (rec["bot_status"], rec["available_quantity"]) == ("listed", 2)
    _run(shop, steps)
    assert group.ebay.writes == [2] and group.ebay.available == 2  # held unit came off eBay

    sid = seen["sid"]
    shop.db.update_stripe_checkout(sid, status="paid")
    monkeypatch.setattr(stripe_api, "retrieve_checkout_session", AsyncMock(return_value=_paid_session(sid, ship=False)))
    bot = _bot(shop.db)
    asyncio.run(stripe_shop.fulfill(bot, sid))
    asyncio.run(stripe_shop.fulfill(bot, sid))  # re-delivered: nothing more happens
    assert len(bot.flow.calls) == 1
    units = shop.db.unit_group_member_items(sku)
    assert sorted(u["status"] for u in units) == ["listed", "listed", "sold"]
    assert group.ebay.available == 2 and group.ebay.status == "Active"  # listing NOT ended
    assert unit_groups.public_sku(units[-1]) == sku


def test_expiry_restores_only_that_unit(shop, group):
    sku = group.sku

    async def steps(c):
        first = await (await _checkout(c, {sku: 2200}, delivery="pickup")).json()
        second = await (await _checkout(c, {sku: 2200}, delivery="pickup")).json()
        assert (await _qty(c, sku))["available_quantity"] == 1
        body = {"id": "evt_x", "type": "checkout.session.expired",
                "data": {"object": {"id": first["session_id"], "object": "checkout.session"}}}
        assert await stripe_shop.handle_event(_bot(shop.db), body) == "expired - items released"
        assert await stripe_shop.handle_event(_bot(shop.db), body) == "duplicate"
        assert (await _qty(c, sku))["available_quantity"] == 2
        assert shop.db.get_stripe_checkout(second["session_id"])["status"] == "open"  # the other hold untouched
    _run(shop, steps)
    assert group.ebay.available == 2 and group.ebay.writes[-1] == 2


def test_last_unit_goes_to_one_buyer_only_and_never_negative(shop, group):
    sku = group.sku

    async def steps(c):
        await _checkout(c, {sku: 2200}, delivery="pickup")
        await _checkout(c, {sku: 2200}, delivery="pickup")
        results = await asyncio.gather(*(_checkout(c, {sku: 2200}, delivery="pickup") for _ in range(2)))
        codes = sorted([r.status for r in results])
        assert codes == [200, 409]
        loser = [r for r in results if r.status == 409][0]
        assert (await loser.json())["unavailable_items"] == [sku]
        rec = await _qty(c, sku)
        assert (rec["bot_status"], rec["available_quantity"]) == ("listed", 0)
    _run(shop, steps)
    # nothing sellable left -> the eBay listing came down (and will come back if a hold expires)
    assert group.ebay.status == "Completed"
    assert shop.db.get_unit_group(sku)["ended_by_bot"] == 1


def test_expired_hold_on_an_ended_listing_relists_it_with_the_new_id(shop, group):
    sku = group.sku
    sids = []

    async def steps(c):
        for _ in range(3):
            sids.append((await (await _checkout(c, {sku: 2200}, delivery="pickup")).json())["session_id"])
    _run(shop, steps)
    assert group.ebay.status == "Completed"
    asyncio.run(stripe_shop.handle_event(_bot(shop.db), {"id": "evt_e", "type": "checkout.session.expired",
                                                         "data": {"object": {"id": sids[0],
                                                                             "object": "checkout.session"}}}))
    assert group.ebay.status == "Active" and group.ebay.listing == "860000000999"
    assert shop.db.get_unit_group(sku)["listing_id"] == "860000000999"
    assert {shop.db.get_ebay_listing_data(u["id"])["ebay_item_id"] for u in shop.db.unit_group_member_items(sku)} \
        == {"860000000999"}


def test_an_unrecorded_ebay_sale_blocks_the_website_instead_of_overselling(shop, group):
    sku = group.sku
    group.ebay.ebay_sale()  # someone bought one on eBay; the order poll hasn't run yet

    async def steps(c):
        r = await _checkout(c, {sku: 2200}, delivery="pickup")
        assert r.status == 409 and (await r.json())["unavailable_items"] == [sku]
    _run(shop, steps)
    assert group.ebay.writes == []  # never wrote a quantity that would resurrect the sold unit
    assert shop.db.list_stripe_checkouts(limit=1)[0]["status"] == "expired"  # its checkout was cancelled
    assert "waiting for the order to sync" in shop.db.get_unit_group(sku)["last_error"]

    # the order poll records it -> safe again
    anchor = shop.db.get_item(group.anchor["id"])
    taken = unit_groups.assign_units("27-77", {"sku": sku, "quantity": 1, "line_item_id": "L1"}, anchor,
                                     order_date="2099-01-01T00:00:00Z")
    shop.db.update_status(taken[0]["id"], shop.db.STATUS_SOLD, actor_id=0)
    assert asyncio.run(unit_groups.sync(sku)).ok


def test_ebay_orders_take_specific_units_and_keep_them(shop, group):
    anchor = shop.db.get_item(group.anchor["id"])
    line = {"sku": group.sku, "quantity": 2, "line_item_id": "L9"}
    first = unit_groups.assign_units("27-1", line, anchor, order_date="2099-01-01T00:00:00Z")
    again = unit_groups.assign_units("27-1", line, anchor, order_date="2099-01-01T00:00:00Z")
    assert len(first) == 2 and [u["id"] for u in first] == [u["id"] for u in again]
    other = unit_groups.assign_units("27-2", {"sku": group.sku, "quantity": 1, "line_item_id": "M1"}, anchor,
                                     order_date="2099-01-01T00:00:00Z")
    assert other[0]["id"] not in {u["id"] for u in first}  # never one unit for two orders
    old = unit_groups.assign_units("26-0", {"sku": group.sku, "quantity": 1}, anchor, order_date="2000-01-01")
    assert old == [anchor]  # an order from before the group existed keeps the old behaviour


def test_added_units_are_not_products_of_their_own(shop, group):
    extra = [u for u in shop.db.unit_group_member_items(group.sku) if u["id"] != group.anchor["id"]][0]

    async def steps(c):
        await _refresh(c, group.sku)
        full = await (await c.get("/shop/catalog", headers=H)).json()
        skus = [i["sku"] for i in full["items"]]
        assert group.sku in skus and _sku(extra) not in skus
        rec = await _qty(c, _sku(extra))
        assert rec["bot_status"] == "unavailable" and rec["available_quantity"] == 0
    _run(shop, steps)


def test_ungrouped_multi_unit_listing_still_cant_be_bought(shop, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    item = _listed(shop.db, price=30.0)[0]
    _setup(shop, monkeypatch, [_live(item, 30.0, "1", quantity=4)])

    async def steps(c):
        r = await _checkout(c, {_sku(item): 3000}, delivery="pickup")
        assert r.status == 409 and (await r.json())["code"] == "not_available"
    _run(shop, steps)


def test_disposable_multi_unit_test_item(shop, monkeypatch):
    import shop_test_item
    _setup(shop, monkeypatch, [])
    monkeypatch.setattr(config, "STRIPE_TEST_SECRET_KEY", "sk_test_separate")
    anchor = shop_test_item.create_multi(actor_id=1)
    assert shop_test_item.create_multi(actor_id=1)["id"] == anchor["id"]  # made once
    sku = _sku(anchor)
    modes = []
    live_create = stripe_api.create_checkout_session

    async def create(params, idempotency_key=None, test=False):
        modes.append(test)
        session = await live_create(params, idempotency_key, test)
        return {**session, "id": session["id"].replace("cs_live_", "cs_test_"), "livemode": not test}
    monkeypatch.setattr(stripe_api, "create_checkout_session", create)

    async def steps(c):
        test = await (await c.get("/shop/catalog?scope=test", headers=H)).json()
        rec = {i["sku"]: i for i in test["items"]}[sku]
        assert rec["available_quantity"] == 3 and rec["bot_status"] == "listed"
        r = await _checkout(c, {sku: 100}, delivery="pickup")
        assert (await r.json())["livemode"] is False
        test = await (await c.get("/shop/catalog?scope=test", headers=H)).json()
        assert {i["sku"]: i for i in test["items"]}[sku]["available_quantity"] == 2
    _run(shop, steps)
    assert modes == [True]
    lines = asyncio.run(shop_test_item.reset(actor_id=1))
    assert "3 units" in lines[-1] or any("3 units" in ln for ln in lines)
    assert len(unit_groups.sellable_units(sku)) == 3


def test_selling_one_unit_in_person_lowers_ebay_quantity_not_the_listing(shop, group, monkeypatch):
    from types import SimpleNamespace
    import cogs.item_flow as item_flow
    from cogs.item_flow import ItemFlow
    bot = SimpleNamespace(get_channel=lambda cid: SimpleNamespace(id=cid, send=AsyncMock()), get_cog=lambda n: None)
    flow = ItemFlow(bot)
    monkeypatch.setattr(item_flow, "send_item_card", AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(flow, "_old_card", AsyncMock(return_value=None))
    monkeypatch.setattr(flow, "_post_to_accounting_channel", AsyncMock())
    monkeypatch.setattr("finance_utils.refresh_finance_message", AsyncMock())
    unit = unit_groups.sellable_units(group.sku)[-1]
    asyncio.run(flow.do_mark_sold(unit["id"], "In Person", actor_id=1))
    assert group.ebay.available == 2 and group.ebay.status == "Active"


def test_order_card_and_sale_logging_use_the_assigned_unit(shop, group):
    import cogs.ebay as ebay_cog
    order = {"order_id": "27-55", "sale_date": "2099-01-01", "total_price": 22.0, "payment_status": "PAID",
             "line_items": [{"sku": group.sku, "quantity": 1}]}
    first = ebay_cog._match_order_line_items(order)
    second = ebay_cog._match_order_line_items(order)
    assert len(first) == 1 and first[0]["id"] == second[0]["id"]
    # a sold anchor doesn't block the listing's other units
    shop.db.update_status(group.anchor["id"], shop.db.STATUS_SOLD, actor_id=0)
    later = ebay_cog._match_order_line_items({**order, "order_id": "27-56"})
    assert later[0]["id"] not in (group.anchor["id"], first[0]["id"])


# ------------------------------------------------- verification follow-ups

def test_stripe_timeout_retries_with_the_same_key_and_gets_the_same_session(shop, monkeypatch):
    item = _listed(shop.db)[0]
    keys, calls = [], {"n": 0}

    async def flaky(params, idempotency_key=None, test=False):
        keys.append(idempotency_key)
        calls["n"] += 1
        if calls["n"] == 1:  # Stripe made it, but the answer was lost
            raise stripe_api.StripeError("no answer (TimeoutError)", ambiguous=True)
        return {"id": "cs_test_same", "url": "https://checkout.stripe.com/c/pay/cs_test_same"}
    monkeypatch.setattr(stripe_api, "create_checkout_session", flaky)
    result = asyncio.run(stripe_shop.start_checkout([item["id"]]))
    assert result["session_id"] == "cs_test_same" and len(keys) == 2 and keys[0] == keys[1]
    assert shop.db.active_stripe_holds([item["id"]]) == {item["id"]: "cs_test_same"}


def test_a_clear_stripe_refusal_is_not_retried(shop, monkeypatch):
    item = _listed(shop.db)[0]
    create = AsyncMock(side_effect=stripe_api.StripeError("card declined setup", ambiguous=False))
    monkeypatch.setattr(stripe_api, "create_checkout_session", create)
    with pytest.raises(stripe_shop.CheckoutError):
        asyncio.run(stripe_shop.start_checkout([item["id"]]))
    assert create.await_count == 1 and shop.db.active_stripe_holds([item["id"]]) == {}


def test_a_paid_checkout_the_bot_never_recorded_raises_refund_needed(shop, monkeypatch):
    sent = []
    monkeypatch.setattr(shop.db, "get_shared_channel_id", lambda name: 9)
    bot = type("B", (), {"get_channel": lambda self, cid: type("C", (), {"send": AsyncMock(side_effect=sent.append)})()})()
    paid = {"id": "evt_o", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_live_orphan", "object": "checkout.session", "payment_status": "paid", "amount_total": 2354,
        "metadata": {"source": "pallet-bot", "item_ids": "7"}}}}
    assert asyncio.run(stripe_shop.handle_event(bot, paid)).startswith("ALERT")
    assert "REFUND NEEDED" in sent[0] and "cs_live_orphan" in sent[0]
    other = {**paid, "data": {"object": {**paid["data"]["object"], "metadata": {}}}}
    assert asyncio.run(stripe_shop.handle_event(bot, other)) == "ignored (not a bot checkout)"


def test_a_real_listing_group_named_in_shop_test_skus_uses_test_keys(shop, group, monkeypatch):
    import shop_test_item
    monkeypatch.setattr(config, "SHOP_TEST_SKUS", frozenset({group.sku}))
    monkeypatch.setattr(config, "STRIPE_TEST_SECRET_KEY", "sk_test_separate")
    ids = shop_test_item.test_item_ids()
    assert {u["id"] for u in shop.db.unit_group_member_items(group.sku)} <= ids
    assert stripe_shop._uses_test_keys(unit_groups.sellable_units(group.sku)[:1]) is True
