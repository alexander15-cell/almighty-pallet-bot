"""
#finance-dashboard (finance_utils.py's build_dashboard_embed/
post_initial_dashboard_message/refresh_dashboard_message) and
#finance-audit-log (post_to_finance_audit_log) - the business-wide
"position" snapshot and permanent transaction record requested alongside
/finance log-sale's per-pallet reporting. Both are best-effort: a missing
channel/message must never raise, since neither is worth crashing whatever
triggered the refresh/post.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import quickbooks as qb
import finance_utils


class _FakeMessage:
    def __init__(self, message_id=1):
        self.id = message_id
        self.edited_with = None
        self.pinned = False

    async def edit(self, **kwargs):
        self.edited_with = kwargs

    async def pin(self, reason=None):
        self.pinned = True


class _FakeChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.sent = []
        self._messages = {}

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        msg = _FakeMessage(message_id=len(self.sent) + 100)
        self._messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        return self._messages[message_id]


class _FakeBot:
    def __init__(self, channels: dict):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


# --------------------------------------------------------------- build_dashboard_embed


def test_dashboard_shows_not_connected_when_quickbooks_isnt(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_CLIENT_ID", None)
    monkeypatch.setattr(config, "QUICKBOOKS_CLIENT_SECRET", None)

    embed = asyncio.run(finance_utils.build_dashboard_embed())

    field_names = {f.name for f in embed.fields}
    assert "QuickBooks" in field_names
    assert "Cash" not in field_names


def test_dashboard_shows_live_account_balances_when_connected(fresh_db, monkeypatch):
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )

    async def fake_get_account_balance(account_id):
        return {"id": account_id, "name": f"Account {account_id}", "balance": 42.0}
    monkeypatch.setattr(qb, "get_account_balance", fake_get_account_balance)

    embed = asyncio.run(finance_utils.build_dashboard_embed())

    fields = {f.name: f.value for f in embed.fields}
    assert fields["Cash"] == "$42.00"
    assert fields["Undeposited Funds"] == "$42.00"
    assert fields["Cost of Goods Sold"] == "$42.00"


def test_dashboard_includes_business_wide_cogs_and_pallet_progress(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 10.0, 3.0, 3.0)

    embed = asyncio.run(finance_utils.build_dashboard_embed())

    fields = {f.name: f.value for f in embed.fields}
    assert fields["Total COGS Logged"] == "$3.00"
    assert "in progress" in fields["Pallets"]


# ---------------------------------------------------- post/refresh dashboard message


def test_post_initial_dashboard_message_sends_pins_and_stores_id(fresh_db):
    channel = _FakeChannel()

    asyncio.run(finance_utils.post_initial_dashboard_message(_FakeBot({}), channel))

    assert len(channel.sent) == 1
    msg = channel._messages[list(channel._messages)[0]]
    assert msg.pinned is True
    assert fresh_db.get_dashboard_message_id() == msg.id


def test_refresh_dashboard_message_is_a_noop_when_never_set_up(fresh_db):
    bot = _FakeBot({})
    asyncio.run(finance_utils.refresh_dashboard_message(bot))  # must not raise


def test_refresh_dashboard_message_edits_the_stored_message(fresh_db):
    channel = _FakeChannel(channel_id=7)
    fresh_db.set_shared_channel("finance-dashboard", 7)
    bot = _FakeBot({7: channel})

    asyncio.run(finance_utils.post_initial_dashboard_message(bot, channel))
    message_id = fresh_db.get_dashboard_message_id()
    msg = channel._messages[message_id]
    assert msg.edited_with is None

    asyncio.run(finance_utils.refresh_dashboard_message(bot))

    assert msg.edited_with is not None
    assert "embed" in msg.edited_with


def test_refresh_dashboard_message_respects_preserve_discord_history(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", True)
    channel = _FakeChannel(channel_id=7)
    fresh_db.set_shared_channel("finance-dashboard", 7)
    fresh_db.set_dashboard_message_id(999)
    bot = _FakeBot({7: channel})

    asyncio.run(finance_utils.refresh_dashboard_message(bot))  # must not raise or fetch anything


# --------------------------------------------------------------- audit log


def test_post_to_finance_audit_log_is_a_noop_when_channel_not_set_up(fresh_db):
    bot = _FakeBot({})
    asyncio.run(finance_utils.post_to_finance_audit_log(bot, "hello"))  # must not raise


def test_post_to_finance_audit_log_sends_the_text(fresh_db):
    channel = _FakeChannel(channel_id=9)
    fresh_db.set_shared_channel("finance-audit-log", 9)
    bot = _FakeBot({9: channel})

    asyncio.run(finance_utils.post_to_finance_audit_log(bot, "💰 Sale #1 logged"))

    assert channel.sent[0][0] == ("💰 Sale #1 logged",)
