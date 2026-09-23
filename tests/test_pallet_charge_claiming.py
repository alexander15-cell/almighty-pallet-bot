"""
_claim_awaiting_charges (cogs/pallet_setup.py) - offered right after a new
pallet is created, lets whoever's setting it up attach any QuickBooks
charges that were filed under "New Pallet (not arrived yet)" before this
pallet existed. Uses minimal fake Discord objects, matching this codebase's
existing preference for DB-level tests over full Discord mocking.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import config
import quickbooks as qb
from cogs.pallet_setup import _claim_awaiting_charges


class _FakeResponse:
    async def defer(self, ephemeral=True, thinking=True):
        pass


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeUser:
    def __init__(self, user_id=1):
        self.id = user_id


class _FakeAwaitingMessage:
    def __init__(self):
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeAwaitingChannel:
    def __init__(self):
        self.messages = {}

    def add(self, message_id, message):
        self.messages[message_id] = message

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class _FakeClient:
    def __init__(self, channels=None):
        self._channels = channels or {}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeInteraction:
    def __init__(self, client):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = _FakeUser()
        self.client = client


@pytest.fixture
def pallet(fresh_db):
    pallet_id = fresh_db.create_pallet("Claim Test Pallet", category_id=222, created_by=1)
    return fresh_db.get_pallet(pallet_id)


def test_claim_single_charge_creates_pallet_cost_and_removes_card(pallet, fresh_db, monkeypatch):
    async def fake_create_expense(*args, **kwargs):
        return {"id": "qb-1"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    charge_id = fresh_db.create_awaiting_pallet_charge("txn-a", 55.0, "Freight Co", "2026-09-10", allocated_by=1)
    awaiting_msg = _FakeAwaitingMessage()
    channel = _FakeAwaitingChannel()
    channel.add(4242, awaiting_msg)
    fresh_db.set_awaiting_pallet_charge_message(charge_id, 4242)
    fresh_db.set_shared_channel("awaiting-pallet-charges", 321)

    interaction = _FakeInteraction(_FakeClient(channels={321: channel}))
    asyncio.run(_claim_awaiting_charges(interaction, pallet["id"], pallet["name"], [str(charge_id)]))

    costs = fresh_db.get_pallet_costs(pallet["id"])
    assert len(costs) == 1
    assert costs[0]["amount"] == 55.0
    assert costs[0]["cost_type"] == "credit_card"

    remaining_unclaimed = fresh_db.get_unclaimed_pallet_charges()
    assert remaining_unclaimed == []
    assert awaiting_msg.deleted is True


def test_claim_multiple_charges_sums_total(pallet, fresh_db, monkeypatch):
    async def fake_create_expense(*args, **kwargs):
        return {"id": "qb-1"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    id1 = fresh_db.create_awaiting_pallet_charge("txn-b1", 100.0, "Auction House", "2026-09-10", allocated_by=1)
    id2 = fresh_db.create_awaiting_pallet_charge("txn-b2", 25.0, "Auction House Fee", "2026-09-10", allocated_by=1)

    interaction = _FakeInteraction(_FakeClient())
    asyncio.run(_claim_awaiting_charges(interaction, pallet["id"], pallet["name"], [str(id1), str(id2)]))

    costs = fresh_db.get_pallet_costs(pallet["id"])
    assert len(costs) == 2
    assert sum(c["amount"] for c in costs) == 125.0
    assert "125.00" in interaction.followup.messages[-1]


def test_claim_with_no_selection_does_nothing(pallet, fresh_db):
    interaction = _FakeInteraction(_FakeClient())
    asyncio.run(_claim_awaiting_charges(interaction, pallet["id"], pallet["name"], []))

    assert fresh_db.get_pallet_costs(pallet["id"]) == []
    assert "No charges attached" in interaction.followup.messages[-1]


def test_claim_survives_quickbooks_push_failure(pallet, fresh_db, monkeypatch):
    async def fake_create_expense(*args, **kwargs):
        raise qb.QuickBooksError("network down")
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    charge_id = fresh_db.create_awaiting_pallet_charge("txn-c", 15.0, "Merchant", "2026-09-10", allocated_by=1)

    interaction = _FakeInteraction(_FakeClient())
    asyncio.run(_claim_awaiting_charges(interaction, pallet["id"], pallet["name"], [str(charge_id)]))

    # Local claim still happened even though the QuickBooks push failed.
    assert len(fresh_db.get_pallet_costs(pallet["id"])) == 1
