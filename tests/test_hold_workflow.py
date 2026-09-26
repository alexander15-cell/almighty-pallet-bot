"""
The Hold workflow: database.place_item_on_hold/get_open_hold/resolve_item_hold,
ItemFlow.place_item_on_hold/resolve_item_hold/_channel_and_view_for_status
(cogs/item_flow.py), the HoldResolvedButton DynamicItem, and /item hold
(cogs/admin_tools.py). Uses the same minimal fake Discord objects as
tests/test_confirm_listed_buttons.py.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from discord import app_commands

import cogs.admin_tools as admin_tools_module
import cogs.item_flow as item_flow_module
import database as db
import pytest
from cogs.admin_tools import AdminTools
from cogs.item_flow import (
    AwaitingListingView,
    HoldResolvedButton,
    ItemFlow,
    ListedView,
    QueueReviewView,
    ShippedView,
    _channel_and_view_for_status,
)


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


class _FakeChannelContext:
    def __init__(self, category_id):
        self.category_id = category_id


class _FakeResponse:
    def __init__(self):
        self.content = None

    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, content=None, **kwargs):
        self.content = content


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


class _FakeInteraction:
    def __init__(self, client=None, message=None, role=None, channel=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = message
        self.user = _FakeUser(role)
        self.guild = object()
        self.client = client
        self.channel = channel


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()
    monkeypatch.setattr(admin_tools_module, "_is_pallet_admin", lambda interaction: True)

    def fake_resolve_role(guild, role_name):
        return role
    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", fake_resolve_role)
    return role


@pytest.fixture
def held_item(fresh_db):
    """An item sitting in Queue Review, ready to be placed on hold."""
    pallet_id = fresh_db.create_pallet("Hold Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=999)
    return fresh_db.get_item(item_id)


# --------------------------------------------------------------- DB layer


def test_place_item_on_hold_sets_status_and_records_hold(fresh_db, held_item):
    hold_id = fresh_db.place_item_on_hold(
        held_item["id"], reason=fresh_db.HOLD_REASON_EBAY_POLICY, note=None,
        pre_hold_status=held_item["status"], actor_id=7,
    )
    assert hold_id is not None

    updated = fresh_db.get_item(held_item["id"])
    assert updated["status"] == fresh_db.STATUS_ON_HOLD

    hold = fresh_db.get_open_hold(held_item["id"])
    assert hold["reason"] == fresh_db.HOLD_REASON_EBAY_POLICY
    assert hold["pre_hold_status"] == fresh_db.STATUS_QUEUE_REVIEW
    assert hold["set_by"] == 7
    assert hold["resolved_at"] is None


def test_get_open_hold_none_when_not_held(fresh_db, held_item):
    assert fresh_db.get_open_hold(held_item["id"]) is None


def test_resolve_item_hold_marks_resolved_without_deleting(fresh_db, held_item):
    fresh_db.place_item_on_hold(
        held_item["id"], reason=fresh_db.HOLD_REASON_OTHER, note="needs review",
        pre_hold_status=held_item["status"], actor_id=1,
    )

    pre_status = fresh_db.resolve_item_hold(held_item["id"], actor_id=9)
    assert pre_status == fresh_db.STATUS_QUEUE_REVIEW

    # No longer "open" ...
    assert fresh_db.get_open_hold(held_item["id"]) is None
    # ... but the row itself, and its reason/note, still exist.
    with fresh_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM item_holds WHERE item_id = ?", (held_item["id"],)).fetchone()
    assert row["resolved_by"] == 9
    assert row["resolved_at"] is not None
    assert row["note"] == "needs review"


def test_resolve_item_hold_returns_none_when_nothing_open(fresh_db, held_item):
    assert fresh_db.resolve_item_hold(held_item["id"], actor_id=1) is None


def test_resolve_item_hold_twice_only_resolves_once(fresh_db, held_item):
    fresh_db.place_item_on_hold(
        held_item["id"], reason=fresh_db.HOLD_REASON_EBAY_POLICY, note=None,
        pre_hold_status=held_item["status"], actor_id=1,
    )
    fresh_db.resolve_item_hold(held_item["id"], actor_id=1)
    assert fresh_db.resolve_item_hold(held_item["id"], actor_id=1) is None


# --------------------------------------------------- _channel_and_view_for_status


def test_channel_and_view_for_queue_review():
    stage, view = _channel_and_view_for_status(1, db.STATUS_QUEUE_REVIEW)
    assert stage == "queue-review"
    assert isinstance(view, QueueReviewView)


def test_channel_and_view_for_awaiting_listing():
    stage, view = _channel_and_view_for_status(1, db.STATUS_AWAITING_LISTING)
    assert stage == "awaiting-listing"
    assert isinstance(view, AwaitingListingView)


def test_channel_and_view_for_pending_ebay_upload():
    stage, view = _channel_and_view_for_status(5, db.STATUS_PENDING_EBAY_UPLOAD)
    assert stage == "pending-ebay-upload"
    assert view.children[0].item.custom_id == "pallet_bot:confirm_ebay_listed:5"


def test_channel_and_view_for_pending_fb_marketplace_upload():
    stage, view = _channel_and_view_for_status(5, db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD)
    assert stage == "pending-fb-marketplace-upload"
    assert view.children[0].item.custom_id == "pallet_bot:confirm_fb_listed:5"


def test_channel_and_view_for_listed():
    stage, view = _channel_and_view_for_status(1, db.STATUS_LISTED)
    assert stage == "listed"
    assert isinstance(view, ListedView)


def test_channel_and_view_for_sold():
    stage, view = _channel_and_view_for_status(1, db.STATUS_SOLD)
    assert stage == "sold"
    assert isinstance(view, ShippedView)


def test_channel_and_view_none_for_ineligible_status():
    assert _channel_and_view_for_status(1, db.STATUS_DATA_ENTRY) == (None, None)
    assert _channel_and_view_for_status(1, db.STATUS_SHIPPED) == (None, None)


# ------------------------------------------------------- ItemFlow.place_item_on_hold


def test_place_item_on_hold_posts_card_and_clears_old_one(fresh_db, held_item):
    old_channel = _FakeChannel(channel_id=100)
    old_msg = asyncio.run(old_channel.send(content="queue review card"))
    fresh_db.update_status(held_item["id"], fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=old_msg.id)
    fresh_db.set_shared_channel("queue-review", 100)

    hold_channel = _FakeChannel(channel_id=200)
    fresh_db.set_shared_channel("hold", 200)

    bot = _FakeBot({100: old_channel, 200: hold_channel})
    cog = ItemFlow(bot)

    item = fresh_db.get_item(held_item["id"])
    placed = asyncio.run(
        cog.place_item_on_hold(item, reason=db.HOLD_REASON_CATEGORY_REVIEW, note="no valid category", actor_id=3)
    )

    assert placed is True
    assert len(hold_channel.sent) == 1
    footer_text = hold_channel.sent[0]["embeds"][0].footer.text
    assert "Category needs manual review" in footer_text
    assert "no valid category" in footer_text
    assert old_msg.deleted is True
    assert fresh_db.get_item(held_item["id"])["status"] == fresh_db.STATUS_ON_HOLD


def test_place_item_on_hold_returns_false_for_ineligible_status(fresh_db):
    pallet_id = fresh_db.create_pallet("Ineligible Pallet", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)  # still at data_entry
    item = fresh_db.get_item(item_id)

    cog = ItemFlow(_FakeBot({}))
    placed = asyncio.run(cog.place_item_on_hold(item, reason=db.HOLD_REASON_OTHER, note="x", actor_id=1))

    assert placed is False
    assert fresh_db.get_item(item_id)["status"] == fresh_db.STATUS_DATA_ENTRY


# --------------------------------------------------------- ItemFlow.resolve_item_hold


def test_resolve_item_hold_reposts_to_pre_hold_channel(fresh_db, held_item):
    hold_channel = _FakeChannel(channel_id=200)
    hold_msg = asyncio.run(hold_channel.send(content="hold card"))
    fresh_db.set_shared_channel("hold", 200)
    fresh_db.place_item_on_hold(
        held_item["id"], reason=db.HOLD_REASON_MANUAL_LISTING, note=None,
        pre_hold_status=fresh_db.STATUS_QUEUE_REVIEW, actor_id=1,
    )
    fresh_db.update_status(held_item["id"], fresh_db.STATUS_ON_HOLD, actor_id=1, new_message_id=hold_msg.id)

    queue_channel = _FakeChannel(channel_id=100)
    fresh_db.set_shared_channel("queue-review", 100)
    bot = _FakeBot({100: queue_channel, 200: hold_channel})
    cog = ItemFlow(bot)

    item = fresh_db.get_item(held_item["id"])
    moved, stage = asyncio.run(cog.resolve_item_hold(item, actor_id=9))

    assert moved is True
    assert stage == "queue-review"
    assert len(queue_channel.sent) == 1
    assert hold_msg.deleted is True
    assert fresh_db.get_item(held_item["id"])["status"] == fresh_db.STATUS_QUEUE_REVIEW
    assert fresh_db.get_open_hold(held_item["id"]) is None


def test_resolve_item_hold_no_op_when_not_on_hold(fresh_db, held_item):
    cog = ItemFlow(_FakeBot({}))
    moved, stage = asyncio.run(cog.resolve_item_hold(held_item, actor_id=1))
    assert (moved, stage) == (False, None)


# ------------------------------------------------------------- HoldResolvedButton


def test_hold_resolved_button_moves_item_back(fresh_db, held_item, bypass_role_check):
    hold_channel = _FakeChannel(channel_id=200)
    hold_msg = asyncio.run(hold_channel.send(content="hold card"))
    fresh_db.set_shared_channel("hold", 200)
    fresh_db.place_item_on_hold(
        held_item["id"], reason=db.HOLD_REASON_PHOTO_DATA_ISSUE, note=None,
        pre_hold_status=fresh_db.STATUS_QUEUE_REVIEW, actor_id=1,
    )
    fresh_db.update_status(held_item["id"], fresh_db.STATUS_ON_HOLD, actor_id=1, new_message_id=hold_msg.id)

    queue_channel = _FakeChannel(channel_id=100)
    fresh_db.set_shared_channel("queue-review", 100)
    cog = ItemFlow(_FakeBot({100: queue_channel, 200: hold_channel}))
    client = _FakeBot({100: queue_channel, 200: hold_channel}, item_flow_cog=cog)

    button = HoldResolvedButton(held_item["id"])
    interaction = _FakeInteraction(client, role=bypass_role_check)
    asyncio.run(button.callback(interaction))

    assert "Hold resolved" in interaction.followup.content
    assert fresh_db.get_item(held_item["id"])["status"] == fresh_db.STATUS_QUEUE_REVIEW


def test_hold_resolved_button_rejects_when_not_on_hold(fresh_db, held_item, bypass_role_check):
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)

    button = HoldResolvedButton(held_item["id"])  # still at queue_review, never held
    interaction = _FakeInteraction(client, role=bypass_role_check)
    asyncio.run(button.callback(interaction))

    assert "isn't on hold anymore" in interaction.response.content


def test_hold_resolved_button_from_custom_id_parses_item_id():
    import re
    match = re.match(HoldResolvedButton.__discord_ui_compiled_template__, "pallet_bot:hold_resolved:17")
    assert match["item_id"] == "17"


# --------------------------------------------------------------------- command


def test_item_hold_requires_pallet_context(fresh_db):
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(channel=_FakeChannelContext(category_id=None))
    reason = app_commands.Choice(name="Other", value=db.HOLD_REASON_OTHER)
    asyncio.run(AdminTools.item_hold.callback(cog, interaction, 1, reason, note="x"))
    assert "Run this inside" in interaction.response.content


def test_item_hold_unknown_item(fresh_db):
    fresh_db.create_pallet("Hold Cmd Pallet", category_id=10, created_by=1)
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(channel=_FakeChannelContext(category_id=10))
    reason = app_commands.Choice(name="Other", value=db.HOLD_REASON_OTHER)
    asyncio.run(AdminTools.item_hold.callback(cog, interaction, 99, reason, note="x"))
    assert "No item #99 found" in interaction.response.content


def test_item_hold_other_requires_note(fresh_db):
    pallet_id = fresh_db.create_pallet("Hold Cmd Pallet 2", category_id=11, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1)

    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(channel=_FakeChannelContext(category_id=11))
    reason = app_commands.Choice(name="Other", value=db.HOLD_REASON_OTHER)
    asyncio.run(AdminTools.item_hold.callback(cog, interaction, 1, reason, note=None))
    assert "A `note` is required" in interaction.response.content


def test_item_hold_success_delegates_to_item_flow_cog(fresh_db):
    pallet_id = fresh_db.create_pallet("Hold Cmd Pallet 3", category_id=12, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=1234)
    fresh_db.set_shared_channel("hold", 300)

    hold_channel = _FakeChannel(channel_id=300)
    queue_channel = _FakeChannel(channel_id=400)
    old_msg = asyncio.run(queue_channel.send(content="qr card"))
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=old_msg.id)
    fresh_db.set_shared_channel("queue-review", 400)

    item_flow_cog = ItemFlow(_FakeBot({300: hold_channel, 400: queue_channel}))
    bot = _FakeBot({300: hold_channel, 400: queue_channel}, item_flow_cog=item_flow_cog)
    cog = AdminTools.__new__(AdminTools)
    cog.bot = bot

    interaction = _FakeInteraction(channel=_FakeChannelContext(category_id=12))
    reason = app_commands.Choice(name=db.HOLD_REASON_LABELS[db.HOLD_REASON_EBAY_POLICY], value=db.HOLD_REASON_EBAY_POLICY)
    asyncio.run(AdminTools.item_hold.callback(cog, interaction, 1, reason, note=None))

    assert "placed on hold" in interaction.followup.content
    assert fresh_db.get_item(item_id)["status"] == fresh_db.STATUS_ON_HOLD
    assert len(hold_channel.sent) == 1


def test_item_hold_reports_ineligible_status(fresh_db):
    pallet_id = fresh_db.create_pallet("Hold Cmd Pallet 4", category_id=13, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)  # still at data_entry

    item_flow_cog = ItemFlow(_FakeBot({}))
    bot = _FakeBot({}, item_flow_cog=item_flow_cog)
    cog = AdminTools.__new__(AdminTools)
    cog.bot = bot

    interaction = _FakeInteraction(channel=_FakeChannelContext(category_id=13))
    reason = app_commands.Choice(name=db.HOLD_REASON_LABELS[db.HOLD_REASON_EBAY_POLICY], value=db.HOLD_REASON_EBAY_POLICY)
    asyncio.run(AdminTools.item_hold.callback(cog, interaction, 1, reason, note=None))

    assert "Can't place item #1 on hold" in interaction.followup.content
