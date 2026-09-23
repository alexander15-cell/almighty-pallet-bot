"""
The credit-card charge -> pallet allocation workflow (cogs/finance.py):
_handle_allocation_choice (the logic behind the Allocate button's pallet
picker) and _post_credit_card_charge/_post_awaiting_charge_card (posting
cards into the two finance channels). Uses minimal fake Discord objects
rather than a real Client, matching this codebase's existing preference for
DB-level tests over full Discord mocking (see test_item_duplicate.py).
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import config
import database as db
import quickbooks as qb
from cogs.finance import (
    _handle_allocation_choice,
    _post_credit_card_charge,
    _post_awaiting_charge_card,
)


class _FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, ephemeral=True, thinking=True):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        pass


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append(content)


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id
        self.mention = f"<@{user_id}>"


class _FakeMessage:
    def __init__(self, msg_id=1):
        self.id = msg_id
        self.embeds = [discord.Embed(title="charge")]
        self.edited = []

    async def edit(self, **kwargs):
        self.edited.append(kwargs)


class _FakeChannel:
    def __init__(self, channel_id=555):
        self.id = channel_id
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeMessage(msg_id=9999)


class _FakeClient:
    def __init__(self, channels=None):
        self._channels = channels or {}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeInteraction:
    def __init__(self, client, user_id=42):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = _FakeUser(user_id)
        self.client = client


@pytest.fixture
def pallet(fresh_db):
    pallet_id = fresh_db.create_pallet("Test Pallet", category_id=111, created_by=1)
    return fresh_db.get_pallet(pallet_id)


# --------------------------------------------------------- allocate to pallet


def test_allocate_to_existing_pallet_creates_pallet_cost_row(pallet, fresh_db, monkeypatch):
    async def fake_create_expense(*args, **kwargs):
        return {"id": "qb-purchase-1"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    client = _FakeClient()
    interaction = _FakeInteraction(client)
    message = _FakeMessage()

    asyncio.run(_handle_allocation_choice(
        interaction, "txn-1", 42.50, "Home Depot", "2026-09-20", message, str(pallet["id"]),
    ))

    costs = fresh_db.get_pallet_costs(pallet["id"])
    assert len(costs) == 1
    assert costs[0]["amount"] == 42.50
    assert costs[0]["cost_type"] == "credit_card"
    assert costs[0]["source"] == "quickbooks"
    assert costs[0]["quickbooks_txn_id"] == "txn-1"

    fin = fresh_db.get_pallet_financials(pallet["id"])
    assert fin["cost"] == 42.50
    assert fin["itemized_costs_total"] == 42.50

    # Original charge card gets marked handled and loses its button.
    assert len(message.edited) == 1
    assert message.edited[0]["view"] is None
    assert "Allocated" in message.edited[0]["embed"].fields[-1].value


def test_allocate_to_existing_pallet_surfaces_quickbooks_push_failure_but_still_allocates(pallet, fresh_db, monkeypatch):
    async def fake_create_expense(*args, **kwargs):
        raise qb.QuickBooksError("boom")
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    interaction = _FakeInteraction(_FakeClient())
    message = _FakeMessage()

    asyncio.run(_handle_allocation_choice(
        interaction, "txn-2", 10.0, "Staples", "2026-09-20", message, str(pallet["id"]),
    ))

    # The allocation itself still happened even though the QuickBooks push failed.
    assert len(fresh_db.get_pallet_costs(pallet["id"])) == 1
    assert any("QuickBooks" in (m or "") for m in interaction.followup.messages)


def test_allocate_to_nonexistent_pallet_does_not_create_cost(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    interaction = _FakeInteraction(_FakeClient())
    message = _FakeMessage()

    asyncio.run(_handle_allocation_choice(
        interaction, "txn-3", 10.0, "Staples", "2026-09-20", message, "999999",
    ))

    assert "no longer exists" in interaction.followup.messages[-1]
    assert message.edited == []


# ------------------------------------------------------------ new pallet path


def test_allocate_to_new_pallet_files_awaiting_charge_and_posts_card(fresh_db):
    channel = _FakeChannel(channel_id=555)
    fresh_db.set_shared_channel("awaiting-pallet-charges", 555)
    client = _FakeClient(channels={555: channel})
    interaction = _FakeInteraction(client, user_id=7)
    message = _FakeMessage()

    asyncio.run(_handle_allocation_choice(
        interaction, "txn-4", 99.99, "U-Haul", "2026-09-19", message, "__new_pallet__",
    ))

    unclaimed = fresh_db.get_unclaimed_pallet_charges()
    assert len(unclaimed) == 1
    assert unclaimed[0]["quickbooks_txn_id"] == "txn-4"
    assert unclaimed[0]["amount"] == 99.99
    assert unclaimed[0]["allocated_by"] == 7

    assert len(channel.sent) == 1
    assert unclaimed[0]["message_id"] == 9999

    assert len(message.edited) == 1
    assert "awaiting-pallet-charges" in message.edited[0]["embed"].fields[-1].value


def test_allocate_to_new_pallet_without_channel_configured_does_not_crash(fresh_db):
    interaction = _FakeInteraction(_FakeClient())
    message = _FakeMessage()

    asyncio.run(_handle_allocation_choice(
        interaction, "txn-5", 5.0, "Merchant", "2026-09-19", message, "__new_pallet__",
    ))

    # Row still exists in the DB even though there was nowhere to post its card.
    assert len(fresh_db.get_unclaimed_pallet_charges()) == 1


# --------------------------------------------------------------- double-alloc


def test_double_allocation_is_rejected(pallet, fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    async def fake_create_expense(*args, **kwargs):
        return {"id": "x"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)

    interaction1 = _FakeInteraction(_FakeClient())
    message1 = _FakeMessage()
    asyncio.run(_handle_allocation_choice(
        interaction1, "txn-6", 20.0, "Merchant", "2026-09-19", message1, str(pallet["id"]),
    ))

    interaction2 = _FakeInteraction(_FakeClient())
    message2 = _FakeMessage()
    asyncio.run(_handle_allocation_choice(
        interaction2, "txn-6", 20.0, "Merchant", "2026-09-19", message2, str(pallet["id"]),
    ))

    assert "already allocated" in interaction2.followup.messages[-1]
    # Only one pallet_costs row exists - the second attempt never wrote anything.
    assert len(fresh_db.get_pallet_costs(pallet["id"])) == 1


def test_already_awaiting_charge_blocks_reallocation(fresh_db):
    fresh_db.create_awaiting_pallet_charge("txn-7", 30.0, "Merchant", "2026-09-19", allocated_by=1)

    interaction = _FakeInteraction(_FakeClient())
    message = _FakeMessage()
    asyncio.run(_handle_allocation_choice(
        interaction, "txn-7", 30.0, "Merchant", "2026-09-19", message, "__new_pallet__",
    ))

    assert "already allocated" in interaction.followup.messages[-1]
    assert len(fresh_db.get_unclaimed_pallet_charges()) == 1  # not duplicated


# ------------------------------------------------------------ posting charges


def test_post_credit_card_charge_without_channel_configured_logs_and_skips(fresh_db):
    client = _FakeClient()
    asyncio.run(_post_credit_card_charge(client, {"id": "t1", "merchant": "M", "amount": 5.0, "date": "2026-09-19"}))
    # No exception - nothing to assert beyond "it didn't crash".


def test_post_credit_card_charge_sends_embed_with_allocate_button(fresh_db):
    channel = _FakeChannel(channel_id=321)
    fresh_db.set_shared_channel("credit-card-charges", 321)
    client = _FakeClient(channels={321: channel})

    asyncio.run(_post_credit_card_charge(client, {"id": "t2", "merchant": "M", "amount": 5.0, "date": "2026-09-19"}))

    assert len(channel.sent) == 1
    view = channel.sent[0]["view"]
    assert len(view.children) == 1
    assert view.children[0].item.custom_id == "qb_allocate:t2"
