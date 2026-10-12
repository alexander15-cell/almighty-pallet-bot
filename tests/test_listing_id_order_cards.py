"""
eBay orders whose listing has no SKU (created/edited by hand in Seller Hub):
the order parser used to drop every such line and reject the whole order,
so its Confirm & log sale card could never be posted - the order
automation still found the item (by listing ID) and moved it to Sold. Now
lines are kept by listing ID and matched by it. Plus /ebay order-card
(post one order's card on demand) and /finance sales (list logged sales).
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import ebay_api
import cogs.ebay as ebay_cog
import cogs.finance as finance_cog
from cogs.finance import Finance, format_sales_list

ORDERS = 990
ROLE = SimpleNamespace(name="role")


# ------------------------------------------------------------------ parsing

def test_order_lines_without_a_sku_are_kept_by_listing_id():
    order = ebay_api._parse_order({
        "orderId": "11-15276-35371", "creationDate": "2026-10-05T10:00:00.000Z",
        "pricingSummary": {"total": {"value": "22.50"}},
        "lineItems": [{"legacyItemId": 1102233, "quantity": 1},
                      {"sku": "pallet-1-item-2", "legacyItemId": "1104455"},
                      {"quantity": 1}],  # neither - still dropped
    })
    assert order["line_items"] == [
        {"sku": "", "quantity": 1, "legacy_item_id": "1102233"},
        {"sku": "pallet-1-item-2", "quantity": 1, "legacy_item_id": "1104455"},
    ]


def test_order_with_no_identifiable_lines_is_still_rejected():
    assert ebay_api._parse_order({"orderId": "1", "pricingSummary": {"total": {"value": "1"}},
                                  "lineItems": [{"quantity": 1}]}) is None


# ----------------------------------------------------------------- matching

def _item(db, pallet_id, listing_id=None):
    item_id = db.create_item(pallet_id, "lamp", [], 1)
    db.save_ebay_listing_data(item_id, "Lamp", "117503", "1000", 20.0, {})
    if listing_id:
        db.set_ebay_item_id(item_id, listing_id)
    return db.get_item(item_id)


def test_match_falls_back_to_the_confirmed_listing_id(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallets 1 -6", category_id=1, created_by=1)
    by_sku = _item(fresh_db, pallet_id)
    by_listing = _item(fresh_db, pallet_id, listing_id="1102233")
    order = {"line_items": [
        {"sku": f"pallet-{pallet_id}-item-{by_sku['item_number']}", "quantity": 1},
        {"sku": "", "quantity": 1, "legacy_item_id": "1102233"},
        {"sku": "", "quantity": 1, "legacy_item_id": "999"},  # unknown listing - skipped
    ]}
    assert [i["id"] for i in ebay_cog._match_order_line_items(order)] == [by_sku["id"], by_listing["id"]]


def test_same_item_matched_two_ways_is_listed_once(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, listing_id="1102233")
    order = {"line_items": [{"sku": f"pallet-{pallet_id}-item-{item['item_number']}", "legacy_item_id": "1102233"}]}
    assert len(ebay_cog._match_order_line_items(order)) == 1


# ------------------------------------------------- the production scenario

class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.sent))


class _Bot:
    def __init__(self, item_flow=None):
        self.channel = _Channel()
        self.item_flow = item_flow

    def get_channel(self, channel_id):
        return self.channel if channel_id == ORDERS else None

    def get_cog(self, name):
        return self.item_flow


def test_sold_item_from_a_sku_less_order_now_gets_its_card(fresh_db, monkeypatch):
    fresh_db.set_shared_channel("ebay-orders", ORDERS)
    pallet_id = fresh_db.create_pallet("Pallets 1 -6", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, listing_id="1102233")
    with fresh_db.get_conn() as conn:  # created before the order
        conn.execute("UPDATE items SET created_at = '2025-01-01T00:00:00+00:00' WHERE id = ?", (item["id"],))
    fresh_db.update_status(item["id"], fresh_db.STATUS_SOLD, actor_id=None)
    raw = {"orderId": "11-15276-35371", "creationDate": "2026-10-05T10:00:00.000Z",
           "pricingSummary": {"total": {"value": "22.50"}}, "lineItems": [{"legacyItemId": "1102233"}]}
    monkeypatch.setattr(ebay_cog.ebay_api, "get_order", AsyncMock(return_value=ebay_api._parse_order(raw)))
    bot = _Bot(item_flow=SimpleNamespace(auto_mark_sold_from_ebay=AsyncMock(), auto_mark_shipped_from_ebay=AsyncMock()))
    modified = [{"order_id": "11-15276-35371", "order_date": "2026-10-05T10:00:00.000Z", "payment_status": "PAID",
                 "cancel_state": "NONE_REQUESTED",
                 "line_items": [{"line_item_id": "L1", "sku": None, "legacy_item_id": "1102233",
                                 "fulfillment_status": "NOT_STARTED"}]}]

    asyncio.run(ebay_cog._reconcile_ebay_order_statuses(bot, modified))

    cards = [m for m in bot.channel.sent if m.get("embed")]
    assert len(cards) == 1 and "$22.50" in cards[0]["embed"].fields[1].value
    assert fresh_db.has_seen_ebay_order("11-15276-35371")


# ---------------------------------------------------------- /ebay order-card

@pytest.fixture
def card_env(fresh_db, monkeypatch):
    fresh_db.set_shared_channel("ebay-orders", ORDERS)
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: ROLE)
    pallet_id = fresh_db.create_pallet("Pallets 1 -6", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, listing_id="1102233")
    order = {"order_id": "11-15276-35371", "sale_date": "2026-10-05", "total_price": 22.50,
             "line_items": [{"sku": "", "quantity": 1, "legacy_item_id": "1102233"}]}
    monkeypatch.setattr(ebay_cog.ebay_api, "get_order", AsyncMock(return_value=order))
    cog = ebay_cog.Ebay.__new__(ebay_cog.Ebay)
    cog.bot = _Bot()
    return cog, item


def _event():
    return SimpleNamespace(guild=object(), user=SimpleNamespace(id=1, roles=[ROLE]),
                           response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


def test_order_card_posts_the_card(fresh_db, card_env):
    cog, item = card_env
    event = _event()
    asyncio.run(ebay_cog.Ebay.order_card.callback(cog, event, " 11-15276-35371 "))

    assert len([m for m in cog.bot.channel.sent if m.get("embed")]) == 1
    assert "Posted the Confirm & log sale card" in event.followup.send.call_args.args[0]
    assert fresh_db.has_seen_ebay_order("11-15276-35371")


def test_order_card_refuses_an_already_logged_sale(fresh_db, card_env):
    cog, item = card_env
    sale_id = fresh_db.create_sale("eBay", "2026-10-05", 22.50, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item["id"], 22.50)
    event = _event()
    asyncio.run(ebay_cog.Ebay.order_card.callback(cog, event, "11-15276-35371"))
    assert "already has a logged sale" in event.followup.send.call_args.args[0]
    assert cog.bot.channel.sent == []


def test_order_card_reports_ebay_errors_and_no_match(fresh_db, card_env, monkeypatch):
    cog, _ = card_env
    monkeypatch.setattr(ebay_cog.ebay_api, "get_order", AsyncMock(side_effect=ebay_api.EbayAPIError("404 not found")))
    event = _event()
    asyncio.run(ebay_cog.Ebay.order_card.callback(cog, event, "bad"))
    assert "404 not found" in event.followup.send.call_args.args[0]

    monkeypatch.setattr(ebay_cog.ebay_api, "get_order", AsyncMock(return_value={
        "order_id": "x", "sale_date": "2026-10-05", "total_price": 5.0,
        "line_items": [{"sku": "someone-else", "quantity": 1}]}))
    event = _event()
    asyncio.run(ebay_cog.Ebay.order_card.callback(cog, event, "x"))
    assert "match a tracked item" in event.followup.send.call_args.args[0]


def test_order_card_needs_finance_role(fresh_db, card_env, monkeypatch):
    cog, _ = card_env
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: object())
    event = _event()
    asyncio.run(ebay_cog.Ebay.order_card.callback(cog, event, "11-15276-35371"))
    assert "Finance Management" in event.response.send_message.call_args.args[0]


# ------------------------------------------------------------ /finance sales

@pytest.fixture
def sales(fresh_db):
    a = fresh_db.create_pallet("Pallets 1 -6", category_id=1, created_by=1)
    b = fresh_db.create_pallet("Pallet B", category_id=2, created_by=1)
    i1, i2, i3 = (fresh_db.create_item(a, "x", [], 1), fresh_db.create_item(a, "y", [], 1),
                  fresh_db.create_item(b, "z", [], 1))
    yard = fresh_db.create_sale("In Person", "2026-10-04", 30.0, True, created_by=1)  # a bundle
    for item_id in (i1, i2):
        fresh_db.add_sale_item(yard, item_id, 15.0)
    fresh_db.set_sale_receipt_id(yard, "1003")
    ebay = fresh_db.create_sale("eBay", "2026-10-06", 16.04, False, created_by=1)  # QuickBooks failed
    fresh_db.add_sale_item(ebay, i3, 16.04)
    other = fresh_db.create_sale("Other: Mercari", "2026-10-07", 9.0, False, created_by=1)
    return fresh_db, a, yard, ebay, other


def test_sales_list_newest_first_with_quickbooks_state(sales):
    db, _, yard, ebay, other = sales
    text = format_sales_list(db.list_logged_sales())
    lines = text.splitlines()
    assert lines[1].startswith(f"`#{other}`") and lines[3].startswith(f"`#{yard}`")
    assert "Pallets 1 -6#1, Pallets 1 -6#2" in lines[3] and "Sales Receipt `1003`" in lines[3]
    assert f"retry-sale sale_id:{ebay}" in lines[2]


def test_sales_filters_by_pallet_and_platform_prefix(sales):
    db, pallet_a, yard, _, other = sales
    assert [s["id"] for s in db.list_logged_sales(pallet_id=pallet_a)] == [yard]
    assert [s["id"] for s in db.list_logged_sales(platform="in person")] == [yard]
    assert [s["id"] for s in db.list_logged_sales(platform="Other")] == [other]


def test_reversed_sale_still_listed_struck_through(sales):
    db, _, yard, _, _ = sales
    db.void_logged_sale(yard, "yard sale guess", actor_id=1)
    row = next(s for s in db.list_logged_sales() if s["id"] == yard)
    assert len(row["items"]) == 2 and row["voided"]["reason"] == "yard sale guess"
    text = format_sales_list([row])
    assert "~~" in text and "reversed: yard sale guess" in text


def test_long_lists_stay_under_discords_limit(sales):
    db = sales[0]
    pallet = db.create_pallet("Big", category_id=3, created_by=1)
    for n in range(60):
        item_id = db.create_item(pallet, "x", [], 1)
        sale_id = db.create_sale("In Person", "2026-10-08", 1.0, True, created_by=1)
        db.add_sale_item(sale_id, item_id, 1.0)
    assert len(format_sales_list(db.list_logged_sales(limit=50))) <= 1900


def test_sales_command_uses_the_channels_pallet(sales, monkeypatch):
    db, pallet_a, yard, _, _ = sales
    monkeypatch.setattr(finance_cog.runtime_settings, "resolve_role", lambda guild, name: ROLE)
    event = SimpleNamespace(guild=object(), user=SimpleNamespace(id=1, roles=[ROLE]),
                            channel=SimpleNamespace(category_id=1),
                            response=SimpleNamespace(send_message=AsyncMock()))
    asyncio.run(Finance.sales.callback(Finance.__new__(Finance), event))
    text = event.response.send_message.call_args.args[0]
    assert "for **Pallets 1 -6**" in text and f"`#{yard}`" in text and "Pallet B" not in text
