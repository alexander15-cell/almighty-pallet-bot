"""
Finance.on_message (cogs/finance.py) - #submit-invoices, the manual
replacement for the QuickBooks credit-card poller (unavailable without API
access): Purchase Management attaches an invoice photo/PDF and types just
the dollar amount, and the bot files a manually-submitted
awaiting_pallet_charges row and posts the accountant's standing card in
#awaiting-pallet-charges, same as a QuickBooks-sourced charge.
"""
import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import cogs.finance as finance_module
from cogs.finance import Finance


class _FakeRole:
    def __init__(self, name):
        self.name = name


class _FakeAttachment:
    def __init__(self, filename="invoice.jpg", data=b"fake-invoice-bytes"):
        self.filename = filename
        self._data = data
        self.saved_to = None

    async def save(self, dest):
        self.saved_to = Path(dest)
        Path(dest).write_bytes(self._data)


class _FakeMessage:
    def __init__(self, msg_id=2000):
        self.id = msg_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []

    async def send(self, *args, **kwargs):
        content = args[0] if args else kwargs.get("content")
        self.sent.append(content)
        return _FakeMessage()


class _FakeAuthor:
    def __init__(self, user_id=1, roles=None):
        self.id = user_id
        self.bot = False
        self.roles = roles or []


class _FakeInvoiceMessage:
    def __init__(self, channel, content="125.50", attachments=None, author_roles=None):
        self.channel = channel
        self.content = content
        self.attachments = attachments if attachments is not None else [_FakeAttachment()]
        self.author = _FakeAuthor(roles=author_roles or [])
        self.guild = object()
        self.replies = []

    async def reply(self, content=None, **kwargs):
        self.replies.append(content)

    async def delete(self):
        pass


class _FakeBot:
    def __init__(self, channels: dict):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    purchase_role = _FakeRole(config.ROLE_PURCHASE_MGMT)
    monkeypatch.setattr(
        finance_module.runtime_settings, "resolve_role",
        lambda guild, name: purchase_role if name == config.ROLE_PURCHASE_MGMT else None,
    )
    return purchase_role


@pytest.fixture
def submit_channel(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(finance_module, "INVOICE_DIR", tmp_path)
    fresh_db.set_shared_channel("submit-invoices", 500)
    fresh_db.set_shared_channel("awaiting-pallet-charges", 600)
    awaiting_channel = _FakeChannel(600)
    channel = _FakeChannel(500)
    channel.id = 500
    bot = _FakeBot({500: channel, 600: awaiting_channel})
    cog = Finance.__new__(Finance)
    cog.bot = bot
    return cog, channel, awaiting_channel


def test_valid_submission_creates_charge_and_posts_card(fresh_db, submit_channel, bypass_role_check):
    cog, channel, awaiting_channel = submit_channel
    message = _FakeInvoiceMessage(channel, content="125.50", author_roles=[bypass_role_check])

    asyncio.run(cog.on_message(message))

    unclaimed = fresh_db.get_unclaimed_pallet_charges()
    assert len(unclaimed) == 1
    assert unclaimed[0]["amount"] == 125.50
    assert unclaimed[0]["source"] == "manual"
    assert unclaimed[0]["invoice_photo_path"]
    assert Path(unclaimed[0]["invoice_photo_path"]).is_file()
    assert len(awaiting_channel.sent) == 1
    assert len(channel.sent) == 1
    assert "125.50" in channel.sent[0]


def test_missing_role_is_rejected(fresh_db, submit_channel):
    cog, channel, awaiting_channel = submit_channel
    message = _FakeInvoiceMessage(channel, content="125.50", author_roles=[])

    asyncio.run(cog.on_message(message))

    assert fresh_db.get_unclaimed_pallet_charges() == []
    assert "Purchase Management" in message.replies[-1]


def test_missing_attachment_is_rejected(fresh_db, submit_channel, bypass_role_check):
    cog, channel, awaiting_channel = submit_channel
    message = _FakeInvoiceMessage(channel, content="125.50", attachments=[], author_roles=[bypass_role_check])

    asyncio.run(cog.on_message(message))

    assert fresh_db.get_unclaimed_pallet_charges() == []
    assert "Attach the invoice" in message.replies[-1]


def test_invalid_amount_is_rejected(fresh_db, submit_channel, bypass_role_check):
    cog, channel, awaiting_channel = submit_channel
    message = _FakeInvoiceMessage(channel, content="not a price", author_roles=[bypass_role_check])

    asyncio.run(cog.on_message(message))

    assert fresh_db.get_unclaimed_pallet_charges() == []
    assert "dollar amount" in message.replies[-1]


def test_wrong_channel_is_ignored(fresh_db, submit_channel, bypass_role_check):
    cog, channel, awaiting_channel = submit_channel
    other_channel = _FakeChannel(999)
    message = _FakeInvoiceMessage(other_channel, content="125.50", author_roles=[bypass_role_check])

    asyncio.run(cog.on_message(message))

    assert fresh_db.get_unclaimed_pallet_charges() == []
    assert message.replies == []
