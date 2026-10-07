"""
The automatic AI-review retry backlog: database.py's ai_review_failed
column/get_items_needing_ai_retry, and cogs/item_flow.py's
ai_retry_backlog_loop - the unattended version of Queue Review's own
"Re-review (AI)" button, for whenever the AI backend (e.g. a local Ollama
server on another machine) was unreachable and nobody's there to click it
once it's back. Calls the loop's raw coroutine directly (Loop.coro) rather
than starting the actual discord.ext.tasks.Loop, matching
tests/test_quickbooks_poll.py's established pattern for testing these
background loops.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import ai_review
import config
import cogs.item_flow as item_flow_module
from cogs.item_flow import ItemFlow


class _FakeMessage:
    def __init__(self, msg_id=1):
        self.id = msg_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []
        self._messages = {}
        self._next_id = 1000

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs or {"content": args[0] if args else None})
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        self._messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        return self._messages[message_id]

    def add(self, message_id, message):
        self._messages[message_id] = message


class _FakeBot:
    def __init__(self, channels: dict):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


@pytest.fixture
def channels(fresh_db):
    queue_channel = _FakeChannel(1)
    automated_channel = _FakeChannel(2)
    fresh_db.set_shared_channel("queue-review", 1)
    fresh_db.set_shared_channel("automated-review", 2)
    return queue_channel, automated_channel


@pytest.fixture
def failed_item(fresh_db, tmp_path, monkeypatch, channels):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    queue_channel, _ = channels
    pallet_id = fresh_db.create_pallet("Retry Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "a drill", [], 1)
    fresh_db.save_ai_review(
        item_id, title="a drill", description="No description provided.", flags="AI review failed",
        backend_failed=True,
    )
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, new_message_id=500)
    queue_channel.add(500, _FakeMessage(500))
    return item_id


# ------------------------------------------------------- database layer --


def test_save_ai_review_sets_the_failed_flag_on_a_fallback_result(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "note", [], 1)

    fresh_db.save_ai_review(item_id, title="t", description="d", flags="", backend_failed=True)
    assert fresh_db.get_item(item_id)["ai_review_failed"] == 1


def test_save_ai_review_clears_the_failed_flag_on_a_real_result(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "note", [], 1)
    fresh_db.save_ai_review(item_id, title="t", description="d", flags="", backend_failed=True)

    fresh_db.save_ai_review(item_id, title="Real Title", description="Real desc", flags="")
    assert fresh_db.get_item(item_id)["ai_review_failed"] == 0


def test_update_description_clears_the_failed_flag(fresh_db):
    """A human's manual edit already fixed the description by hand - an
    unattended retry overwriting it later would be a real bug."""
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "note", [], 1)
    fresh_db.save_ai_review(item_id, title="t", description="d", flags="", backend_failed=True)

    fresh_db.update_description(item_id, "Manually corrected description")
    assert fresh_db.get_item(item_id)["ai_review_failed"] == 0


def test_get_items_needing_ai_retry_only_returns_flagged_queue_review_items(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    flagged = fresh_db.create_item(pallet_id, "note", [], 1)
    fresh_db.save_ai_review(flagged, title="t", description="d", flags="", backend_failed=True)
    fresh_db.update_status(flagged, fresh_db.STATUS_QUEUE_REVIEW)

    not_flagged = fresh_db.create_item(pallet_id, "note", [], 1)
    fresh_db.update_status(not_flagged, fresh_db.STATUS_QUEUE_REVIEW)

    flagged_but_approved = fresh_db.create_item(pallet_id, "note", [], 1)
    fresh_db.save_ai_review(flagged_but_approved, title="t", description="d", flags="", backend_failed=True)
    fresh_db.update_status(flagged_but_approved, fresh_db.STATUS_AWAITING_LISTING)

    result = fresh_db.get_items_needing_ai_retry(limit=10)
    assert [r["id"] for r in result] == [flagged]


def test_get_items_needing_ai_retry_respects_the_limit(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    for _ in range(3):
        item_id = fresh_db.create_item(pallet_id, "note", [], 1)
        fresh_db.save_ai_review(item_id, title="t", description="d", flags="", backend_failed=True)
        fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW)

    assert len(fresh_db.get_items_needing_ai_retry(limit=2)) == 2


# -------------------------------------------------------------- the loop --


def test_loop_noops_when_ai_disabled(failed_item, fresh_db, channels, monkeypatch):
    queue_channel, automated_channel = channels
    monkeypatch.setattr(config, "AI_ENABLED", False)
    cog = ItemFlow(_FakeBot({1: queue_channel, 2: automated_channel}))

    asyncio.run(ItemFlow.ai_retry_backlog_loop.coro(cog))

    assert automated_channel.sent == []
    assert fresh_db.get_item(failed_item)["ai_review_failed"] == 1


def test_loop_retries_a_backlogged_item_and_clears_the_flag(failed_item, fresh_db, channels, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)

    async def fake_review_item(photo_paths, raw_description):
        return {
            "suggested_title": "Real Title",
            "suggested_description": "Real description.",
            "flags": [],
            "confidence": "high",
            "suggested_category": None,
            "suggested_price": None,
            "estimated_weight_lb": None,
            "estimated_length_in": None,
            "estimated_width_in": None,
            "estimated_height_in": None,
        }
    monkeypatch.setattr(ai_review, "review_item", fake_review_item)

    queue_channel, automated_channel = channels
    cog = ItemFlow(_FakeBot({1: queue_channel, 2: automated_channel}))

    asyncio.run(ItemFlow.ai_retry_backlog_loop.coro(cog))

    item = fresh_db.get_item(failed_item)
    assert item["ai_title"] == "Real Title"
    assert item["ai_review_failed"] == 0
    assert len(automated_channel.sent) == 1  # the "Retrying..." placeholder
    assert len(queue_channel.sent) == 1  # the fresh Queue Review card


def test_loop_skips_an_item_that_was_approved_in_the_meantime(failed_item, fresh_db, channels, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    fresh_db.update_status(failed_item, fresh_db.STATUS_AWAITING_LISTING)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("should not have re-run AI review on an item that already moved on")
    monkeypatch.setattr(ai_review, "review_item", fail_if_called)

    queue_channel, automated_channel = channels
    cog = ItemFlow(_FakeBot({1: queue_channel, 2: automated_channel}))

    asyncio.run(ItemFlow.ai_retry_backlog_loop.coro(cog))

    assert automated_channel.sent == []


def test_loop_noops_when_preserve_discord_history(failed_item, channels, monkeypatch):
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", True)
    monkeypatch.setattr(config, "AI_ENABLED", True)
    queue_channel, automated_channel = channels
    cog = ItemFlow(_FakeBot({1: queue_channel, 2: automated_channel}))

    asyncio.run(ItemFlow.ai_retry_backlog_loop.coro(cog))

    assert automated_channel.sent == []
