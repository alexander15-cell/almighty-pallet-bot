"""Selling an item anywhere but eBay, or putting it on hold, takes its eBay
listing down; resolving the hold puts it back up (cogs/item_flow.py)."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import ebay_api
import cogs.item_flow as item_flow
from cogs.item_flow import ItemFlow

ORDERS = 701


class _Channel:
    def __init__(self, cid):
        self.id, self.sent = cid, []

    async def send(self, text=None, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(id=900 + len(self.sent))


class _Bot:
    def __init__(self):
        self.channels = {}

    def get_channel(self, cid):
        return self.channels.setdefault(cid, _Channel(cid))

    def get_cog(self, name):
        return None


@pytest.fixture
def env(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    monkeypatch.setattr("finance_utils.refresh_finance_message", AsyncMock())
    fresh_db.set_shared_channel("ebay-orders", ORDERS)
    pallet_id = fresh_db.create_pallet("Pallets 1-6", category_id=1, created_by=1)
    ended = AsyncMock(return_value="eBay listing ended.")
    monkeypatch.setattr(ebay_api, "end_listing_for_item", ended)
    bot = _Bot()
    return SimpleNamespace(db=fresh_db, pallet_id=pallet_id, ended=ended, bot=bot, flow=ItemFlow(bot))


def _listed(env, listing_id="860000000001", units=1):
    item_id = env.db.create_item(env.pallet_id, "lamp", [], 1)
    env.db.save_ebay_listing_data(item_id, "Lamp", "1", "1500", 20.0, {})
    env.db.set_ebay_item_id(item_id, listing_id)
    env.db.update_status(item_id, env.db.STATUS_LISTED, actor_id=1)
    item = env.db.get_item(item_id)
    env.db.record_active_snapshot([{"sku": f"pallet-{env.pallet_id}-item-{item['item_number']}",
                                    "listing_id": listing_id, "price": 20.0, "quantity": units,
                                    "listing_type": "FixedPriceItem"}], "2026-10-22T00:00:00+00:00")
    return item


def _notices(env):
    return env.bot.get_channel(ORDERS).sent


def test_in_person_sale_ends_the_ebay_listing(env, monkeypatch):
    item = _listed(env)
    monkeypatch.setattr(item_flow, "send_item_card", AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(env.flow, "_old_card", AsyncMock(return_value=None))
    monkeypatch.setattr(env.flow, "_post_to_accounting_channel", AsyncMock())
    asyncio.run(env.flow.do_mark_sold(item["id"], "In Person", actor_id=1))
    env.ended.assert_awaited_once()
    assert env.ended.await_args.kwargs["listing_id"] == "860000000001"
    assert "sold (In Person)" in _notices(env)[0] and "eBay listing ended" in _notices(env)[0]
    assert any("eBay: eBay listing ended" in (e["note"] or "") for e in env.db.get_item_events(item["id"]))


@pytest.mark.parametrize("platform", ["eBay", "Website"])
def test_ebay_and_website_sales_dont_end_it_here(env, monkeypatch, platform):
    item = _listed(env)
    monkeypatch.setattr(item_flow, "send_item_card", AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(env.flow, "_old_card", AsyncMock(return_value=None))
    monkeypatch.setattr(env.flow, "_post_to_accounting_channel", AsyncMock())
    asyncio.run(env.flow.do_mark_sold(item["id"], platform, actor_id=1))
    env.ended.assert_not_awaited()


def test_multi_unit_listing_is_left_alone_with_a_warning(env):
    item = _listed(env, units=17)
    note = asyncio.run(env.flow.end_ebay_listing_elsewhere(item, "sold (In Person)"))
    env.ended.assert_not_awaited()
    assert "17 units" in note and "lower its quantity" in _notices(env)[0]


def test_failure_is_loud(env):
    item = _listed(env)
    env.ended.side_effect = ebay_api.EbayAPIError("eBay down")
    note = asyncio.run(env.flow.end_ebay_listing_elsewhere(item, "sold (In Person)"))
    assert "end it in Seller Hub NOW" in note and "NOW" in _notices(env)[0]


def test_no_ebay_listing_means_nothing_to_do(env):
    item_id = env.db.create_item(env.pallet_id, "lamp", [], 1)
    assert asyncio.run(env.flow.end_ebay_listing_elsewhere(env.db.get_item(item_id), "sold")) == ""
    env.ended.assert_not_awaited() and _notices(env) == []


def test_relist_after_hold_records_the_new_listing_id(env, monkeypatch):
    item = _listed(env, listing_id="860000000009")
    env.db.record_hold_ebay_end(item["id"], "860000000009")
    relist = AsyncMock(return_value="860000000777")
    monkeypatch.setattr(ebay_api, "relist_listing_for_item", relist)

    assert "relisted on eBay as 860000000777" in asyncio.run(env.flow.relist_after_hold(item))
    assert relist.await_args.kwargs["listing_id"] == "860000000009"
    assert env.db.get_ebay_listing_data(item["id"])["ebay_item_id"] == "860000000777"
    assert env.db.get_hold_ebay_end(item["id"]) is None
    assert asyncio.run(env.flow.relist_after_hold(item)) == ""  # only once


def test_relist_failure_says_to_relist_by_hand(env, monkeypatch):
    item = _listed(env)
    env.db.record_hold_ebay_end(item["id"], "860000000001")
    monkeypatch.setattr(ebay_api, "relist_listing_for_item", AsyncMock(side_effect=ebay_api.EbayAPIError("nope")))
    assert "relist it in Seller Hub" in asyncio.run(env.flow.relist_after_hold(item))
    assert env.db.get_hold_ebay_end(item["id"]) is not None  # still owed a relist
