"""
/finance pallet-summary and /finance overview (cogs/finance.py) - calls
each command's underlying callback directly (Command.callback), same
approach as tests/test_pirate_ship_assignment.py.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import quickbooks as qb
from cogs.finance import Finance


class _FakeResponse:
    def __init__(self):
        self.sent_embed = None

    async def defer(self, ephemeral=False, thinking=True):
        pass

    async def send_message(self, content=None, embed=None, ephemeral=False, **kwargs):
        self.content = content
        self.sent_embed = embed


class _FakeFollowup:
    def __init__(self):
        self.sent_embed = None

    async def send(self, content=None, embed=None, **kwargs):
        self.content = content
        self.sent_embed = embed


class _FakeInteraction:
    def __init__(self, channel=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.channel = channel


class _FakeChannel:
    def __init__(self, category_id):
        self.category_id = category_id


@pytest.fixture
def cog():
    return Finance.__new__(Finance)


def _field(embed, name):
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


# --------------------------------------------------------------- pallet-summary


def test_pallet_summary_by_name(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Report Pallet", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 100.0, actor_id=1)
    fresh_db.add_pallet_cost(pallet_id, "shipping", 10.0, source="pirateship")
    item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.record_item_sale(item, 200.0, "eBay", actor_id=1)

    interaction = _FakeInteraction()
    asyncio.run(Finance.pallet_summary.callback(cog, interaction, pallet_name="report pallet"))

    embed = interaction.response.sent_embed
    assert embed is not None
    assert "Purchase" in _field(embed, "Cost Basis")
    assert "Shipping" in _field(embed, "Cost Basis")
    assert "Total: $110.00" in _field(embed, "Cost Basis")
    assert _field(embed, "Revenue So Far") == "$200.00"


def test_pallet_summary_unknown_name(fresh_db, cog):
    interaction = _FakeInteraction()
    asyncio.run(Finance.pallet_summary.callback(cog, interaction, pallet_name="Nope"))

    assert interaction.response.sent_embed is None
    assert "No pallet named" in interaction.response.content


def test_pallet_summary_falls_back_to_channel_context(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Channel Pallet", category_id=42, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 50.0, actor_id=1)

    interaction = _FakeInteraction(channel=_FakeChannel(category_id=42))
    asyncio.run(Finance.pallet_summary.callback(cog, interaction, pallet_name=None))

    embed = interaction.response.sent_embed
    assert embed is not None
    assert embed.title.startswith("📊 Channel Pallet")


def test_pallet_summary_no_context_and_no_name(fresh_db, cog):
    interaction = _FakeInteraction(channel=_FakeChannel(category_id=None))
    asyncio.run(Finance.pallet_summary.callback(cog, interaction, pallet_name=None))

    assert interaction.response.sent_embed is None
    assert "pallet_name" in interaction.response.content


def test_pallet_summary_shows_cost_not_set_when_nothing_entered(fresh_db, cog):
    fresh_db.create_pallet("No Cost Pallet", category_id=1, created_by=1)
    interaction = _FakeInteraction()
    asyncio.run(Finance.pallet_summary.callback(cog, interaction, pallet_name="No Cost Pallet"))

    embed = interaction.response.sent_embed
    assert _field(embed, "Cost Basis") == "Nothing recorded yet"
    assert _field(embed, "Profit/Margin") == "Cost not set yet"


# ------------------------------------------------------------------- overview


class _FakeBot:
    pass


def test_overview_shows_not_configured_when_no_quickbooks_app(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_CLIENT_ID", None)
    monkeypatch.setattr(config, "QUICKBOOKS_CLIENT_SECRET", None)
    cog.bot = _FakeBot()

    interaction = _FakeInteraction()
    asyncio.run(Finance.overview.callback(cog, interaction))

    embed = interaction.followup.sent_embed
    assert _field(embed, "QuickBooks Balance") == "Not configured"


def test_overview_shows_balance_when_connected(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )

    async def fake_get_account_balance(account_id):
        return {"id": account_id, "name": "Business Card", "balance": 250.0}
    monkeypatch.setattr(qb, "get_account_balance", fake_get_account_balance)

    cog.bot = _FakeBot()
    interaction = _FakeInteraction()
    asyncio.run(Finance.overview.callback(cog, interaction))

    embed = interaction.followup.sent_embed
    assert "$250.00" in _field(embed, "QuickBooks Balance")


def test_overview_shows_pallet_progress_and_mtd(fresh_db, cog):
    p1 = fresh_db.create_pallet("A", category_id=1, created_by=1)
    item = fresh_db.create_item(p1, "widget", [], 1)
    fresh_db.record_item_sale(item, 40.0, "eBay", actor_id=1)
    fresh_db.update_status(item, fresh_db.STATUS_SOLD, actor_id=1)
    fresh_db.create_pallet("B", category_id=2, created_by=1)

    cog.bot = _FakeBot()
    interaction = _FakeInteraction()
    asyncio.run(Finance.overview.callback(cog, interaction))

    embed = interaction.followup.sent_embed
    assert _field(embed, "Month-to-Date Revenue") == "$40.00"
    assert "1 in progress, 1 fully sold out" == _field(embed, "Pallets")
