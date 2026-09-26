"""
ItemFlow.rereview_item / QueueReviewView's "Re-review (AI)" button
(cogs/item_flow.py) - sends an item already in Queue Review back through
run_ai_review, for after editing its description or when the AI's first
pass errored/timed out. Also covers run_ai_review's preference for
ai_description (a manual Edit correction) over the original raw_description
when both are present.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import ai_review
import config
import cogs.item_flow as item_flow_module
import pytest
from cogs.item_flow import ItemFlow, QueueReviewView


class _FakeMessage:
    def __init__(self, msg_id=1000):
        self.id = msg_id
        self.deleted = False
        self.edited_view = "unset"

    async def delete(self):
        self.deleted = True

    async def edit(self, view=None, **kwargs):
        self.edited_view = view


class _FakeChannel:
    def __init__(self, channel_id=1):
        self.id = channel_id
        self.sent = []

    async def send(self, *args, **kwargs):
        content = args[0] if args else kwargs.get("content")
        self.sent.append(content)
        return _FakeMessage()


class _FakeBot:
    def __init__(self, channels: dict, item_flow_cog=None):
        self._channels = channels
        self._item_flow_cog = item_flow_cog

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_cog(self, name):
        return self._item_flow_cog


class _FakeRole:
    pass


class _FakeUser:
    def __init__(self, role, user_id=42):
        self.id = user_id
        self.roles = [role]


class _FakeResponse:
    def __init__(self):
        self.content = None

    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, content=None, **kwargs):
        self.content = content


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


class _FakeInteraction:
    def __init__(self, client, message=None, role=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = message
        self.user = _FakeUser(role)
        self.guild = object()
        self.client = client


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()

    def fake_resolve_role(guild, role_name):
        return role
    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", fake_resolve_role)
    return role


@pytest.fixture
def queue_review_item(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("Rereview Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "cordless drill", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=999)
    return fresh_db.get_item(item_id)


def _fake_result(**overrides):
    result = {
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
    result.update(overrides)
    return result


def test_rereview_requires_ai_enabled(fresh_db, queue_review_item, bypass_role_check, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(cog.rereview_item(interaction, queue_review_item["id"]))

    assert "nothing to re-run" in interaction.response.content


def test_rereview_rejects_item_not_in_queue_review(fresh_db, queue_review_item, bypass_role_check, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    fresh_db.update_status(queue_review_item["id"], fresh_db.STATUS_AWAITING_LISTING, actor_id=1)
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(cog.rereview_item(interaction, queue_review_item["id"]))

    assert "already moved on" in interaction.response.content


def test_rereview_requires_automated_review_channel(fresh_db, queue_review_item, bypass_role_check, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    cog = ItemFlow(_FakeBot({}))  # no channels configured at all
    client = _FakeBot({}, item_flow_cog=cog)
    interaction = _FakeInteraction(client, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(cog.rereview_item(interaction, queue_review_item["id"]))

    assert "isn't set up" in interaction.response.content


def test_rereview_clears_old_card_and_reruns_ai(fresh_db, queue_review_item, bypass_role_check, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", True)

    async def fake_review_item(photo_paths, raw_description):
        return _fake_result(suggested_title="Re-reviewed Title")
    monkeypatch.setattr(ai_review, "review_item", fake_review_item)

    automated_review_channel = _FakeChannel(channel_id=200)
    queue_channel = _FakeChannel(channel_id=300)
    fresh_db.set_shared_channel("automated-review", 200)
    fresh_db.set_shared_channel("queue-review", 300)

    cog = ItemFlow(_FakeBot({200: automated_review_channel, 300: queue_channel}))
    client = _FakeBot({200: automated_review_channel, 300: queue_channel}, item_flow_cog=cog)

    old_card = _FakeMessage()
    interaction = _FakeInteraction(client, message=old_card, role=bypass_role_check)

    asyncio.run(cog.rereview_item(interaction, queue_review_item["id"]))

    assert old_card.deleted is True  # old Queue Review card cleared
    assert any("Re-reviewing" in s for s in automated_review_channel.sent)
    assert len(queue_channel.sent) == 1  # fresh card posted
    assert fresh_db.get_item(queue_review_item["id"])["ai_title"] == "Re-reviewed Title"
    assert fresh_db.get_item(queue_review_item["id"])["status"] == fresh_db.STATUS_QUEUE_REVIEW
    assert "Sent item" in interaction.followup.content


def test_rereview_button_wired_to_cog_method(fresh_db, queue_review_item, bypass_role_check, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)  # short-circuits before any Discord I/O
    cog = ItemFlow(_FakeBot({}))
    client = _FakeBot({}, item_flow_cog=cog)

    view = QueueReviewView(queue_review_item["id"])
    button = next(c for c in view.children if c.custom_id == f"pallet_bot:qr_rereview:{queue_review_item['id']}")
    interaction = _FakeInteraction(client, message=_FakeMessage(), role=bypass_role_check)

    asyncio.run(button.callback(interaction))

    assert "nothing to re-run" in interaction.response.content


# ------------------------------------------------- run_ai_review input preference


def test_run_ai_review_prefers_edited_ai_description(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("Edit Pref Pallet", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "original raw note", [], 1)
    fresh_db.update_description(item_id, "manually corrected description")

    seen_notes = []

    async def fake_review_item(photo_paths, raw_description):
        seen_notes.append(raw_description)
        return _fake_result()
    monkeypatch.setattr(ai_review, "review_item", fake_review_item)
    fresh_db.set_shared_channel("queue-review", 300)

    cog = ItemFlow(_FakeBot({300: _FakeChannel(channel_id=300)}))
    asyncio.run(cog.run_ai_review([item_id], _FakeMessage()))

    assert seen_notes == ["manually corrected description"]


def test_run_ai_review_falls_back_to_raw_description_when_no_edit(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("No Edit Pallet", category_id=3, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "original raw note", [], 1)

    seen_notes = []

    async def fake_review_item(photo_paths, raw_description):
        seen_notes.append(raw_description)
        return _fake_result()
    monkeypatch.setattr(ai_review, "review_item", fake_review_item)
    fresh_db.set_shared_channel("queue-review", 300)

    cog = ItemFlow(_FakeBot({300: _FakeChannel(channel_id=300)}))
    asyncio.run(cog.run_ai_review([item_id], _FakeMessage()))

    assert seen_notes == ["original raw note"]
