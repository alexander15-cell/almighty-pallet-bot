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
    monkeypatch.setattr(config, "QUICKBOOKS_BANK_ACCOUNT_ID", "120")
    monkeypatch.setattr(config, "QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID", "24")
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "")
    names = {"120": "Old National Checking", "24": "Undeposited funds"}

    async def fake_get_account_balance(account_id):
        return {"id": account_id, "name": names[account_id], "balance": 42.0}
    monkeypatch.setattr(qb, "get_account_balance", fake_get_account_balance)

    async def fake_pl(start, end):
        assert start.endswith("-01-01")
        return {"income": 122.66, "cogs": 2239.31, "gross_profit": -2116.65, "expenses": 61.61,
                "other_income": 0.0, "other_expenses": 0.0, "net_income": -2178.26}
    monkeypatch.setattr(qb, "get_profit_and_loss", fake_pl)

    embed = asyncio.run(finance_utils.build_dashboard_embed())

    fields = {f.name: f.value for f in embed.fields}
    assert fields["Old National Checking"] == "$42.00"  # the bank's own name, not "Cash"
    assert fields["Undeposited Funds"] == "$42.00"
    assert "Credit Card" not in fields
    assert "Cost of Goods Sold" not in fields  # P&L accounts have no balance in QuickBooks
    ytd = next(v for k, v in fields.items() if "Year-to-Date" in k)
    assert "Sales: $122.66" in ytd
    assert "Pallet purchases (COGS): $2,239.31" in ytd
    assert "Fees & other expenses: $61.61" in ytd
    assert "Net income: $-2,178.26" in ytd


def test_dashboard_profit_and_loss_failure_keeps_the_balances(fresh_db, monkeypatch):
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "")

    async def fake_get_account_balance(account_id):
        return {"id": account_id, "name": "Cash", "balance": 5.0}
    monkeypatch.setattr(qb, "get_account_balance", fake_get_account_balance)

    async def failing_pl(start, end):
        raise qb.QuickBooksError("boom")
    monkeypatch.setattr(qb, "get_profit_and_loss", failing_pl)

    embed = asyncio.run(finance_utils.build_dashboard_embed())
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Undeposited Funds"] == "$5.00"
    assert "Couldn't fetch" in fields["Year-to-Date"]


def test_profit_and_loss_report_is_parsed(monkeypatch):
    report = {"Rows": {"Row": [
        {"group": "Income", "Summary": {"ColData": [{"value": "Total Income"}, {"value": "122.66"}]}},
        {"group": "COGS", "Summary": {"ColData": [{"value": "Total COGS"}, {"value": "2239.31"}]}},
        {"group": "GrossProfit", "Summary": {"ColData": [{"value": "Gross Profit"}, {"value": "-2116.65"}]}},
        {"group": "Expenses", "Summary": {"ColData": [{"value": "Total Expenses"}, {"value": "61.61"}]}},
        {"group": "NetIncome", "Summary": {"ColData": [{"value": "Net Income"}, {"value": "-2178.26"}]}},
        {"Header": {"ColData": []}},
    ]}}
    seen = {}

    async def fake_request(method, path, params=None, json_body=None):
        seen.update(path=path, params=params)
        return report
    monkeypatch.setattr(qb, "_request", fake_request)

    pl = asyncio.run(qb.get_profit_and_loss("2026-01-01", "2026-10-13"))
    assert seen["path"] == "reports/ProfitAndLoss"
    assert seen["params"]["accounting_method"] == "Cash"
    assert pl["income"] == 122.66 and pl["cogs"] == 2239.31 and pl["expenses"] == 61.61
    assert pl["net_income"] == -2178.26 and pl["other_expenses"] == 0.0


class _Msg:
    def __init__(self):
        self.edited = None

    async def edit(self, embed=None):
        self.edited = embed


class _Channel:
    def __init__(self, existing=None):
        self.existing = existing
        self.sent = []

    async def fetch_message(self, message_id):
        if self.existing is None:
            import discord
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "nf"})(), "gone")
        return self.existing

    async def send(self, embed=None):
        msg = _Msg()
        msg.id = 999

        async def pin(reason=None):
            msg.pinned = True
        msg.pin = pin
        self.sent.append(msg)
        return msg


class _Bot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


def _setup_dashboard(fresh_db, monkeypatch, channel):
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    monkeypatch.setattr(fresh_db, "get_shared_channel_id", lambda name: 55 if name == "finance-dashboard" else None)
    fresh_db.set_dashboard_message_id(123)
    return _Bot(channel)


def test_refresh_edits_the_existing_message(fresh_db, monkeypatch):
    msg = _Msg()
    bot = _setup_dashboard(fresh_db, monkeypatch, _Channel(existing=msg))
    assert asyncio.run(finance_utils.refresh_dashboard_message(bot)) == "updated"
    assert msg.edited is not None


def test_refresh_reposts_and_pins_only_when_asked(fresh_db, monkeypatch):
    channel = _Channel(existing=None)
    bot = _setup_dashboard(fresh_db, monkeypatch, channel)

    assert asyncio.run(finance_utils.refresh_dashboard_message(bot)) == "missing"
    assert channel.sent == []

    assert asyncio.run(finance_utils.refresh_dashboard_message(bot, repost_if_missing=True)) == "reposted"
    assert len(channel.sent) == 1 and channel.sent[0].pinned
    assert fresh_db.get_dashboard_message_id() == 999


def test_dashboard_includes_pallet_progress(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 10.0)

    embed = asyncio.run(finance_utils.build_dashboard_embed())

    fields = {f.name: f.value for f in embed.fields}
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
