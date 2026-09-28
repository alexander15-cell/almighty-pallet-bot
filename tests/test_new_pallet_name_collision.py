"""
NewPalletModal.on_submit (cogs/pallet_setup.py) - pallets.name is UNIQUE in
the database and never freed up, even for an archived pallet (its Discord
category gets deleted on archive, but the database row - and its reserved
name - stays permanently, see /admin pallet-archive). The old name-collision
check only looked at live Discord categories, so trying to reuse an
archived pallet's name sailed past it, created a brand-new Discord
category, and only then crashed with sqlite3.IntegrityError: UNIQUE
constraint failed: pallets.name - leaving an orphaned category with no
matching pallet behind. The real, working check has to happen first,
against the database, before Discord is touched at all.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

from cogs.pallet_setup import NewPalletModal


class _FakeRole:
    def __init__(self, name):
        self.name = name


class _FakeCategory:
    def __init__(self, name, category_id):
        self.name = name
        self.id = category_id
        self.channels = []


class _FakeGuild:
    def __init__(self):
        self.categories = []
        self.roles = []
        self.default_role = _FakeRole("@everyone")
        self.category_created = False

    async def create_category(self, name):
        self.category_created = True
        return _FakeCategory(name, 9999)


class _FakeResponse:
    async def defer(self, ephemeral=True, thinking=True):
        pass


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeUser:
    id = 1


class _FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = _FakeUser()


def _modal(name, notes=""):
    modal = NewPalletModal.__new__(NewPalletModal)
    discord_ui_modal_init = super(NewPalletModal, modal).__init__
    discord_ui_modal_init()
    modal.pallet_name._value = name
    modal.notes._value = notes
    return modal


def test_reusing_an_archived_pallets_name_is_rejected_before_touching_discord(fresh_db):
    pallet_id = fresh_db.create_pallet("Old Pallet", category_id=111, created_by=1)
    fresh_db.archive_pallet(pallet_id)

    guild = _FakeGuild()  # no matching category - it was deleted on archive
    interaction = _FakeInteraction(guild)
    modal = _modal("Old Pallet")

    asyncio.run(modal.on_submit(interaction))

    assert guild.category_created is False
    assert "already exists" in interaction.followup.messages[-1]
    assert "archived" in interaction.followup.messages[-1]
    # Still exactly one pallet named this - no orphaned duplicate attempt happened.
    assert len(fresh_db.get_all_pallets(include_archived=True)) == 1


def test_reusing_an_active_pallets_name_is_rejected_too(fresh_db):
    fresh_db.create_pallet("Active Pallet", category_id=222, created_by=1)

    guild = _FakeGuild()
    interaction = _FakeInteraction(guild)
    modal = _modal("Active Pallet")

    asyncio.run(modal.on_submit(interaction))

    assert guild.category_created is False
    assert "already exists" in interaction.followup.messages[-1]
    assert "archived" not in interaction.followup.messages[-1]
