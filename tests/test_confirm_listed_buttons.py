"""
ConfirmEbayListedButton / ConfirmFbMarketplaceListedButton (cogs/item_flow.py)
- the per-card "Confirm Listed" buttons on pending-upload cards, added so
someone standing in the shared #pending-ebay-upload/#pending-fb-marketplace-upload
channel can confirm an item without navigating to that specific pallet's
own channel (which /ebay confirm-listed and /fb-marketplace confirm-listed
still require, since item numbers aren't unique across pallets). Also
checks that add_to_ebay_batch/add_to_fb_marketplace_batch attach the
correct button to the posted pending card.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import ebay_csv
import fb_marketplace_csv
import cogs.item_flow as item_flow_module
import pytest
from cogs.item_flow import ItemFlow, ConfirmEbayListedButton, ConfirmFbMarketplaceListedButton


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


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.deferred = False

    async def defer(self, ephemeral=True, thinking=True):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.content = content


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

    def fake_resolve_role(guild, role_name):
        return role

    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", fake_resolve_role)
    return role


@pytest.fixture
def pending_ebay_item(fresh_db):
    pallet_id = fresh_db.create_pallet("Ebay Confirm Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, ebay_title="Widget", category_id="123", condition_id="1500",
        price=25.0, item_specifics={}, weight_lb=1.0, length_in=5, width_in=5, height_in=5,
    )
    fresh_db.update_status(item_id, fresh_db.STATUS_PENDING_EBAY_UPLOAD, actor_id=1)
    return fresh_db.get_item(item_id)


@pytest.fixture
def pending_fb_item(fresh_db):
    pallet_id = fresh_db.create_pallet("FB Confirm Pallet", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, ebay_title="Widget", category_id="123", condition_id="1500",
        price=25.0, item_specifics={}, weight_lb=1.0, length_in=5, width_in=5, height_in=5,
    )
    fresh_db.update_status(item_id, fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD, actor_id=1)
    return fresh_db.get_item(item_id)


# ----------------------------------------- buttons attached when posting


def test_add_to_ebay_batch_attaches_confirm_button(fresh_db, tmp_path, monkeypatch, bypass_role_check):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    pallet_id = fresh_db.create_pallet("Ebay Attach Pallet", category_id=3, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, ebay_title="Widget", category_id="123", condition_id="1500",
        price=25.0, item_specifics={}, weight_lb=1.0, length_in=5, width_in=5, height_in=5,
    )
    fresh_db.update_status(item_id, fresh_db.STATUS_AWAITING_LISTING, actor_id=1)
    item = fresh_db.get_item(item_id)

    pending_channel = _FakeChannel(channel_id=555)
    fresh_db.set_shared_channel("pending-ebay-upload", 555)
    bot = _FakeBot({555: pending_channel})
    cog = ItemFlow(bot)

    interaction = _FakeInteraction(bot, message=_FakeMessage(), role=bypass_role_check)
    asyncio.run(cog.add_to_ebay_batch(interaction, item["id"]))

    view = pending_channel.sent[0]["view"]
    assert view.children[0].item.custom_id == f"pallet_bot:confirm_ebay_listed:{item['id']}"


def test_add_to_fb_marketplace_batch_attaches_confirm_button(fresh_db, tmp_path, monkeypatch, bypass_role_check):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    pallet_id = fresh_db.create_pallet("FB Attach Pallet", category_id=4, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, ebay_title="Widget", category_id="123", condition_id="1500",
        price=25.0, item_specifics={}, weight_lb=1.0, length_in=5, width_in=5, height_in=5,
    )
    fresh_db.update_status(item_id, fresh_db.STATUS_AWAITING_LISTING, actor_id=1)
    item = fresh_db.get_item(item_id)

    pending_channel = _FakeChannel(channel_id=556)
    fresh_db.set_shared_channel("pending-fb-marketplace-upload", 556)
    bot = _FakeBot({556: pending_channel})
    cog = ItemFlow(bot)

    interaction = _FakeInteraction(bot, message=_FakeMessage(), role=bypass_role_check)
    asyncio.run(cog.add_to_fb_marketplace_batch(interaction, item["id"]))

    view = pending_channel.sent[0]["view"]
    assert view.children[0].item.custom_id == f"pallet_bot:confirm_fb_listed:{item['id']}"


# --------------------------------------------------------------- eBay button


def test_confirm_ebay_listed_button_moves_item_to_listed(fresh_db, pending_ebay_item, bypass_role_check):
    listed_channel = _FakeChannel(channel_id=777)
    fresh_db.set_shared_channel("listed", 777)
    cog = ItemFlow(_FakeBot({777: listed_channel}))
    client = _FakeBot({777: listed_channel}, item_flow_cog=cog)

    button = ConfirmEbayListedButton(pending_ebay_item["id"])
    interaction = _FakeInteraction(client, role=bypass_role_check)
    asyncio.run(button.callback(interaction))

    assert fresh_db.get_item(pending_ebay_item["id"])["status"] == fresh_db.STATUS_LISTED
    assert "Confirmed live on eBay" in interaction.followup.content
    assert len(listed_channel.sent) == 1


def test_confirm_ebay_listed_button_rejects_item_no_longer_pending(fresh_db, pending_ebay_item, bypass_role_check):
    fresh_db.update_status(pending_ebay_item["id"], fresh_db.STATUS_LISTED, actor_id=1)
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)

    button = ConfirmEbayListedButton(pending_ebay_item["id"])
    interaction = _FakeInteraction(client, role=bypass_role_check)
    asyncio.run(button.callback(interaction))

    assert "isn't waiting on an eBay batch upload" in interaction.response.content


def test_confirm_ebay_listed_button_from_custom_id_parses_item_id():
    import re
    match = re.match(ConfirmEbayListedButton.__discord_ui_compiled_template__, "pallet_bot:confirm_ebay_listed:42")
    assert match["item_id"] == "42"


# ------------------------------------------------------------ FB Marketplace


def test_confirm_fb_listed_button_moves_item_to_listed(fresh_db, pending_fb_item, bypass_role_check):
    listed_channel = _FakeChannel(channel_id=888)
    fresh_db.set_shared_channel("listed", 888)
    cog = ItemFlow(_FakeBot({888: listed_channel}))
    client = _FakeBot({888: listed_channel}, item_flow_cog=cog)

    button = ConfirmFbMarketplaceListedButton(pending_fb_item["id"])
    interaction = _FakeInteraction(client, role=bypass_role_check)
    asyncio.run(button.callback(interaction))

    assert fresh_db.get_item(pending_fb_item["id"])["status"] == fresh_db.STATUS_LISTED
    assert "Confirmed live on FB Marketplace" in interaction.followup.content
    assert len(listed_channel.sent) == 1


def test_confirm_fb_listed_button_rejects_item_no_longer_pending(fresh_db, pending_fb_item, bypass_role_check):
    fresh_db.update_status(pending_fb_item["id"], fresh_db.STATUS_LISTED, actor_id=1)
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)

    button = ConfirmFbMarketplaceListedButton(pending_fb_item["id"])
    interaction = _FakeInteraction(client, role=bypass_role_check)
    asyncio.run(button.callback(interaction))

    assert "isn't waiting on an FB Marketplace batch upload" in interaction.response.content
