"""eBay -> bot price sync (price_sync.py, ebay_api.get_live_listing_price)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import config
import ebay_api
import price_sync
from cogs.item_flow import PRICE_FIELD


@pytest.fixture
def env(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    monkeypatch.setattr("finance_utils.refresh_finance_message", AsyncMock())
    return fresh_db


def _listed(db, price, listing_id, fmt="FixedPrice", status=None, pallet="P"):
    pallet_id = db.create_pallet(pallet + listing_id, category_id=int(listing_id[-3:]), created_by=1)
    item_id = db.create_item(pallet_id, "lamp", [], 1)
    db.save_ebay_listing_data(item_id, "Lamp", "123", "1500", price, {}, listing_format=fmt)
    db.set_ebay_item_id(item_id, listing_id)
    db.update_status(item_id, status or db.STATUS_LISTED, actor_id=1)
    return db.get_item(item_id)


class _Message:
    def __init__(self, fields):
        embed = discord.Embed(title="Lamp")
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=True)
        self.embeds = [embed]
        self.edited = None

    async def edit(self, embeds=None):
        self.edited = embeds


def _bot(message):
    flow = SimpleNamespace(_fetch_current_card=AsyncMock(return_value=message))
    return SimpleNamespace(get_cog=lambda name: flow if name == "ItemFlow" else None)


def test_changed_price_updates_the_bot_and_the_card(env, monkeypatch):
    item = _listed(env, 24.99, "110000000101")
    same = _listed(env, 10.00, "110000000102")
    prices = {"110000000101": {"price": 19.99, "currency": "USD"}, "110000000102": {"price": 10.0, "currency": "USD"}}
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(side_effect=lambda lid: prices[lid]))
    message = _Message([("📦 Pallet", "P"), ("Item #", "1"), ("Status", "listed"), ("Website price cents", "2499")])

    result = asyncio.run(price_sync.sync_prices(_bot(message)))

    assert result["checked"] == 2 and [(c[0]["id"], c[1], c[2]) for c in result["changed"]] == [(item["id"], 24.99, 19.99)]
    assert env.get_ebay_listing_data(item["id"])["price"] == 19.99
    assert env.get_ebay_listing_data(same["id"])["price"] == 10.0
    fields = {f.name: f.value for f in message.edited[0].fields}
    assert fields[PRICE_FIELD] == "$19.99" and fields["Website price cents"] == "1999"
    assert [f.name for f in message.edited[0].fields][3] == PRICE_FIELD  # right after Status
    assert "Price synced from eBay: $24.99 -> $19.99" in [e["note"] for e in env.get_item_events(item["id"])]


def test_existing_price_field_is_replaced_not_duplicated(env, monkeypatch):
    _listed(env, 24.99, "110000000201")
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(return_value={"price": 30.0, "currency": "USD"}))
    message = _Message([("Status", "listed"), (PRICE_FIELD, "$24.99")])
    asyncio.run(price_sync.sync_prices(_bot(message)))
    names = [f.name for f in message.edited[0].fields]
    assert names.count(PRICE_FIELD) == 1 and message.edited[0].fields[1].value == "$30.00"


def test_auctions_ended_listings_errors_and_non_listed_items_are_left_alone(env, monkeypatch):
    auction = _listed(env, 5.0, "110000000301", fmt="Auction")
    ended = _listed(env, 12.0, "110000000302")
    broken = _listed(env, 8.0, "110000000303")
    sold = _listed(env, 9.0, "110000000304", status=env.STATUS_SOLD)

    async def live(lid):
        if lid == "110000000303":
            raise ebay_api.EbayAPIError("eBay down")
        return None if lid == "110000000302" else {"price": 99.0, "currency": "USD"}
    monkeypatch.setattr(ebay_api, "get_live_listing_price", live)

    result = asyncio.run(price_sync.sync_prices(_bot(None)))

    assert result["checked"] == 2  # the auction and the sold item aren't even checked
    assert result["changed"] == [] and len(result["errors"]) == 1
    for item, price in ((auction, 5.0), (ended, 12.0), (broken, 8.0), (sold, 9.0)):
        assert env.get_ebay_listing_data(item["id"])["price"] == price


def test_live_price_reader(monkeypatch):
    monkeypatch.setattr(ebay_api, "get_app_access_token", AsyncMock(return_value="t"))
    replies = {}

    class _Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body

        async def json(self, content_type=None):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, headers=None):
            replies["url"] = url
            return _Resp(*replies["next"])
    monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _Session())

    replies["next"] = (200, {"price": {"value": "19.99", "currency": "USD"}, "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]})
    assert asyncio.run(ebay_api.get_live_listing_price("110000000101")) == {"price": 19.99, "currency": "USD"}
    assert "get_item_by_legacy_id?legacy_item_id=110000000101" in replies["url"]
    replies["next"] = (200, {"currentBidPrice": {"value": "3.00"}, "buyingOptions": ["AUCTION"]})
    assert asyncio.run(ebay_api.get_live_listing_price("1")) is None
    replies["next"] = (404, {"errors": [{"errorId": 11001}]})
    assert asyncio.run(ebay_api.get_live_listing_price("1")) is None
