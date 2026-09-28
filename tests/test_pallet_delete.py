"""
database.delete_pallet_permanently and /admin pallet delete
(cogs/admin_tools.py) - unlike archive_pallet (Discord channels removed,
every database row kept forever), this actually erases a pallet and
everything tied to it, freeing its name (UNIQUE in the database) for
reuse. The main risk here is a foreign-key ordering mistake leaving
orphaned rows behind or crashing outright (PRAGMA foreign_keys = ON is
set - see database.get_conn) - this test builds a pallet with a full,
realistic history (item events, a hold, eBay listing data, a refund, an
expense, a claimed QuickBooks-style charge) and confirms every single
piece of it is actually gone, not just the pallet row itself.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import database as db
import cogs.admin_tools as admin_tools_module
from cogs.admin_tools import AdminTools


@pytest.fixture
def full_history_pallet(fresh_db):
    pallet_id = fresh_db.create_pallet("History Pallet", category_id=555, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW)  # item_events row
    fresh_db.place_item_on_hold(item_id, reason=fresh_db.HOLD_REASON_OTHER, note="", pre_hold_status=fresh_db.STATUS_QUEUE_REVIEW)
    fresh_db.save_ebay_listing_data(item_id, "Title", "123", "3000", 19.99, {})
    fresh_db.record_item_sale(item_id, 25.0, "eBay", actor_id=1)
    fresh_db.record_refund(item_id, 5.0, "damaged", actor_id=1)
    fresh_db.record_expense(pallet_id, 10.0, "packaging", actor_id=1)

    charge_id = fresh_db.create_awaiting_pallet_charge("txn-history", 40.0, "Freight Co", "2026-09-10", allocated_by=1)
    fresh_db.claim_awaiting_pallet_charge(charge_id, pallet_id, actor_id=1)

    return pallet_id, item_id, charge_id


def test_delete_pallet_permanently_erases_every_related_row(fresh_db, full_history_pallet):
    pallet_id, item_id, charge_id = full_history_pallet

    with fresh_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM item_events WHERE item_id = ?", (item_id,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM item_holds WHERE item_id = ?", (item_id,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM ebay_listing_data WHERE item_id = ?", (item_id,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM finance_transactions WHERE pallet_id = ?", (pallet_id,)).fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM pallet_costs WHERE pallet_id = ?", (pallet_id,)).fetchone()[0] > 0

    fresh_db.delete_pallet_permanently(pallet_id)

    assert fresh_db.get_pallet(pallet_id) is None
    assert fresh_db.get_item(item_id) is None
    with fresh_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM item_events WHERE item_id = ?", (item_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM item_holds WHERE item_id = ?", (item_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ebay_listing_data WHERE item_id = ?", (item_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM finance_transactions WHERE pallet_id = ?", (pallet_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pallet_costs WHERE pallet_id = ?", (pallet_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM channel_map WHERE pallet_id = ?", (pallet_id,)).fetchone()[0] == 0

    # The claimed charge's money isn't lost - it's back in the unclaimed pool.
    charge = fresh_db.get_awaiting_pallet_charge(charge_id)
    assert charge["claimed"] == 0
    assert charge["claimed_pallet_id"] is None
    assert charge in fresh_db.get_unclaimed_pallet_charges()


def test_delete_pallet_permanently_frees_the_name_for_reuse(fresh_db, full_history_pallet):
    pallet_id, item_id, charge_id = full_history_pallet
    fresh_db.delete_pallet_permanently(pallet_id)

    # Would previously raise sqlite3.IntegrityError: UNIQUE constraint failed: pallets.name
    new_id = fresh_db.create_pallet("History Pallet", category_id=777, created_by=1)
    assert new_id != pallet_id
    assert fresh_db.get_pallet(new_id)["name"] == "History Pallet"


class _FakeRole:
    def __init__(self, name):
        self.name = name


class _FakeChannel:
    def __init__(self):
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True


class _FakeCategory:
    def __init__(self, channels):
        self.channels = channels
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True


class _FakeGuild:
    def __init__(self, category):
        self._category = category
        self.roles = [_FakeRole(config.ROLE_ADMIN)]

    def get_channel(self, channel_id):
        return self._category


class _FakeResponse:
    def __init__(self):
        self.messages = []

    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


_ADMIN_ROLE = _FakeRole(config.ROLE_ADMIN)


class _FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.user = SimpleNamespace(roles=[_ADMIN_ROLE])
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    monkeypatch.setattr(
        admin_tools_module.runtime_settings, "resolve_role",
        lambda guild, name: _ADMIN_ROLE if name == config.ROLE_ADMIN else None,
    )


def test_pallet_delete_preview_does_not_delete_anything(fresh_db, full_history_pallet):
    pallet_id, item_id, charge_id = full_history_pallet
    cog = AdminTools.__new__(AdminTools)
    guild = _FakeGuild(category=None)
    interaction = _FakeInteraction(guild)

    asyncio.run(AdminTools.pallet_delete.callback(cog, interaction, "History Pallet", confirm=False))

    assert fresh_db.get_pallet(pallet_id) is not None
    assert "permanently deletes" in interaction.response.messages[-1]


def test_pallet_delete_confirmed_deletes_pallet_and_discord_channels(fresh_db, full_history_pallet):
    pallet_id, item_id, charge_id = full_history_pallet
    channel = _FakeChannel()
    category = _FakeCategory([channel])
    cog = AdminTools.__new__(AdminTools)
    guild = _FakeGuild(category)
    interaction = _FakeInteraction(guild)

    asyncio.run(AdminTools.pallet_delete.callback(cog, interaction, "History Pallet", confirm=True))

    assert fresh_db.get_pallet(pallet_id) is None
    assert channel.deleted is True
    assert category.deleted is True
    assert "permanently deleted" in interaction.followup.messages[-1]


def test_pallet_delete_unknown_name_is_rejected(fresh_db):
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction(_FakeGuild(category=None))

    asyncio.run(AdminTools.pallet_delete.callback(cog, interaction, "Nonexistent Pallet", confirm=True))

    assert "No pallet named" in interaction.response.messages[-1]
