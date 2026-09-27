"""Offline integration of new upstream hold flow with website/public cards."""
import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

import cogs.item_flow as flow
import database as db
from website_contract import FOOTER_PREFIX


def fields(message):
    return {field.name: field.value for field in message.embeds[0].fields}


class Message:
    def __init__(self, message_id, kwargs):
        self.id = message_id
        self.embeds = kwargs.get("embeds", [])
        self.kwargs = kwargs
        self.deleted = False
        self.delete_error = None
        self.edited = []

    async def delete(self):
        if self.delete_error:
            raise self.delete_error
        self.deleted = True

    async def edit(self, **kwargs):
        self.edited.append(kwargs)
        if "embeds" in kwargs:
            self.embeds = kwargs["embeds"]


class Channel:
    def __init__(self, channel_id, clock):
        self.id = channel_id
        self.clock = clock
        self.messages = []
        self.send_error = None

    async def send(self, **kwargs):
        if self.send_error:
            raise self.send_error
        message = Message(next(self.clock), kwargs)
        self.messages.append(message)
        return message

    async def fetch_message(self, message_id):
        return next(message for message in self.messages if message.id == message_id)


@pytest.fixture
def lifecycle(fresh_db, tmp_path, monkeypatch):
    clock = itertools.count(10000)
    channels = {stage: Channel(index, clock) for index, stage in
                enumerate(("listed", "hold", "sold", "queue-review"), 1)}
    for stage, channel in channels.items():
        fresh_db.set_shared_channel(stage, channel.id)
    bot = SimpleNamespace(get_channel=lambda channel_id: next(
        (channel for channel in channels.values() if channel.id == channel_id), None))
    cog = flow.ItemFlow(bot)
    monkeypatch.setattr(cog, "_require_role", AsyncMock(return_value=True))
    monkeypatch.setattr(flow.finance_utils, "refresh_finance_message", AsyncMock())
    photo = tmp_path / "fixture.jpg"
    photo.write_bytes(b"offline-only-fixture")
    pallet_id = fresh_db.create_pallet("Synthetic pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Approved product description", [str(photo)], 1)
    fresh_db.save_ebay_listing_data(item_id, ebay_title="Approved product", category_id="123",
                                  condition_id="3000", price=19.99, item_specifics={})
    fresh_db.update_status(item_id, fresh_db.STATUS_LISTED, actor_id=1)
    old = asyncio.run(flow.send_item_card(channels["listed"], fresh_db.get_item(item_id)))
    fresh_db.update_status(item_id, fresh_db.STATUS_LISTED, new_message_id=old.id)
    return SimpleNamespace(db=fresh_db, cog=cog, channels=channels, item_id=item_id, old=old)


def interaction(message):
    return SimpleNamespace(message=message, user=SimpleNamespace(id=42),
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


def place_hold(case):
    item = case.db.get_item(case.item_id)
    item.update(ai_flags="PRIVATE STAFF FLAGS", recipient_name="PRIVATE BUYER")
    return asyncio.run(case.cog.place_item_on_hold(
        item, reason=db.HOLD_REASON_OTHER, note="PRIVATE HOLD REASON", actor_id=87654))


def test_listed_hold_release_sale_shipment_keep_public_contract_order(lifecycle):
    case = lifecycle
    assert fields(case.old)["Website hold"] == "false"
    assert place_hold(case)
    notice = case.channels["listed"].messages[-1]
    held = case.channels["hold"].messages[-1]
    assert case.old.id < notice.id < held.id
    assert case.old.deleted
    assert not notice.deleted
    assert notice.kwargs["allowed_mentions"].everyone is False
    assert fields(notice) == {
        "Website item ID": str(case.item_id), "Website SKU": "PALLET-1-ITEM-1",
        "Website status": "listed", "Website hold": "true", "Website issue": "none",
    }
    assert "PRIVATE" not in json.dumps(notice.embeds[0].to_dict())
    assert "87654" not in json.dumps(notice.embeds[0].to_dict())
    assert "files" not in notice.kwargs
    assert "Website status" not in fields(held)
    assert not (held.embeds[0].footer.text or "").startswith(FOOTER_PREFIX)
    assert case.db.get_item(case.item_id)["status"] == db.STATUS_ON_HOLD

    result = asyncio.run(case.cog.resolve_item_hold(case.db.get_item(case.item_id), actor_id=42))
    assert result == (True, "listed")
    released = case.channels["listed"].messages[-1]
    assert released.id > notice.id
    assert fields(released)["Website status"] == "listed"
    assert fields(released)["Website hold"] == "false"
    assert fields(released)["Website price cents"] == "1999"
    assert held.deleted
    assert not notice.deleted  # retained for cold reconciliation of stale older cards
    assert case.db.get_open_hold(case.item_id) is None

    asyncio.run(case.cog.move_to_sold(interaction(released), case.item_id))
    sold = case.channels["sold"].messages[-1]
    assert sold.id > released.id
    assert fields(sold)["Website status"] == "sold"
    assert released.deleted
    asyncio.run(case.cog.mark_shipped(interaction(sold), case.item_id))
    assert fields(sold)["Website status"] == "shipped"
    assert case.db.get_item(case.item_id)["status"] == db.STATUS_SHIPPED


def test_hold_notice_survives_failed_old_listing_deletion(lifecycle):
    lifecycle.old.delete_error = aiohttp.ClientError("Discord refused deletion")
    assert place_hold(lifecycle)
    assert not lifecycle.old.deleted
    assert lifecycle.old.edited[-1]["view"] is None
    notice = lifecycle.channels["listed"].messages[-1]
    assert notice.id > lifecycle.old.id
    assert fields(notice)["Website hold"] == "true"
    assert not notice.deleted


def test_failed_withdrawal_aborts_hold_without_database_or_channel_changes(lifecycle):
    lifecycle.channels["listed"].send_error = aiohttp.ClientError("Discord unavailable")
    with pytest.raises(aiohttp.ClientError):
        place_hold(lifecycle)
    assert lifecycle.db.get_item(lifecycle.item_id)["status"] == db.STATUS_LISTED
    assert lifecycle.db.get_open_hold(lifecycle.item_id) is None
    assert not lifecycle.channels["hold"].messages
    assert not lifecycle.old.deleted


def test_failed_hold_card_leaves_fail_closed_public_notice(lifecycle):
    lifecycle.channels["hold"].send_error = aiohttp.ClientError("Discord unavailable")
    with pytest.raises(aiohttp.ClientError):
        place_hold(lifecycle)
    assert fields(lifecycle.channels["listed"].messages[-1])["Website hold"] == "true"
    assert lifecycle.db.get_open_hold(lifecycle.item_id) is None
    lifecycle.channels["hold"].send_error = None
    assert place_hold(lifecycle)  # safe retry then normal resolution is possible


def test_failed_resolution_preserves_open_hold_for_retry(lifecycle):
    assert place_hold(lifecycle)
    lifecycle.channels["listed"].send_error = aiohttp.ClientError("Discord unavailable")
    with pytest.raises(aiohttp.ClientError):
        asyncio.run(lifecycle.cog.resolve_item_hold(lifecycle.db.get_item(lifecycle.item_id), actor_id=42))
    assert lifecycle.db.get_open_hold(lifecycle.item_id) is not None
    assert lifecycle.db.get_item(lifecycle.item_id)["status"] == db.STATUS_ON_HOLD
    assert not lifecycle.channels["hold"].messages[-1].deleted
    lifecycle.channels["listed"].send_error = None
    assert asyncio.run(lifecycle.cog.resolve_item_hold(lifecycle.db.get_item(lifecycle.item_id), actor_id=42)) == (True, "listed")


def test_sold_hold_and_resolution_stay_sold(lifecycle):
    case = lifecycle
    asyncio.run(case.cog.move_to_sold(interaction(case.old), case.item_id))
    assert place_hold(case)
    assert len(case.channels["listed"].messages) == 1  # no new listed/held event for sold
    assert asyncio.run(case.cog.resolve_item_hold(case.db.get_item(case.item_id), actor_id=42)) == (True, "sold")
    assert fields(case.channels["sold"].messages[-1])["Website status"] == "sold"


def test_stale_rereview_does_not_republish_listed_item(lifecycle, monkeypatch):
    monkeypatch.setattr(flow.config, "AI_ENABLED", True)
    action = interaction(lifecycle.old)
    asyncio.run(lifecycle.cog.rereview_item(action, lifecycle.item_id))
    assert "already moved on" in action.response.send_message.call_args.args[0]
    assert not lifecycle.old.deleted
    assert not lifecycle.channels["queue-review"].messages
    assert len(lifecycle.channels["listed"].messages) == 1
