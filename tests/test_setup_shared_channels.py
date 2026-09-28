"""
/setup shared-channels (cogs/pallet_setup.py) - now routes the finance
channels (credit-card-charges, awaiting-pallet-charges, submit-invoices)
into their own "Finance" category, separate from the item-pipeline stage
channels' "Shared Pallet Pipeline" category, rather than mixing both kinds
of channel into one category as before.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import config
from cogs.pallet_setup import PalletSetup


class _FakeRole:
    def __init__(self, name):
        self.name = name


class _FakeMessage:
    async def pin(self, reason=None):
        pass


class _FakeCategory:
    def __init__(self, name, category_id):
        self.name = name
        self.id = category_id
        self.channels = []


class _FakeTextChannel:
    def __init__(self, name, channel_id, category):
        self.name = name
        self.id = channel_id
        self.category = category

    async def send(self, *args, **kwargs):
        return _FakeMessage()


class _FakeGuild:
    def __init__(self):
        self.categories = []
        self.roles = [_FakeRole(config.ROLE_ADMIN)]
        self.default_role = _FakeRole("@everyone")
        self._next_channel_id = 1000
        self._next_category_id = 5000

    async def create_category(self, name):
        cat = _FakeCategory(name, self._next_category_id)
        self._next_category_id += 1
        self.categories.append(cat)
        return cat

    async def create_text_channel(self, name, category, overwrites=None, topic=None):
        ch = _FakeTextChannel(name, self._next_channel_id, category)
        self._next_channel_id += 1
        category.channels.append(ch)
        return ch


class _FakeResponse:
    async def defer(self, ephemeral=True, thinking=True):
        pass


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeInteraction:
    def __init__(self, guild):
        self.guild = guild
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def test_finance_and_pipeline_channels_land_in_separate_categories(fresh_db):
    cog = PalletSetup.__new__(PalletSetup)
    guild = _FakeGuild()
    interaction = _FakeInteraction(guild)

    asyncio.run(PalletSetup.setup_shared_channels.callback(cog, interaction))

    pipeline_category = next(c for c in guild.categories if c.name == config.SHARED_PIPELINE_CATEGORY_NAME)
    finance_category = next(c for c in guild.categories if c.name == config.FINANCE_CATEGORY_NAME)
    assert pipeline_category is not finance_category

    finance_channel_names = {ch.name for ch in finance_category.channels}
    pipeline_channel_names = {ch.name for ch in pipeline_category.channels}
    assert finance_channel_names == set(config.FINANCE_SHARED_CHANNELS)
    assert pipeline_channel_names == set(config.SHARED_STAGE_CHANNELS)

    for stage in config.FINANCE_SHARED_CHANNELS:
        assert fresh_db.get_shared_channel_id(stage) is not None
    for stage in config.SHARED_STAGE_CHANNELS:
        assert fresh_db.get_shared_channel_id(stage) is not None
