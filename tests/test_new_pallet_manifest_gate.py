"""
Gating "Start New Pallet" on a ready invoice+manifest package from
#submit-invoices (cogs/pallet_setup.py) - the user's own design: a pallet
shouldn't be creatable until its invoice and manifest are ready, since
Queue Review has nothing to match items against otherwise. Pallet Admin
gets an escape hatch for a lot that genuinely has no manifest.
"""
import asyncio
import os
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import config
import cogs.pallet_setup as pallet_setup_module
import quickbooks as qb
from cogs.pallet_setup import (
    NewPalletModal, NewPalletView, ReadyPackageSelect, ReadyPackageView,
    _claim_charges_for_pallet,
)


class _FakeRole:
    def __init__(self, name):
        self.name = name


class _FakeResponse:
    def __init__(self):
        self.messages = []
        self.modals = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, ephemeral=True, thinking=True):
        pass


class _FakeUser:
    def __init__(self, roles=None):
        self.roles = roles or []


class _FakeGuild:
    def __init__(self):
        self.roles = []


class _FakeInteraction:
    def __init__(self, roles=None):
        self.response = _FakeResponse()
        self.user = _FakeUser(roles=roles)
        self.guild = _FakeGuild()
        self.client = None


@pytest.fixture
def admin_role(monkeypatch):
    role = _FakeRole(config.ROLE_ADMIN)
    monkeypatch.setattr(
        pallet_setup_module.runtime_settings, "resolve_role",
        lambda guild, name: role if name == config.ROLE_ADMIN else None,
    )
    return role


@pytest.fixture
def no_admin_role(monkeypatch):
    monkeypatch.setattr(pallet_setup_module.runtime_settings, "resolve_role", lambda guild, name: None)


def _button_callback(view):
    # discord.py binds @discord.ui.button methods as _ItemCallback - the
    # correct invocation is .callback(interaction), not .callback(view, interaction);
    # see tests/test_log_sale_command.py for the same established pattern.
    return view.start_new_pallet.callback


def test_blocked_when_shared_channels_not_setup(fresh_db, no_admin_role):
    view = NewPalletView()
    interaction = _FakeInteraction()

    asyncio.run(_button_callback(view)(interaction))

    assert "shared pipeline channels" in interaction.response.messages[-1][0]
    assert interaction.response.modals == []


def test_non_admin_blocked_with_no_ready_package(fresh_db, no_admin_role):
    fresh_db.set_shared_channel("automated-review", 1)
    for stage in config.SHARED_STAGE_CHANNELS:
        fresh_db.set_shared_channel(stage, 100)
    view = NewPalletView()
    interaction = _FakeInteraction()

    asyncio.run(_button_callback(view)(interaction))

    assert "No ready invoice + manifest package" in interaction.response.messages[-1][0]
    assert interaction.response.modals == []


def test_admin_can_skip_with_no_ready_package(fresh_db, admin_role):
    for stage in config.SHARED_STAGE_CHANNELS:
        fresh_db.set_shared_channel(stage, 100)
    view = NewPalletView()
    interaction = _FakeInteraction(roles=[admin_role])

    asyncio.run(_button_callback(view)(interaction))

    assert len(interaction.response.modals) == 1
    assert interaction.response.modals[0].charge_id is None


def test_ready_package_shown_when_available(fresh_db, no_admin_role):
    for stage in config.SHARED_STAGE_CHANNELS:
        fresh_db.set_shared_channel(stage, 100)
    charge_id = fresh_db.create_manual_invoice_charge(200.0, submitted_by=1)
    fresh_db.upsert_manifest_lot(charge_id, 200.0, 400.0, "m.csv", 1, [
        {"sku": "A1", "product": "Widget", "retail_price": 200.0},
        {"sku": "A1", "product": "Widget", "retail_price": 200.0},
    ])
    view = NewPalletView()
    interaction = _FakeInteraction()

    asyncio.run(_button_callback(view)(interaction))

    content, kwargs = interaction.response.messages[-1]
    assert isinstance(kwargs["view"], ReadyPackageView)


def test_ready_package_select_carries_charge_id_into_modal(fresh_db):
    charge_id = fresh_db.create_manual_invoice_charge(150.0, submitted_by=1)
    fresh_db.upsert_manifest_lot(charge_id, 150.0, 300.0, "m.csv", 1, [
        {"sku": "A1", "product": "Widget", "retail_price": 150.0},
    ])
    charges = fresh_db.get_ready_manifested_charges()
    select = ReadyPackageSelect(charges)
    select._values = [str(charge_id)]
    interaction = _FakeInteraction()

    asyncio.run(select.callback(interaction))

    assert len(interaction.response.modals) == 1
    assert interaction.response.modals[0].charge_id == charge_id


class _FakeAwaitingChannel:
    async def fetch_message(self, message_id):
        raise AssertionError("no message to fetch in this test")


class _FakeClient:
    def get_channel(self, channel_id):
        return None


def test_claim_charges_for_pallet_links_manifest_lot_to_pallet(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    pallet_id = fresh_db.create_pallet("Gate Test Pallet", category_id=333, created_by=1)
    charge_id = fresh_db.create_manual_invoice_charge(150.0, submitted_by=1)
    lot_id = fresh_db.upsert_manifest_lot(charge_id, 150.0, 300.0, "m.csv", 1, [
        {"sku": "A1", "product": "Widget", "retail_price": 150.0},
    ])

    claimed_total = asyncio.run(
        _claim_charges_for_pallet(_FakeClient(), pallet_id, "Gate Test Pallet", [str(charge_id)], actor_id=1)
    )

    assert claimed_total == 150.0
    costs = fresh_db.get_pallet_costs(pallet_id)
    assert len(costs) == 1
    assert costs[0]["cost_type"] == "invoice"
    lot = fresh_db.get_manifest_lot(lot_id)
    assert lot["pallet_id"] == pallet_id


def test_claim_charges_for_pallet_never_calls_quickbooks_for_manual_source(fresh_db, monkeypatch):
    create_expense = AsyncMock(side_effect=AssertionError("no QuickBooks push for a manual invoice"))
    monkeypatch.setattr(qb, "create_expense", create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    pallet_id = fresh_db.create_pallet("Gate Test Pallet 2", category_id=334, created_by=1)
    charge_id = fresh_db.create_manual_invoice_charge(50.0, submitted_by=1)

    asyncio.run(_claim_charges_for_pallet(_FakeClient(), pallet_id, "Gate Test Pallet 2", [str(charge_id)], actor_id=1))

    create_expense.assert_not_awaited()
