"""/setup refresh-channel-info: brings an existing channel's topic and the
bot's own pinned "ℹ️" guide up to date with config.CHANNEL_INFO (they're
otherwise only written when /setup shared-channels creates a channel)."""
import asyncio

from cogs.pallet_setup import refresh_channel_info_message

BOT = object()


class _Msg:
    def __init__(self, author, content):
        self.author, self.content = author, content
        self.pinned = False

    async def edit(self, content=None):
        self.content = content

    async def pin(self, reason=None):
        self.pinned = True


class _Channel:
    def __init__(self, topic, pins):
        self.topic, self._pins, self.sent, self.topic_edits = topic, pins, [], 0

    async def edit(self, topic=None, reason=None):
        self.topic = topic
        self.topic_edits += 1

    async def pins(self):
        return self._pins

    async def send(self, content):
        msg = _Msg(BOT, content)
        self.sent.append(msg)
        return msg


class _Client:
    user = BOT


def test_edits_the_bots_pinned_guide_and_topic():
    someone_elses = _Msg(object(), "ℹ️ a human's pinned note")
    ours = _Msg(BOT, "ℹ️ old text about COGS")
    channel = _Channel("old text about COGS", [someone_elses, ours])

    assert asyncio.run(refresh_channel_info_message(_Client(), channel, "new text")) == "updated"
    assert ours.content == "ℹ️ new text"
    assert someone_elses.content == "ℹ️ a human's pinned note"
    assert channel.topic == "new text" and channel.sent == []


def test_unchanged_when_already_current():
    ours = _Msg(BOT, "ℹ️ same")
    channel = _Channel("same", [ours])
    assert asyncio.run(refresh_channel_info_message(_Client(), channel, "same")) == "unchanged"
    assert channel.topic_edits == 0


def test_posts_and_pins_when_the_guide_is_gone():
    channel = _Channel("", [])
    assert asyncio.run(refresh_channel_info_message(_Client(), channel, "text")) == "posted"
    assert len(channel.sent) == 1 and channel.sent[0].pinned and channel.sent[0].content == "ℹ️ text"


def test_topic_is_capped_at_discords_limit():
    channel = _Channel("", [_Msg(BOT, "ℹ️ x")])
    asyncio.run(refresh_channel_info_message(_Client(), channel, "y" * 1500))
    assert len(channel.topic) == 1024
