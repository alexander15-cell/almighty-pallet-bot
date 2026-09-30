"""
The platform picker shown right after Mark as Sold (cogs/item_flow.py) - a
plain button click can't collect a dropdown value, so clicking Mark as Sold
now shows a Select (eBay/Facebook Marketplace/In Person/Other) before the
item actually moves to sold; picking a platform (or submitting the Other
modal for free text) hands off to finish_move_to_sold, which does the real
transition. This matters for /finance log-sale's sales-tax handling
downstream - eBay already collects/remits tax, the others don't.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import cogs.item_flow as item_flow_module
from cogs.item_flow import ItemFlow, SalePlatformSelectView, OtherSalePlatformModal


class _FakeMessage:
    def __init__(self, msg_id=1000):
        self.id = msg_id
        self.deleted = False
        self.edited_with = None

    async def delete(self):
        self.deleted = True

    async def edit(self, **kwargs):
        self.edited_with = kwargs


class _FakeChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return _FakeMessage(9999)


class _FakeBot:
    def __init__(self, channels: dict):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeRole:
    pass


class _FakeUser:
    def __init__(self, role, user_id=42):
        self.id = user_id
        self.roles = [role]


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.edited_content = None
        self.edited_view = None
        self.deferred = False
        self.modal = None

    async def defer(self, ephemeral=True, thinking=True):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.content = content

    async def edit_message(self, content=None, view=None, **kwargs):
        self.edited_content = content
        self.edited_view = view

    async def send_modal(self, modal):
        self.modal = modal


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


class _FakeInteraction:
    def __init__(self, client, message=None, role=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = message
        self.user = _FakeUser(role)
        self.guild = object()
        self.client = client


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()
    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", lambda guild, name: role)
    return role


@pytest.fixture
def listed_item(fresh_db):
    pallet_id = fresh_db.create_pallet("Sale Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_LISTED, actor_id=1)
    return fresh_db.get_item(item_id)


def test_move_to_sold_shows_a_platform_picker_instead_of_moving_immediately(fresh_db, listed_item, bypass_role_check):
    bot = _FakeBot({})
    cog = ItemFlow(bot)
    interaction = _FakeInteraction(bot, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(cog.move_to_sold(interaction, listed_item["id"]))

    assert isinstance(interaction.response.edited_view, SalePlatformSelectView)
    assert fresh_db.get_item(listed_item["id"])["status"] == fresh_db.STATUS_LISTED


def test_move_to_sold_still_blocks_an_item_thats_already_moved_on(fresh_db, listed_item, bypass_role_check):
    fresh_db.update_status(listed_item["id"], fresh_db.STATUS_SOLD, actor_id=1)
    bot = _FakeBot({})
    cog = ItemFlow(bot)
    interaction = _FakeInteraction(bot, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(cog.move_to_sold(interaction, listed_item["id"]))

    assert interaction.response.edited_view is None
    assert "already moved on" in interaction.response.content


def test_select_dispatches_ebay_straight_to_finish_move_to_sold(monkeypatch, fresh_db, listed_item, bypass_role_check):
    sold_channel = _FakeChannel(channel_id=5)
    fresh_db.set_shared_channel("sold", 5)
    bot = _FakeBot({5: sold_channel})
    cog = ItemFlow(bot)
    bot.get_cog = lambda name: cog

    view = SalePlatformSelectView(listed_item["id"])
    monkeypatch.setattr(type(view.select), "values", property(lambda self: ["eBay"]))
    interaction = _FakeInteraction(bot, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(view._on_select(interaction))

    item = fresh_db.get_item(listed_item["id"])
    assert item["status"] == fresh_db.STATUS_SOLD
    assert item["sale_platform"] == "eBay"


def test_select_other_opens_the_free_text_modal(monkeypatch, fresh_db, listed_item, bypass_role_check):
    bot = _FakeBot({})
    view = SalePlatformSelectView(listed_item["id"])
    monkeypatch.setattr(type(view.select), "values", property(lambda self: ["Other"]))
    interaction = _FakeInteraction(bot, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(view._on_select(interaction))

    assert isinstance(interaction.response.modal, OtherSalePlatformModal)
    # The item hasn't moved yet - only submitting the modal finishes it.
    assert fresh_db.get_item(listed_item["id"])["status"] == fresh_db.STATUS_LISTED


def test_other_modal_records_a_custom_platform_string(fresh_db, listed_item, bypass_role_check):
    sold_channel = _FakeChannel(channel_id=5)
    fresh_db.set_shared_channel("sold", 5)
    bot = _FakeBot({5: sold_channel})
    cog = ItemFlow(bot)
    bot.get_cog = lambda name: cog
    message = _FakeMessage()

    modal = OtherSalePlatformModal(listed_item["id"], message)
    modal.platform._value = "Mercari"
    interaction = _FakeInteraction(bot, message=message, role=bypass_role_check)

    asyncio.run(modal.on_submit(interaction))

    item = fresh_db.get_item(listed_item["id"])
    assert item["status"] == fresh_db.STATUS_SOLD
    assert item["sale_platform"] == "Other: Mercari"


def test_finish_move_to_sold_posts_a_worklist_entry_to_the_accounting_channel(fresh_db, listed_item, bypass_role_check):
    sold_channel = _FakeChannel(channel_id=5)
    accounting_channel = _FakeChannel(channel_id=6)
    fresh_db.set_shared_channel("sold", 5)
    fresh_db.set_shared_channel("accounting", 6)
    bot = _FakeBot({5: sold_channel, 6: accounting_channel})
    cog = ItemFlow(bot)
    message = _FakeMessage()
    interaction = _FakeInteraction(bot, message=message, role=bypass_role_check)

    asyncio.run(cog.finish_move_to_sold(interaction, listed_item["id"], "In Person", message))

    assert len(accounting_channel.sent) == 1
    posted_text = accounting_channel.sent[0][0][0]
    assert "In Person" in posted_text
    assert "log-sale" in posted_text


def test_finish_move_to_sold_is_a_noop_when_accounting_channel_not_set_up(fresh_db, listed_item, bypass_role_check):
    sold_channel = _FakeChannel(channel_id=5)
    fresh_db.set_shared_channel("sold", 5)
    bot = _FakeBot({5: sold_channel})
    cog = ItemFlow(bot)
    message = _FakeMessage()
    interaction = _FakeInteraction(bot, message=message, role=bypass_role_check)

    # Should not raise even though #accounting was never created.
    asyncio.run(cog.finish_move_to_sold(interaction, listed_item["id"], "eBay", message))

    assert fresh_db.get_item(listed_item["id"])["status"] == fresh_db.STATUS_SOLD


def test_finish_move_to_sold_blocks_an_item_thats_already_moved_on(fresh_db, listed_item, bypass_role_check):
    fresh_db.update_status(listed_item["id"], fresh_db.STATUS_SOLD, actor_id=1)
    bot = _FakeBot({})
    cog = ItemFlow(bot)
    message = _FakeMessage()
    interaction = _FakeInteraction(bot, message=message, role=bypass_role_check)

    asyncio.run(cog.finish_move_to_sold(interaction, listed_item["id"], "eBay", message))

    assert "already moved on" in interaction.response.content
