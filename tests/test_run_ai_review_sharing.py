"""
ItemFlow.run_ai_review() called with more than one item_id (a Data Entry
submission of N identical items - see parse_quantity in item_flow.py) must
only call the AI backend ONCE and copy that single result to every item,
not review each one separately - they're identical by definition, so N
separate calls would just burn extra API cost/time for the same answer.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import ai_review
import cogs.item_flow as item_flow_module
from cogs.item_flow import ItemFlow


class _FakeMessage:
    def __init__(self, msg_id=1):
        self.id = msg_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeChannel:
    def __init__(self):
        self.sent = []
        self._next_id = 1000

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        self._next_id += 1
        return _FakeMessage(self._next_id)


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


@pytest.fixture
def three_items(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("Shared AI Pallet", category_id=1, created_by=1)
    item_ids = [fresh_db.create_item(pallet_id, "3x cordless drill", [], 1) for _ in range(3)]
    return item_ids


def test_ai_backend_is_called_exactly_once_for_duplicate_items(three_items, monkeypatch):
    call_count = 0

    async def fake_review_item(photo_paths, raw_description):
        nonlocal call_count
        call_count += 1
        return {
            "suggested_title": "DeWalt Cordless Drill",
            "suggested_description": "A drill.",
            "flags": [],
            "confidence": "high",
            "suggested_category": "cordless drill",
            "suggested_price": 59.99,
            "estimated_weight_lb": 4.0,
            "estimated_length_in": 10,
            "estimated_width_in": 8,
            "estimated_height_in": 4,
        }

    monkeypatch.setattr(ai_review, "review_item", fake_review_item)

    channel = _FakeChannel()
    cog = ItemFlow(_FakeBot(channel))
    placeholder = _FakeMessage()

    asyncio.run(cog.run_ai_review(three_items, placeholder))

    assert call_count == 1
    assert placeholder.deleted is True


def test_shared_ai_result_is_saved_to_every_duplicate(three_items, fresh_db, monkeypatch):
    async def fake_review_item(photo_paths, raw_description):
        return {
            "suggested_title": "DeWalt Cordless Drill",
            "suggested_description": "A drill.",
            "flags": ["needs testing"],
            "confidence": "high",
            "suggested_category": "cordless drill",
            "suggested_price": 59.99,
            "estimated_weight_lb": 4.0,
            "estimated_length_in": 10,
            "estimated_width_in": 8,
            "estimated_height_in": 4,
        }

    monkeypatch.setattr(ai_review, "review_item", fake_review_item)

    channel = _FakeChannel()
    cog = ItemFlow(_FakeBot(channel))

    asyncio.run(cog.run_ai_review(three_items, _FakeMessage()))

    for item_id in three_items:
        item = fresh_db.get_item(item_id)
        assert item["ai_title"] == "DeWalt Cordless Drill"
        assert item["ai_suggested_price"] == 59.99
        assert item["ai_suggested_weight_lb"] == 4.0
        assert item["status"] == fresh_db.STATUS_QUEUE_REVIEW

    assert len(channel.sent) == 3  # one Queue Review card per item
