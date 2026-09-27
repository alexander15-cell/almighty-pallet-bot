"""
Regression guard: several /finance commands (setprice, record-sale, refund,
expense, reverse-sale, override-count, clear-count-override, refresh-card)
used to send their FIRST response to the interaction only at the very end,
after already calling finance_utils.refresh_finance_message - which does a
real fetch_message + msg.edit Discord round trip on the pallet's pinned
finance card. Under load that can take longer than the 3 seconds Discord
allows for a slash command's first response, causing a real "Unknown
interaction" (error 10062) failure in production - the same bug class fixed
in cogs/item_flow.py (see test_ebay_approval_interaction_timing.py).

Each of these commands must defer immediately once its fast validity checks
pass, then use a followup for the final confirmation - never respond to the
interaction directly after refresh_finance_message (or, for the QuickBooks
allocate button, after the QuickBooks API call) has started.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import cogs.finance as finance_module
from cogs.finance import Finance


class _FakeRole:
    pass


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()
    monkeypatch.setattr(finance_module.runtime_settings, "resolve_role", lambda guild, name: role)
    return role


@pytest.fixture
def cog(monkeypatch):
    c = Finance.__new__(Finance)
    c.bot = SimpleNamespace()
    monkeypatch.setattr(finance_module.finance_utils, "refresh_finance_message", AsyncMock())
    return c


def interaction(pallet_category_id, role):
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(category_id=pallet_category_id),
        user=SimpleNamespace(id=42, roles=[role]),
        guild=object(),
    )


def _pallet(fresh_db):
    return fresh_db.create_pallet("Timing Pallet", category_id=42, created_by=1)


@pytest.mark.parametrize("method,args", [
    ("setprice", (100.0,)),
    ("override_count", (5,)),
    ("clear_count_override", ()),
    ("refresh_card", ()),
])
def test_pallet_level_commands_defer_before_refresh_and_use_followup(fresh_db, cog, bypass_role_check, method, args):
    _pallet(fresh_db)
    event = interaction(42, bypass_role_check)
    asyncio.run(getattr(Finance, method).callback(cog, event, *args))

    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    finance_module.finance_utils.refresh_finance_message.assert_awaited_once()
    event.followup.send.assert_awaited_once()


@pytest.mark.parametrize("method,args_factory", [
    ("record_sale", lambda item_number: (item_number, 25.0, "eBay")),
    ("refund", lambda item_number: (item_number, 5.0, "damaged")),
    ("expense", lambda item_number: (10.0, "packaging", None)),
])
def test_item_level_commands_defer_before_refresh_and_use_followup(fresh_db, cog, bypass_role_check, method, args_factory):
    pallet_id = _pallet(fresh_db)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    event = interaction(42, bypass_role_check)
    asyncio.run(getattr(Finance, method).callback(cog, event, *args_factory(item["item_number"])))

    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    finance_module.finance_utils.refresh_finance_message.assert_awaited_once()
    event.followup.send.assert_awaited_once()


def test_reverse_sale_defers_before_refresh_and_uses_followup(fresh_db, cog, bypass_role_check):
    pallet_id = _pallet(fresh_db)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.record_item_sale(item_id, 30.0, "eBay", actor_id=1)
    item = fresh_db.get_item(item_id)
    event = interaction(42, bypass_role_check)
    asyncio.run(Finance.reverse_sale.callback(cog, event, item["item_number"], "duplicate entry"))

    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    finance_module.finance_utils.refresh_finance_message.assert_awaited_once()
    event.followup.send.assert_awaited_once()


def test_allocate_charge_button_defers_before_quickbooks_lookup_and_uses_followup(fresh_db, monkeypatch):
    monkeypatch.setattr(finance_module.db, "has_quickbooks_txn_been_allocated", lambda txn_id: False)
    monkeypatch.setattr(finance_module.db, "get_awaiting_pallet_charge_by_txn", lambda txn_id: None)
    monkeypatch.setattr(finance_module.db, "get_all_pallets", lambda include_archived=False: [])
    monkeypatch.setattr(
        finance_module.quickbooks, "get_transaction",
        AsyncMock(return_value={"amount": 12.34, "merchant": "Depot", "date": "2026-01-01"}),
    )

    button = finance_module.AllocateChargeButton.__new__(finance_module.AllocateChargeButton)
    button.txn_id = "txn-1"

    event = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=object(),
    )
    asyncio.run(finance_module.AllocateChargeButton.callback(button, event))

    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    finance_module.quickbooks.get_transaction.assert_awaited_once()
    event.followup.send.assert_awaited_once()
