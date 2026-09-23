"""
ItemFlow.add_to_fb_marketplace_batch and confirm_fb_marketplace_pending_item
(cogs/item_flow.py) - the FB Marketplace counterpart to add_to_ebay_batch/
confirm_ebay_pending_item. Uses minimal fake Discord objects, matching this
codebase's existing preference for DB-level tests over full Discord mocking
(see test_item_duplicate.py).
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import cogs.item_flow as item_flow_module
import fb_marketplace_csv
from cogs.item_flow import ItemFlow


class _FakeMessage:
    def __init__(self, msg_id=1000):
        self.id = msg_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.sent = []
        self._next_id = 2000
        self.messages = {}

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class _FakeBot:
    def __init__(self, channels: dict):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeRole:
    pass


class _FakeUser:
    def __init__(self, role):
        self.id = 42
        self.roles = [role]


class _FakeResponse:
    def __init__(self):
        self.content = None

    async def send_message(self, content=None, **kwargs):
        self.content = content


class _FakeInteraction:
    def __init__(self, message, role):
        self.response = _FakeResponse()
        self.message = message
        self.user = _FakeUser(role)
        self.guild = object()


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()

    def fake_resolve_role(guild, role_name):
        return role

    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", fake_resolve_role)
    return role


@pytest.fixture
def awaiting_item(fresh_db):
    pallet_id = fresh_db.create_pallet("FB Test Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "cordless drill, new in box", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, ebay_title="DeWalt Cordless Drill", category_id="12345", condition_id="1500",
        price=45.0, item_specifics={}, weight_lb=4.0, length_in=10, width_in=8, height_in=4,
    )
    fresh_db.update_status(item_id, fresh_db.STATUS_AWAITING_LISTING, actor_id=1)
    return fresh_db.get_item(item_id)


@pytest.fixture
def pending_channel():
    return _FakeChannel(channel_id=555)


def _setup(fresh_db, pallet_id, pending_channel):
    fresh_db.set_shared_channel("pending-fb-marketplace-upload", 555)


# --------------------------------------------------------- add to batch


def test_add_to_fb_marketplace_batch_appends_csv_row_and_moves_status(
    fresh_db, awaiting_item, pending_channel, bypass_role_check, tmp_path, monkeypatch
):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    _setup(fresh_db, awaiting_item["pallet_id"], pending_channel)

    bot = _FakeBot({555: pending_channel})
    cog = ItemFlow(bot)

    message = _FakeMessage()
    interaction = _FakeInteraction(message, bypass_role_check)
    asyncio.run(cog.add_to_fb_marketplace_batch(interaction, awaiting_item["id"]))

    rows = fb_marketplace_csv._read_existing_rows()
    assert len(rows) == 1
    assert rows[0]["Title"] == "DeWalt Cordless Drill"
    assert rows[0]["Price"] == "45"

    updated = fresh_db.get_item(awaiting_item["id"])
    assert updated["status"] == fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD
    assert len(pending_channel.sent) == 1
    assert message.deleted is True


def test_add_to_fb_marketplace_batch_rejects_item_not_awaiting_listing(
    fresh_db, awaiting_item, pending_channel, bypass_role_check, tmp_path, monkeypatch
):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    fresh_db.update_status(awaiting_item["id"], fresh_db.STATUS_LISTED, actor_id=1)

    bot = _FakeBot({555: pending_channel})
    cog = ItemFlow(bot)
    interaction = _FakeInteraction(_FakeMessage(), bypass_role_check)
    asyncio.run(cog.add_to_fb_marketplace_batch(interaction, awaiting_item["id"]))

    assert "already moved on" in interaction.response.content
    assert pending_channel.sent == []


def test_add_to_fb_marketplace_batch_requires_listing_data(fresh_db, bypass_role_check, pending_channel):
    pallet_id = fresh_db.create_pallet("No Listing Data Pallet", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_AWAITING_LISTING, actor_id=1)

    bot = _FakeBot({555: pending_channel})
    cog = ItemFlow(bot)
    interaction = _FakeInteraction(_FakeMessage(), bypass_role_check)
    asyncio.run(cog.add_to_fb_marketplace_batch(interaction, item_id))

    assert "No listing data was captured" in interaction.response.content
    assert pending_channel.sent == []


# --------------------------------------------------------- confirm listed


def test_confirm_fb_marketplace_pending_item_moves_to_listed(fresh_db, awaiting_item, pending_channel):
    fresh_db.set_shared_channel("pending-fb-marketplace-upload", 555)
    listed_channel = _FakeChannel(channel_id=777)
    fresh_db.set_shared_channel("listed", 777)

    pending_msg = asyncio.run(pending_channel.send(content="pending card"))
    fresh_db.update_status(
        awaiting_item["id"], fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD, actor_id=1, new_message_id=pending_msg.id
    )
    item = fresh_db.get_item(awaiting_item["id"])

    bot = _FakeBot({555: pending_channel, 777: listed_channel})
    cog = ItemFlow(bot)

    moved = asyncio.run(cog.confirm_fb_marketplace_pending_item(item, actor_id=1))

    assert moved is True
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED
    assert pending_msg.deleted is True
    assert len(listed_channel.sent) == 1


def test_confirm_fb_marketplace_pending_item_no_op_when_not_pending(fresh_db, awaiting_item):
    # awaiting_item is still at awaiting_listing, not pending_fb_marketplace_upload
    bot = _FakeBot({})
    cog = ItemFlow(bot)
    moved = asyncio.run(cog.confirm_fb_marketplace_pending_item(awaiting_item, actor_id=1))
    assert moved is False
