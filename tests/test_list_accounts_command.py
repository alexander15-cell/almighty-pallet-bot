"""
/finance list-accounts (cogs/finance.py) - lets an admin find a QuickBooks
account's numeric Id (needed for QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID) without
digging through QuickBooks' own UI/URL. Mocks quickbooks.list_accounts
directly rather than the HTTP layer - that's already covered by
test_quickbooks.py's own list_accounts tests.
"""
import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest
from discord import app_commands

import config
import quickbooks as qb
import cogs.finance as finance_module
from cogs.finance import Finance


class _FakeRole:
    def __init__(self, name):
        self.name = name


_ADMIN_ROLE = _FakeRole(config.ROLE_ADMIN)


@pytest.fixture(autouse=True)
def role_lookup(monkeypatch):
    monkeypatch.setattr(
        finance_module.runtime_settings, "resolve_role",
        lambda guild, name: _ADMIN_ROLE if name == config.ROLE_ADMIN else None,
    )


class _FakeResponse:
    def __init__(self):
        self.content = None

    async def defer(self, ephemeral=False, thinking=True):
        pass

    async def send_message(self, content=None, **kwargs):
        self.content = content


class _FakeFollowup:
    def __init__(self):
        self.content = None
        self.sent_embed = None

    async def send(self, content=None, embed=None, **kwargs):
        self.content = content
        self.sent_embed = embed


def _interaction(roles):
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(roles=roles),
        response=_FakeResponse(),
        followup=_FakeFollowup(),
    )


@pytest.fixture
def cog():
    return Finance.__new__(Finance)


def test_requires_admin_role(fresh_db, cog):
    interaction = _interaction(roles=[])

    asyncio.run(Finance.list_accounts.callback(cog, interaction, None))

    assert "Pallet Admin" in interaction.response.content


def test_requires_quickbooks_to_be_connected(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(qb, "is_connected", lambda: False)
    interaction = _interaction(roles=[_ADMIN_ROLE])

    asyncio.run(Finance.list_accounts.callback(cog, interaction, None))

    assert "connect-quickbooks" in interaction.response.content


def test_lists_accounts_with_ids_by_default_scoped_to_bank_and_credit_card(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(qb, "is_connected", lambda: True)

    async def fake_list_accounts(account_types):
        assert account_types == ["Bank", "Credit Card"]
        return [{"id": "77", "name": "Business Card", "type": "Credit Card", "balance": 123.45}]
    monkeypatch.setattr(qb, "list_accounts", fake_list_accounts)

    interaction = _interaction(roles=[_ADMIN_ROLE])
    asyncio.run(Finance.list_accounts.callback(cog, interaction, None))

    embed = interaction.followup.sent_embed
    assert "Business Card" in embed.description
    assert "77" in embed.description
    assert "$123.45" in embed.description


def test_all_account_types_choice_passes_no_filter(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(qb, "is_connected", lambda: True)
    seen = {}

    async def fake_list_accounts(account_types):
        seen["types"] = account_types
        return []
    monkeypatch.setattr(qb, "list_accounts", fake_list_accounts)

    choice = app_commands.Choice(name="All account types", value="all")
    interaction = _interaction(roles=[_ADMIN_ROLE])
    asyncio.run(Finance.list_accounts.callback(cog, interaction, choice))

    assert seen["types"] is None


def test_shows_a_helpful_message_when_no_accounts_found(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(qb, "is_connected", lambda: True)

    async def fake_list_accounts(account_types):
        return []
    monkeypatch.setattr(qb, "list_accounts", fake_list_accounts)

    interaction = _interaction(roles=[_ADMIN_ROLE])
    asyncio.run(Finance.list_accounts.callback(cog, interaction, None))

    assert "No matching accounts" in interaction.followup.content


def test_surfaces_a_quickbooks_error_instead_of_crashing(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(qb, "is_connected", lambda: True)

    async def fake_list_accounts(account_types):
        raise qb.QuickBooksError("boom")
    monkeypatch.setattr(qb, "list_accounts", fake_list_accounts)

    interaction = _interaction(roles=[_ADMIN_ROLE])
    asyncio.run(Finance.list_accounts.callback(cog, interaction, None))

    assert "boom" in interaction.followup.content
