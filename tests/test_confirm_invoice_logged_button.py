"""
ConfirmInvoiceLoggedButton (cogs/finance.py) - the button on a manually-
submitted invoice's #awaiting-pallet-charges card, letting Finance
Management dismiss the card once they've entered it into QuickBooks by
hand. Only removes the Discord card - the underlying awaiting_pallet_charges
row is untouched, so a pallet can still claim it later regardless.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import cogs.finance as finance_module
from cogs.finance import ConfirmInvoiceLoggedButton


class _FakeRole:
    def __init__(self, name):
        self.name = name


_FINANCE_ROLE = _FakeRole(config.ROLE_FINANCE_MGMT)
_ADMIN_ROLE = _FakeRole(config.ROLE_ADMIN)


@pytest.fixture(autouse=True)
def role_lookup(monkeypatch):
    def resolve(guild, name):
        if name == config.ROLE_FINANCE_MGMT:
            return _FINANCE_ROLE
        if name == config.ROLE_ADMIN:
            return _ADMIN_ROLE
        return None
    monkeypatch.setattr(finance_module.runtime_settings, "resolve_role", resolve)


def _interaction(roles):
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(roles=roles),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(delete=AsyncMock()),
    )


def test_finance_management_can_dismiss_the_card():
    button = ConfirmInvoiceLoggedButton(charge_id=1)
    event = _interaction(roles=[_FINANCE_ROLE])

    asyncio.run(button.callback(event))

    event.response.defer.assert_awaited_once()
    event.message.delete.assert_awaited_once()
    event.followup.send.assert_awaited_once()


def test_admin_can_dismiss_the_card_too():
    button = ConfirmInvoiceLoggedButton(charge_id=1)
    event = _interaction(roles=[_ADMIN_ROLE])

    asyncio.run(button.callback(event))

    event.message.delete.assert_awaited_once()


def test_unauthorized_user_cannot_dismiss_the_card():
    button = ConfirmInvoiceLoggedButton(charge_id=1)
    event = _interaction(roles=[])

    asyncio.run(button.callback(event))

    event.message.delete.assert_not_awaited()
    event.response.send_message.assert_awaited_once()
    assert "Finance Management" in event.response.send_message.call_args.args[0]


def test_dismissing_survives_an_already_deleted_message():
    button = ConfirmInvoiceLoggedButton(charge_id=1)
    event = _interaction(roles=[_FINANCE_ROLE])
    import discord
    event.message.delete = AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason="x"), "gone"))

    asyncio.run(button.callback(event))

    event.followup.send.assert_awaited_once()


def test_custom_id_round_trips_the_charge_id():
    button = ConfirmInvoiceLoggedButton(charge_id=42)
    assert button.item.custom_id == "invoice_confirmed:42"
