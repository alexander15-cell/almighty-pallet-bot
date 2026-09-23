"""
The Discord-facing half of the Pirate Ship shipping-cost import:
_handle_unmatched_shipping_assignment (the manual picker for rows that
couldn't be auto-matched) and Finance.import_pirateship (the command
itself, calling its underlying callback directly - see
tests/test_quickbooks_poll.py for the same "call the wrapped coroutine
directly" approach used for tasks.loop). Uses minimal fake Discord objects,
matching this codebase's existing test style.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import cogs.finance as finance_module
from cogs.finance import Finance, _handle_unmatched_shipping_assignment


class _FakeResponse:
    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, *args, **kwargs):
        pass


class _FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, content=None, **kwargs):
        self.calls.append({"content": content, **kwargs})


class _FakeUser:
    def __init__(self, user_id=1):
        self.id = user_id


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = _FakeUser()
        self.client = object()


class _FakeAttachment:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self):
        return self._content


class _FakeBot:
    def get_channel(self, channel_id):
        return None


@pytest.fixture
def pallet(fresh_db):
    pallet_id = fresh_db.create_pallet("Shipping Test Pallet", category_id=333, created_by=1)
    return fresh_db.get_pallet(pallet_id)


@pytest.fixture(autouse=True)
def bypass_admin_check(monkeypatch):
    # These tests exercise the command's CSV-matching/allocation logic, not
    # the role check itself - the fake interaction has no real guild/roles
    # for runtime_settings.resolve_role to inspect.
    monkeypatch.setattr(finance_module, "_is_admin", lambda interaction: True)


# ------------------------------------------------------- manual assignment


def test_assign_unmatched_row_creates_shipping_cost(pallet, fresh_db):
    interaction = _FakeInteraction()
    asyncio.run(_handle_unmatched_shipping_assignment(interaction, "some-order-ref", 8.75, str(pallet["id"])))

    costs = fresh_db.get_pallet_costs(pallet["id"])
    assert len(costs) == 1
    assert costs[0]["amount"] == 8.75
    assert costs[0]["cost_type"] == "shipping"
    assert costs[0]["source"] == "pirateship"
    assert costs[0]["item_id"] is None  # manual assignment is pallet-level, no specific item known


def test_assign_to_nonexistent_pallet_does_nothing(fresh_db):
    interaction = _FakeInteraction()
    asyncio.run(_handle_unmatched_shipping_assignment(interaction, "ref", 8.75, "999999"))

    assert "no longer exists" in interaction.followup.calls[-1]["content"]


# --------------------------------------------------------------- full import


def test_import_pirateship_allocates_matched_rows_to_items(pallet, fresh_db):
    item_id = fresh_db.create_item(pallet["id"], "widget", [], 1)
    item = fresh_db.get_item(item_id)

    csv_text = f"Order Number,Cost\npallet-{pallet['id']}-item-{item['item_number']},12.34\n"
    interaction = _FakeInteraction()
    cog = Finance.__new__(Finance)
    cog.bot = _FakeBot()

    asyncio.run(Finance.import_pirateship.callback(
        cog, interaction, _FakeAttachment("shipping.csv", csv_text.encode("utf-8"))
    ))

    costs = fresh_db.get_pallet_costs(pallet["id"])
    assert len(costs) == 1
    assert costs[0]["item_id"] == item_id
    assert costs[0]["amount"] == 12.34
    assert "Allocated shipping cost to 1 item" in interaction.followup.calls[-1]["content"]


def test_import_pirateship_treats_reference_to_missing_item_as_unmatched(fresh_db):
    csv_text = "Order Number,Cost\npallet-99999-item-1,5.00\n"
    interaction = _FakeInteraction()
    cog = Finance.__new__(Finance)
    cog.bot = _FakeBot()

    asyncio.run(Finance.import_pirateship.callback(
        cog, interaction, _FakeAttachment("shipping.csv", csv_text.encode("utf-8"))
    ))

    reply = interaction.followup.calls[-1]["content"]
    assert "Allocated shipping cost to 0 item" in reply
    assert "1 row(s) couldn't be matched" in reply


def test_import_pirateship_rejects_non_csv_attachment(fresh_db):
    interaction = _FakeInteraction()
    cog = Finance.__new__(Finance)
    cog.bot = _FakeBot()

    asyncio.run(Finance.import_pirateship.callback(
        cog, interaction, _FakeAttachment("shipping.xlsx", b"not a csv")
    ))

    assert interaction.followup.calls == []  # rejected before defer/followup
