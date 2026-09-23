"""
Finance.credit_card_poll_loop - the scheduled QuickBooks sync (see
cogs/finance.py). Calls the loop's raw coroutine directly (Loop.coro)
rather than starting the actual discord.ext.tasks.Loop, so this never
spins up a real background task or needs a live asyncio loop bound to a
Bot instance - matching this codebase's preference for exercising cog
logic directly instead of full Discord/task-loop machinery.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import config
import quickbooks as qb
from cogs.finance import Finance


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


class _FakeSelf:
    """Stands in for `self` inside credit_card_poll_loop.coro - only needs
    a `.bot` attribute, since the loop body only ever touches that."""
    def __init__(self, bot):
        self.bot = bot


@pytest.fixture
def channel_ready(fresh_db):
    fresh_db.set_shared_channel("credit-card-charges", 321)
    return _FakeChannel()


def test_poll_loop_noops_when_not_connected(fresh_db, monkeypatch, channel_ready):
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    bot = _FakeBot(channel_ready)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("should not have queried QuickBooks - not connected")
    monkeypatch.setattr(qb, "list_recent_transactions", fail_if_called)

    asyncio.run(Finance.credit_card_poll_loop.coro(_FakeSelf(bot)))
    assert channel_ready.sent == []


def test_poll_loop_noops_when_no_account_configured(fresh_db, monkeypatch, channel_ready):
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "")
    bot = _FakeBot(channel_ready)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("should not have queried QuickBooks - no account configured")
    monkeypatch.setattr(qb, "list_recent_transactions", fail_if_called)

    asyncio.run(Finance.credit_card_poll_loop.coro(_FakeSelf(bot)))
    assert channel_ready.sent == []


def test_poll_loop_posts_only_unseen_transactions(fresh_db, monkeypatch, channel_ready):
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")
    fresh_db.mark_quickbooks_txn_seen("already-seen")

    async def fake_list_recent_transactions(account_id, since=None, max_results=100):
        return [
            {"id": "already-seen", "date": "2026-09-19", "amount": 1.0, "merchant": "Old", "account_id": account_id},
            {"id": "brand-new", "date": "2026-09-20", "amount": 2.0, "merchant": "New", "account_id": account_id},
        ]
    monkeypatch.setattr(qb, "list_recent_transactions", fake_list_recent_transactions)

    bot = _FakeBot(channel_ready)
    asyncio.run(Finance.credit_card_poll_loop.coro(_FakeSelf(bot)))

    assert len(channel_ready.sent) == 1
    assert fresh_db.has_seen_quickbooks_txn("brand-new")


def test_poll_loop_marks_seen_before_posting_so_a_post_failure_never_reposts(fresh_db, monkeypatch, channel_ready):
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(config, "QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "77")

    async def fake_list_recent_transactions(account_id, since=None, max_results=100):
        return [{"id": "flaky", "date": "2026-09-20", "amount": 2.0, "merchant": "New", "account_id": account_id}]
    monkeypatch.setattr(qb, "list_recent_transactions", fake_list_recent_transactions)

    class _FakeResponse:
        status = 500
        reason = "Internal Server Error"

    async def broken_channel_send(**kwargs):
        raise discord.HTTPException(_FakeResponse(), "boom")
    channel_ready.send = broken_channel_send

    bot = _FakeBot(channel_ready)
    # Should not raise even though posting the card fails.
    asyncio.run(Finance.credit_card_poll_loop.coro(_FakeSelf(bot)))
    assert fresh_db.has_seen_quickbooks_txn("flaky")
