"""
/item move-back (cogs/admin_tools.py) and the machinery behind it -
database.unwind_item_downstream_data, ItemFlow.admin_move_item_to_stage,
MoveBackStageSelect/MoveBackStageView (cogs/item_flow.py). The admin fix for
a wrong click (e.g. the wrong item marked Sold): force-moves an item to a
different stage, reversing a logged sale and clearing a captured eBay
listing as needed, rather than leaving stale sold/listed data behind.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import ai_review
import cogs.admin_tools as admin_tools_module
import cogs.item_flow as item_flow_module
import config
import database as db
from cogs.admin_tools import AdminTools
from cogs.item_flow import ItemFlow, MoveBackStageSelect, MoveBackStageView


class _FakeMessage:
    def __init__(self, msg_id=1000):
        self.id = msg_id
        self.deleted = False
        self.edited_view = "unset"

    async def delete(self):
        self.deleted = True

    async def edit(self, view=None, **kwargs):
        self.edited_view = view


class _FakeChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.sent = []
        self._next_id = 2000
        self.messages = {}

    async def send(self, *args, **kwargs):
        content = args[0] if args else kwargs.get("content")
        self.sent.append(content if content is not None else kwargs)
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class _FakeBot:
    def __init__(self, channels: dict, item_flow_cog=None):
        self._channels = channels
        self._item_flow_cog = item_flow_cog

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_cog(self, name):
        return self._item_flow_cog


class _FakeRole:
    pass


class _FakeUser:
    def __init__(self, role, user_id=42):
        self.id = user_id
        self.roles = [role]


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.view = None

    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, content=None, **kwargs):
        self.content = content
        self.view = kwargs.get("view")


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


class _FakeInteraction:
    def __init__(self, client=None, message=None, role=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = message
        self.user = _FakeUser(role)
        self.guild = object()
        self.client = client


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()
    monkeypatch.setattr(admin_tools_module, "_is_pallet_admin", lambda interaction: True)
    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", lambda guild, name: role)
    return role


@pytest.fixture
def sold_item(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("Move Back Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.save_ebay_listing_data(item_id, "Widget", "123", "1000", 9.99, {})
    fresh_db.update_status(item_id, fresh_db.STATUS_LISTED, actor_id=1, new_message_id=1)
    fresh_db.record_item_sale(item_id, 9.99, "eBay", actor_id=1)
    fresh_db.update_status(item_id, fresh_db.STATUS_SOLD, actor_id=1, new_message_id=5555)
    return fresh_db.get_item(item_id)


# -------------------------------------------------------- database layer --


def test_unwind_reverses_sale_and_clears_listing_data_moving_before_awaiting_listing(fresh_db, sold_item):
    result = db.unwind_item_downstream_data(sold_item["id"], db.STATUS_QUEUE_REVIEW, actor_id=1)

    assert result == {"reversed_sale_amount": 9.99, "cleared_listing_data": True}
    item = fresh_db.get_item(sold_item["id"])
    assert item["sale_price"] is None
    assert item["sold_at"] is None
    assert item["listed_at"] is None
    assert fresh_db.get_ebay_listing_data(sold_item["id"]) is None


def test_unwind_keeps_listing_data_moving_only_to_awaiting_listing(fresh_db, sold_item):
    result = db.unwind_item_downstream_data(sold_item["id"], db.STATUS_AWAITING_LISTING, actor_id=1)

    assert result["cleared_listing_data"] is False
    assert fresh_db.get_ebay_listing_data(sold_item["id"]) is not None


def test_unwind_moving_to_sold_does_not_reverse_itself(fresh_db, sold_item):
    result = db.unwind_item_downstream_data(sold_item["id"], db.STATUS_SOLD, actor_id=1)

    assert result["reversed_sale_amount"] is None
    item = fresh_db.get_item(sold_item["id"])
    assert item["sale_price"] == 9.99


def test_unwind_refuses_when_sale_already_logged_to_quickbooks(fresh_db, sold_item):
    sale_id = fresh_db.create_sale("eBay", "2026-01-01", 9.99, False, created_by=1)
    fresh_db.add_sale_item(sale_id, sold_item["id"], 9.99)

    with pytest.raises(ValueError, match="already logged to QuickBooks"):
        db.unwind_item_downstream_data(sold_item["id"], db.STATUS_QUEUE_REVIEW, actor_id=1)


# -------------------------------------------------------------- cog layer --


def test_admin_move_sold_item_back_to_queue_review(fresh_db, sold_item, bypass_role_check):
    sold_channel = _FakeChannel(10)
    sold_channel.messages[5555] = _FakeMessage(5555)
    queue_channel = _FakeChannel(20)
    fresh_db.set_shared_channel("sold", 10)
    fresh_db.set_shared_channel("queue-review", 20)
    cog = ItemFlow(_FakeBot({10: sold_channel, 20: queue_channel}))
    client = _FakeBot({10: sold_channel, 20: queue_channel}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, role=bypass_role_check)

    asyncio.run(cog.admin_move_item_to_stage(interaction, sold_item["id"], db.STATUS_QUEUE_REVIEW))

    item = fresh_db.get_item(sold_item["id"])
    assert item["status"] == db.STATUS_QUEUE_REVIEW
    assert item["sale_price"] is None
    assert fresh_db.get_ebay_listing_data(sold_item["id"]) is None
    assert sold_channel.messages[5555].deleted is True
    assert len(queue_channel.sent) == 1
    assert "reversed its $9.99 sale" in interaction.followup.content
    assert "cleared its captured eBay listing details" in interaction.followup.content


def test_admin_move_item_to_data_entry_posts_resubmit_card(fresh_db, sold_item, bypass_role_check):
    sold_channel = _FakeChannel(10)
    sold_channel.messages[5555] = _FakeMessage(5555)
    data_entry_channel = _FakeChannel(30)
    fresh_db.set_shared_channel("sold", 10)
    fresh_db.map_channel(sold_item["pallet_id"], "data-entry", 30)
    cog = ItemFlow(_FakeBot({10: sold_channel, 30: data_entry_channel}))
    client = _FakeBot({10: sold_channel, 30: data_entry_channel}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, role=bypass_role_check)

    asyncio.run(cog.admin_move_item_to_stage(interaction, sold_item["id"], db.STATUS_DATA_ENTRY))

    item = fresh_db.get_item(sold_item["id"])
    assert item["status"] == db.STATUS_REJECTED
    assert len(data_entry_channel.sent) == 1
    embed = data_entry_channel.sent[0]["embeds"][0]
    assert "REPLY to this message" in embed.footer.text


def test_admin_move_item_to_automated_review_reruns_ai(fresh_db, sold_item, bypass_role_check, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)

    async def fake_review_item(photo_paths, raw_description):
        return {
            "suggested_title": "Re-reviewed Widget", "suggested_description": "desc", "flags": [],
            "confidence": "high",
        }
    monkeypatch.setattr(ai_review, "review_item", fake_review_item)

    sold_channel = _FakeChannel(10)
    sold_channel.messages[5555] = _FakeMessage(5555)
    automated_review_channel = _FakeChannel(40)
    queue_channel = _FakeChannel(20)
    fresh_db.set_shared_channel("sold", 10)
    fresh_db.set_shared_channel("automated-review", 40)
    fresh_db.set_shared_channel("queue-review", 20)
    cog = ItemFlow(_FakeBot({10: sold_channel, 40: automated_review_channel, 20: queue_channel}))
    client = _FakeBot({10: sold_channel, 40: automated_review_channel, 20: queue_channel}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, role=bypass_role_check)

    asyncio.run(cog.admin_move_item_to_stage(interaction, sold_item["id"], db.STATUS_AUTOMATED_REVIEW))

    item = fresh_db.get_item(sold_item["id"])
    assert item["status"] == db.STATUS_QUEUE_REVIEW
    assert item["ai_title"] == "Re-reviewed Widget"


def test_admin_move_refuses_when_sale_already_logged(fresh_db, sold_item, bypass_role_check):
    sale_id = fresh_db.create_sale("eBay", "2026-01-01", 9.99, False, created_by=1)
    fresh_db.add_sale_item(sale_id, sold_item["id"], 9.99)
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, role=bypass_role_check)

    asyncio.run(cog.admin_move_item_to_stage(interaction, sold_item["id"], db.STATUS_QUEUE_REVIEW))

    assert "already logged to QuickBooks" in interaction.followup.content
    item = fresh_db.get_item(sold_item["id"])
    assert item["status"] == db.STATUS_SOLD


def test_move_back_stage_select_excludes_current_status():
    select = MoveBackStageSelect(item_id=1, current_status=db.STATUS_QUEUE_REVIEW)
    values = [opt.value for opt in select.options]
    assert db.STATUS_QUEUE_REVIEW not in values
    assert db.STATUS_LISTED in values


# -------------------------------------------------------- slash command --


def test_slash_command_rejects_unknown_pallet(fresh_db, bypass_role_check):
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(role=bypass_role_check)

    asyncio.run(AdminTools.item_move_back.callback(cog, interaction, "No Such Pallet", 1))

    assert "No pallet named" in interaction.response.content


def test_slash_command_rejects_on_hold_item(fresh_db, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Hold Pallet", category_id=5, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_ON_HOLD, actor_id=1)
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(role=bypass_role_check)

    asyncio.run(AdminTools.item_move_back.callback(cog, interaction, "Hold Pallet", 1))

    assert "on hold" in interaction.response.content


def test_slash_command_shows_stage_picker_for_a_valid_item(fresh_db, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Valid Pallet", category_id=6, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_SOLD, actor_id=1)
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(role=bypass_role_check)

    asyncio.run(AdminTools.item_move_back.callback(cog, interaction, "Valid Pallet", 1))

    assert isinstance(interaction.response.view, MoveBackStageView)
