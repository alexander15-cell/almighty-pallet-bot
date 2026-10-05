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
import finance_utils
import quickbooks as qb
from cogs.finance import (
    _handle_allocation_choice,
    _post_credit_card_charge,
    _post_awaiting_charge_card,
    PalletAllocateSelect,
    ExpenseAccountSelect,
    ExpenseAccountSelectView,
)


class _FakeResponse:
    def __init__(self):
        self.deferred = False
        self.edited_content = None
        self.edited_view = None

    async def defer(self, ephemeral=True, thinking=True):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        pass

    async def edit_message(self, content=None, view=None, **kwargs):
        self.edited_content = content
        self.edited_view = view


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
        self.sent_args = []

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs)
        self.sent_args.append(args)
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


# ------------------------------------------------------- expense account picker


def test_choosing_a_real_pallet_shows_the_account_picker_not_immediate_allocation(fresh_db, pallet, monkeypatch):
    async def fake_list_accounts(account_types):
        assert account_types == ["Expense", "Cost of Goods Sold"]
        return [{"id": "10", "name": "Office Supplies", "type": "Expense", "balance": 0.0}]
    monkeypatch.setattr(qb, "list_accounts", fake_list_accounts)

    select = PalletAllocateSelect("txn-8", 15.0, "Staples", "2026-09-20", _FakeMessage(), [pallet])
    monkeypatch.setattr(type(select), "values", property(lambda self: [str(pallet["id"])]))
    interaction = _FakeInteraction(_FakeClient())

    asyncio.run(select.callback(interaction))

    assert isinstance(interaction.response.edited_view, ExpenseAccountSelectView)
    # Nothing allocated yet - only picking an account (or "don't categorize") finishes it.
    assert fresh_db.get_pallet_costs(pallet["id"]) == []


def test_choosing_new_pallet_skips_the_account_picker(fresh_db, monkeypatch):
    select = PalletAllocateSelect("txn-9", 15.0, "Staples", "2026-09-20", _FakeMessage(), [])
    monkeypatch.setattr(type(select), "values", property(lambda self: ["__new_pallet__"]))
    interaction = _FakeInteraction(_FakeClient())

    asyncio.run(select.callback(interaction))

    assert interaction.response.edited_view is None  # went straight to allocation
    assert len(fresh_db.get_unclaimed_pallet_charges()) == 1


def test_account_picker_falls_back_to_no_accounts_on_quickbooks_error(fresh_db, pallet, monkeypatch):
    async def fake_list_accounts(account_types):
        raise qb.QuickBooksError("down")
    monkeypatch.setattr(qb, "list_accounts", fake_list_accounts)

    select = PalletAllocateSelect("txn-10", 15.0, "Staples", "2026-09-20", _FakeMessage(), [pallet])
    monkeypatch.setattr(type(select), "values", property(lambda self: [str(pallet["id"])]))
    interaction = _FakeInteraction(_FakeClient())

    asyncio.run(select.callback(interaction))  # must not raise

    view = interaction.response.edited_view
    assert isinstance(view, ExpenseAccountSelectView)


def test_picking_the_default_option_allocates_to_cogs_account(pallet, fresh_db, monkeypatch):
    """Cash basis: picking the default option (no explicit override) still
    categorizes the expense, to QUICKBOOKS_COGS_ACCOUNT_ID - a pallet
    purchase's cost is always recognized immediately, never left
    uncategorized the way the old "Don't categorize" default used to."""
    calls = []
    async def fake_create_expense(*args, **kwargs):
        calls.append(kwargs)
        return {"id": "x"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    monkeypatch.setattr(config, "QUICKBOOKS_COGS_ACCOUNT_ID", "48")

    account_select = ExpenseAccountSelect(
        "txn-11", 20.0, "Staples", "2026-09-20", _FakeMessage(), pallet["id"], accounts=[],
    )
    monkeypatch.setattr(type(account_select), "values", property(lambda self: ["__none__"]))
    interaction = _FakeInteraction(_FakeClient())

    asyncio.run(account_select.callback(interaction))

    assert calls[0]["expense_account_ref"] == "48"
    assert len(fresh_db.get_pallet_costs(pallet["id"])) == 1


def test_picking_a_specific_account_passes_its_id_to_quickbooks(pallet, fresh_db, monkeypatch):
    calls = []
    async def fake_create_expense(*args, **kwargs):
        calls.append(kwargs)
        return {"id": "x"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    accounts = [{"id": "55", "name": "Shipping Supplies", "type": "Expense", "balance": 0.0}]
    account_select = ExpenseAccountSelect(
        "txn-12", 20.0, "Staples", "2026-09-20", _FakeMessage(), pallet["id"], accounts=accounts,
    )
    monkeypatch.setattr(type(account_select), "values", property(lambda self: ["55"]))
    interaction = _FakeInteraction(_FakeClient())

    asyncio.run(account_select.callback(interaction))

    assert calls[0]["expense_account_ref"] == "55"


# --------------------------------------------------------------- audit log


def test_allocating_to_a_pallet_posts_to_the_audit_log(pallet, fresh_db, monkeypatch):
    async def fake_create_expense(*args, **kwargs):
        return {"id": "x"}
    monkeypatch.setattr(qb, "create_expense", fake_create_expense)
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    audit_channel = _FakeChannel(channel_id=444)
    fresh_db.set_shared_channel("finance-audit-log", 444)
    client = _FakeClient(channels={444: audit_channel})

    asyncio.run(_handle_allocation_choice(
        _FakeInteraction(client), "txn-13", 12.0, "Staples", "2026-09-20", _FakeMessage(), str(pallet["id"]),
        expense_account_ref="55", expense_account_name="Shipping Supplies",
    ))

    assert len(audit_channel.sent_args) == 1
    assert "Shipping Supplies" in audit_channel.sent_args[0][0]


def test_filing_under_new_pallet_posts_to_the_audit_log(fresh_db):
    audit_channel = _FakeChannel(channel_id=445)
    fresh_db.set_shared_channel("finance-audit-log", 445)
    client = _FakeClient(channels={445: audit_channel})

    asyncio.run(_handle_allocation_choice(
        _FakeInteraction(client), "txn-14", 8.0, "U-Haul", "2026-09-20", _FakeMessage(), "__new_pallet__",
    ))

    assert len(audit_channel.sent) == 1
