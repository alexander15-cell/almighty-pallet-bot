"""
ItemFlow.on_message (cogs/item_flow.py) - the plain-text "logged as item
#N" confirmation posted back in the Data Entry channel itself right after
a submission, added because the previous behavior only ever announced the
assigned item number in the shared #automated-review channel, which
whoever just submitted an item may not be watching - they need the number
immediately, in Data Entry, in time to write it on the item's box/sticker.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import cogs.item_flow as item_flow_module
import pytest
from cogs.item_flow import ItemFlow


class _FakeAttachment:
    def __init__(self, filename="photo.jpg", data=b"fake-bytes"):
        self.filename = filename
        self._data = data

    async def read(self):
        return self._data


class _FakeAuthor:
    def __init__(self, user_id=1):
        self.id = user_id
        self.bot = False


class _FakeMessage:
    def __init__(self, msg_id=1000):
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


class _FakeDataEntryMessage:
    def __init__(self, channel, content="a widget, untested", attachments=None, author_id=1):
        self.channel = channel
        self.content = content
        self.attachments = attachments if attachments is not None else [_FakeAttachment()]
        self.author = _FakeAuthor(author_id)
        self.reference = None
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _FakeBot:
    def __init__(self, channels: dict):
        self._channels = channels

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


@pytest.fixture
def pallet_channels(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("Sticker Pallet", category_id=1, created_by=1)

    data_entry_channel = _FakeChannel(channel_id=100)
    automated_review_channel = _FakeChannel(channel_id=200)
    queue_review_channel = _FakeChannel(channel_id=300)
    fresh_db.map_channel(pallet_id, "data-entry", 100)
    fresh_db.set_shared_channel("automated-review", 200)
    fresh_db.set_shared_channel("queue-review", 300)

    bot = _FakeBot({100: data_entry_channel, 200: automated_review_channel, 300: queue_review_channel})
    cog = ItemFlow(bot)
    return pallet_id, data_entry_channel, automated_review_channel, cog


def test_single_item_confirmation_names_its_number(pallet_channels):
    pallet_id, data_entry_channel, automated_review_channel, cog = pallet_channels
    message = _FakeDataEntryMessage(data_entry_channel, content="cordless drill, untested")

    asyncio.run(cog.on_message(message))

    confirmations = [s for s in data_entry_channel.sent if s and "logged for" in s]
    assert len(confirmations) == 1
    assert "Item #1" in confirmations[0]
    assert "write that number on the box" in confirmations[0]
    assert message.deleted is True


def test_multi_item_confirmation_lists_every_number(pallet_channels):
    pallet_id, data_entry_channel, automated_review_channel, cog = pallet_channels
    message = _FakeDataEntryMessage(data_entry_channel, content="3x cordless drill, new in box")

    asyncio.run(cog.on_message(message))

    confirmations = [s for s in data_entry_channel.sent if s and "logged for" in s]
    assert len(confirmations) == 1
    assert "Items #1, #2, #3" in confirmations[0]
    assert "write the matching number on each box" in confirmations[0]


def test_confirmation_names_the_pallet(pallet_channels):
    pallet_id, data_entry_channel, automated_review_channel, cog = pallet_channels
    message = _FakeDataEntryMessage(data_entry_channel, content="a lamp")

    asyncio.run(cog.on_message(message))

    confirmations = [s for s in data_entry_channel.sent if s and "logged for" in s]
    assert "Sticker Pallet" in confirmations[0]
