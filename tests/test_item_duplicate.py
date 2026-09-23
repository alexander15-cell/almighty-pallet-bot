"""
ItemFlow.duplicate_item() - the manual counterpart to Data Entry's automatic
"3x ..." quantity detection (parse_quantity), for splitting an item already
sitting in Queue Review into N independently-tracked items (/item duplicate
in admin_tools.py). Uses a minimal fake bot/channel rather than a real
discord.py Client, since this method only ever calls bot.get_channel(...)
and channel.send(...) - matching this codebase's existing preference for
DB-level tests over full Discord mocking.
"""
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import cogs.item_flow as item_flow_module
from cogs.item_flow import ItemFlow


class _FakeMessage:
    def __init__(self, msg_id):
        self.id = msg_id


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
def source_item(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)

    pallet_id = fresh_db.create_pallet("Dup Test Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "cordless drill, new in box", [], 1)
    fresh_db.save_ai_review(
        item_id, "DeWalt Cordless Drill", "A drill.", "[]",
        suggested_category="cordless drill", suggested_price=59.99,
        suggested_weight_lb=4.0, suggested_length_in=10, suggested_width_in=8, suggested_height_in=4,
    )

    folder = item_flow_module.photo_dir_for(item_id)
    photo_path = folder / "photo_0.jpg"
    photo_path.write_bytes(b"fake photo bytes")
    fresh_db.update_photo_public_urls(item_id, ["https://example.com/photo_0.jpg"])
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls = ? WHERE id = ?", (json.dumps([str(photo_path)]), item_id))

    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1)
    return fresh_db.get_item(item_id)


def test_duplicate_item_creates_independent_items_with_copied_ai_data(source_item, fresh_db):
    channel = _FakeChannel()
    cog = ItemFlow(_FakeBot(channel))

    new_numbers = asyncio.run(cog.duplicate_item(source_item, extra_count=2, actor_id=1))

    assert len(new_numbers) == 2
    assert len(set(new_numbers)) == 2  # genuinely distinct new numbers

    for number in new_numbers:
        new_item = fresh_db.get_item_by_pallet_and_number(source_item["pallet_id"], number)
        assert new_item["id"] != source_item["id"]
        assert new_item["status"] == fresh_db.STATUS_QUEUE_REVIEW
        assert new_item["raw_description"] == source_item["raw_description"]
        assert new_item["ai_title"] == source_item["ai_title"]
        assert new_item["ai_suggested_price"] == source_item["ai_suggested_price"]
        assert new_item["photo_public_urls"] == source_item["photo_public_urls"]

        new_photo_paths = json.loads(new_item["photo_urls"])
        assert len(new_photo_paths) == 1
        assert new_photo_paths[0] != json.loads(source_item["photo_urls"])[0]  # own file, not shared
        assert Path(new_photo_paths[0]).read_bytes() == b"fake photo bytes"


def test_duplicate_item_posts_a_card_per_duplicate(source_item):
    channel = _FakeChannel()
    cog = ItemFlow(_FakeBot(channel))

    asyncio.run(cog.duplicate_item(source_item, extra_count=3, actor_id=1))

    assert len(channel.sent) == 3


def test_duplicating_never_touches_the_original_item(source_item, fresh_db):
    channel = _FakeChannel()
    cog = ItemFlow(_FakeBot(channel))

    asyncio.run(cog.duplicate_item(source_item, extra_count=2, actor_id=1))

    unchanged = fresh_db.get_item(source_item["id"])
    assert unchanged["item_number"] == source_item["item_number"]
    assert unchanged["photo_urls"] == source_item["photo_urls"]
