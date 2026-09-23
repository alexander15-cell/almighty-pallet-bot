"""
/fb-marketplace export-batch and /fb-marketplace confirm-listed
(cogs/fb_marketplace.py) - calls each command's underlying callback
directly (Command.callback), same approach as
tests/test_pirate_ship_assignment.py and tests/test_finance_summary_commands.py.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import cogs.fb_marketplace as fb_marketplace_module
import fb_marketplace_csv
from cogs.fb_marketplace import FbMarketplace


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.file = None

    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, content=None, file=None, **kwargs):
        self.content = content
        self.file = file


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


class _FakeChannel:
    def __init__(self, category_id=None):
        self.category_id = category_id


class _FakeInteraction:
    def __init__(self, category_id=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.channel = _FakeChannel(category_id)
        self.user = type("U", (), {"id": 1})()


class _FakeItemFlowCog:
    def __init__(self):
        self.confirmed_ids = []

    async def confirm_fb_marketplace_pending_item(self, item, actor_id):
        if item["status"] != "pending_fb_marketplace_upload":
            return False
        self.confirmed_ids.append(item["id"])
        return True


class _FakeBot:
    def __init__(self, item_flow_cog):
        self._item_flow_cog = item_flow_cog

    def get_cog(self, name):
        return self._item_flow_cog

    def get_channel(self, channel_id):
        return None


@pytest.fixture(autouse=True)
def bypass_admin_check(monkeypatch):
    monkeypatch.setattr(fb_marketplace_module, "_is_pallet_admin", lambda interaction: True)


@pytest.fixture
def cog():
    item_flow_cog = _FakeItemFlowCog()
    bot = _FakeBot(item_flow_cog)
    c = FbMarketplace.__new__(FbMarketplace)
    c.bot = bot
    return c, item_flow_cog


# --------------------------------------------------------------- export-batch


def test_export_batch_empty_batch(fresh_db, cog, tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(fb_marketplace_csv, "ARCHIVE_DIR", tmp_path / "archive")
    c, _ = cog

    interaction = _FakeInteraction()
    asyncio.run(FbMarketplace.export_batch.callback(c, interaction))

    assert "empty" in interaction.response.content


def test_export_batch_attaches_file_and_clears_live_csv(fresh_db, cog, tmp_path, monkeypatch):
    batch_path = tmp_path / "batch.csv"
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", batch_path)
    monkeypatch.setattr(fb_marketplace_csv, "ARCHIVE_DIR", tmp_path / "archive")

    pallet_id = fresh_db.create_pallet("Export Test Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD, actor_id=1)
    item = fresh_db.get_item(item_id)
    fb_marketplace_csv.append_item_to_batch(item, {"ebay_title": "Item", "price": 10.0})
    c, _ = cog

    interaction = _FakeInteraction()
    asyncio.run(FbMarketplace.export_batch.callback(c, interaction))

    assert interaction.response.file is not None
    assert not batch_path.exists()
    assert "1 item(s)" in interaction.response.content


# ------------------------------------------------------------- confirm-listed


@pytest.fixture
def pending_item(fresh_db):
    pallet_id = fresh_db.create_pallet("FB Cog Test Pallet", category_id=99, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD, actor_id=1)
    return fresh_db.get_item(item_id)


def test_confirm_listed_requires_pallet_context(fresh_db, cog):
    c, _ = cog
    interaction = _FakeInteraction(category_id=None)
    asyncio.run(FbMarketplace.confirm_listed.callback(c, interaction, item_number=None))
    assert "Run this inside" in interaction.response.content


def test_confirm_listed_single_item(fresh_db, cog, pending_item):
    c, item_flow_cog = cog
    interaction = _FakeInteraction(category_id=99)
    asyncio.run(FbMarketplace.confirm_listed.callback(c, interaction, item_number=pending_item["item_number"]))

    assert "Confirmed item" in interaction.response.content
    assert item_flow_cog.confirmed_ids == [pending_item["id"]]


def test_confirm_listed_unknown_item_number(fresh_db, cog, pending_item):
    c, _ = cog
    interaction = _FakeInteraction(category_id=99)
    asyncio.run(FbMarketplace.confirm_listed.callback(c, interaction, item_number=99999))
    assert "No item #99999" in interaction.response.content


def test_confirm_listed_item_not_pending(fresh_db, cog, pending_item):
    fresh_db.update_status(pending_item["id"], fresh_db.STATUS_LISTED, actor_id=1)
    c, _ = cog
    interaction = _FakeInteraction(category_id=99)
    asyncio.run(FbMarketplace.confirm_listed.callback(c, interaction, item_number=pending_item["item_number"]))
    assert "isn't waiting" in interaction.response.content


def test_confirm_listed_bulk_mode_confirms_all_pending_in_pallet(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Bulk Pallet", category_id=100, created_by=1)
    id1 = fresh_db.create_item(pallet_id, "a", [], 1)
    id2 = fresh_db.create_item(pallet_id, "b", [], 1)
    fresh_db.update_status(id1, fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD, actor_id=1)
    fresh_db.update_status(id2, fresh_db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD, actor_id=1)

    c, item_flow_cog = cog
    interaction = _FakeInteraction(category_id=100)
    asyncio.run(FbMarketplace.confirm_listed.callback(c, interaction, item_number=None))

    assert "Confirmed 2 item(s)" in interaction.followup.content
    assert set(item_flow_cog.confirmed_ids) == {id1, id2}


def test_confirm_listed_bulk_mode_no_pending_items(fresh_db, cog):
    fresh_db.create_pallet("Empty Pallet", category_id=101, created_by=1)
    c, _ = cog
    interaction = _FakeInteraction(category_id=101)
    asyncio.run(FbMarketplace.confirm_listed.callback(c, interaction, item_number=None))
    assert "No items" in interaction.response.content
