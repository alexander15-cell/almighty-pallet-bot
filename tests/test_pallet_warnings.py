"""
finance_utils._post_pallet_warnings - the auto-flag early warnings (cost
basis exceeding the manifest retail value estimate, and low margin once a
pallet is fully sold out). Called directly with fresh_db-built pallets/fin
dicts rather than routing through refresh_finance_message's Discord message
fetching, since the warning logic itself is what's under test here.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import config
import finance_utils


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content):
        self.sent.append(content)


def test_no_warning_when_cost_under_manifest_estimate_and_not_sold_out(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 10.0, actor_id=1)
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.save_ai_review(item, "Widget", "desc", "[]", suggested_price=50.0)

    pallet = fresh_db.get_pallet(pallet_id)
    fin = fresh_db.get_pallet_financials(pallet_id)
    channel = _FakeChannel()
    asyncio.run(finance_utils._post_pallet_warnings(channel, pallet, fin))

    assert channel.sent == []


def test_warns_when_cost_exceeds_manifest_estimate(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 100.0, actor_id=1)
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.save_ai_review(item, "Widget", "desc", "[]", suggested_price=20.0)

    pallet = fresh_db.get_pallet(pallet_id)
    fin = fresh_db.get_pallet_financials(pallet_id)
    channel = _FakeChannel()
    asyncio.run(finance_utils._post_pallet_warnings(channel, pallet, fin))

    assert len(channel.sent) == 1
    assert "exceeded" in channel.sent[0]


def test_no_manifest_warning_when_no_ai_prices_estimated_yet(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 100.0, actor_id=1)
    fresh_db.create_item(pallet_id, "widget", [], 1)  # no AI review saved - estimate stays 0

    pallet = fresh_db.get_pallet(pallet_id)
    fin = fresh_db.get_pallet_financials(pallet_id)
    channel = _FakeChannel()
    asyncio.run(finance_utils._post_pallet_warnings(channel, pallet, fin))

    assert channel.sent == []  # a 0 estimate isn't a real estimate to compare against


def test_warns_on_low_margin_once_fully_sold_out(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT", 20.0)
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 90.0, actor_id=1)
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.record_item_sale(item, 100.0, "eBay", actor_id=1)  # 10% margin - below 20% threshold
    fresh_db.update_status(item, fresh_db.STATUS_SOLD, actor_id=1)

    pallet = fresh_db.get_pallet(pallet_id)
    fin = fresh_db.get_pallet_financials(pallet_id)
    channel = _FakeChannel()
    asyncio.run(finance_utils._post_pallet_warnings(channel, pallet, fin))

    assert len(channel.sent) == 1
    assert "fully sold out" in channel.sent[0]
    assert "margin" in channel.sent[0]


def test_no_low_margin_warning_when_not_fully_sold_out(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT", 20.0)
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 90.0, actor_id=1)
    item1 = fresh_db.create_item(pallet_id, "widget 1", [], 1)
    fresh_db.record_item_sale(item1, 100.0, "eBay", actor_id=1)
    fresh_db.update_status(item1, fresh_db.STATUS_SOLD, actor_id=1)
    fresh_db.create_item(pallet_id, "widget 2 still listed", [], 1)  # not sold yet

    pallet = fresh_db.get_pallet(pallet_id)
    fin = fresh_db.get_pallet_financials(pallet_id)
    channel = _FakeChannel()
    asyncio.run(finance_utils._post_pallet_warnings(channel, pallet, fin))

    assert channel.sent == []


def test_no_low_margin_warning_when_margin_healthy(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT", 20.0)
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 10.0, actor_id=1)
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.record_item_sale(item, 100.0, "eBay", actor_id=1)  # 90% margin
    fresh_db.update_status(item, fresh_db.STATUS_SOLD, actor_id=1)

    pallet = fresh_db.get_pallet(pallet_id)
    fin = fresh_db.get_pallet_financials(pallet_id)
    channel = _FakeChannel()
    asyncio.run(finance_utils._post_pallet_warnings(channel, pallet, fin))

    assert channel.sent == []
