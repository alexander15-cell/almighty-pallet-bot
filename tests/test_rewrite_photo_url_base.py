"""
database.get_items_with_photo_url_prefix / rewrite_photo_url_prefix, and
/admin rewrite-photo-url-base (cogs/admin_tools.py) - the one-time fix for
stored R2 photo URLs after correcting a wrong R2_PUBLIC_URL_BASE in .env.
Changing that env var only affects photos uploaded from then on, since
each URL is written into items.photo_public_urls once at upload time, not
rebuilt from config on read - this is what backfills the already-stored
ones. Calls the command callback directly with minimal fake Discord
objects, matching tests/test_admin_photo_purge.py's style.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import cogs.admin_tools as admin_tools_module
import pytest
from cogs.admin_tools import AdminTools

OLD_BASE = "https://8f29134597998cf65dcbb80f26e3bcf7.r2.cloudflarestorage.com/"
NEW_BASE = "https://pub-d2913da42e5f420683d61bd260c5573d.r2.dev"


class _FakeResponse:
    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def send_message(self, content=None, **kwargs):
        raise AssertionError("should defer and use followup.send, not response.send_message")


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = type("U", (), {"id": 1})()


@pytest.fixture(autouse=True)
def bypass_admin_check(monkeypatch):
    monkeypatch.setattr(admin_tools_module, "_is_pallet_admin", lambda interaction: True)


@pytest.fixture
def cog():
    return AdminTools.__new__(AdminTools)


# --------------------------------------------------------------- DB layer


def test_get_items_with_photo_url_prefix_matches_only_old_base(fresh_db):
    pallet_id = fresh_db.create_pallet("Prefix Pallet", category_id=1, created_by=1)
    matching_item = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_photo_public_urls(matching_item, [OLD_BASE.rstrip("/") + "/items/1/a.jpg"])
    other_item = fresh_db.create_item(pallet_id, "gadget", [], 1)
    fresh_db.update_photo_public_urls(other_item, [NEW_BASE + "/items/2/a.jpg"])

    matches = fresh_db.get_items_with_photo_url_prefix(OLD_BASE)
    assert [m["id"] for m in matches] == [matching_item]


def test_get_items_with_photo_url_prefix_no_matches(fresh_db):
    pallet_id = fresh_db.create_pallet("No Match Pallet", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_photo_public_urls(item_id, [NEW_BASE + "/items/1/a.jpg"])

    assert fresh_db.get_items_with_photo_url_prefix(OLD_BASE) == []


def test_rewrite_photo_url_prefix_swaps_domain_keeps_object_key(fresh_db):
    pallet_id = fresh_db.create_pallet("Rewrite Pallet", category_id=3, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_photo_public_urls(
        item_id, [OLD_BASE.rstrip("/") + "/items/5/photo_0.jpg", OLD_BASE.rstrip("/") + "/items/5/photo_1.jpg"]
    )

    updated_count = fresh_db.rewrite_photo_url_prefix(OLD_BASE, NEW_BASE, actor_id=99)

    assert updated_count == 1
    item = fresh_db.get_item(item_id)
    import json
    urls = json.loads(item["photo_public_urls"])
    assert urls == [
        NEW_BASE.rstrip("/") + "/items/5/photo_0.jpg",
        NEW_BASE.rstrip("/") + "/items/5/photo_1.jpg",
    ]


def test_rewrite_photo_url_prefix_leaves_non_matching_urls_alone(fresh_db):
    pallet_id = fresh_db.create_pallet("Mixed Pallet", category_id=4, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    other_url = "https://some-other-host.example/items/9/a.jpg"
    fresh_db.update_photo_public_urls(item_id, [OLD_BASE.rstrip("/") + "/items/9/photo_0.jpg", other_url])

    fresh_db.rewrite_photo_url_prefix(OLD_BASE, NEW_BASE, actor_id=99)

    import json
    urls = json.loads(fresh_db.get_item(item_id)["photo_public_urls"])
    assert urls[0] == NEW_BASE.rstrip("/") + "/items/9/photo_0.jpg"
    assert urls[1] == other_url  # untouched - didn't match old_prefix


def test_rewrite_photo_url_prefix_logs_item_event(fresh_db):
    pallet_id = fresh_db.create_pallet("Event Pallet", category_id=5, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_photo_public_urls(item_id, [OLD_BASE.rstrip("/") + "/items/1/a.jpg"])

    fresh_db.rewrite_photo_url_prefix(OLD_BASE, NEW_BASE, actor_id=42)

    with fresh_db.get_conn() as conn:
        events = conn.execute(
            "SELECT * FROM item_events WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,)
        ).fetchall()
    assert "R2 photo URL base corrected" in events[0]["note"]


# ------------------------------------------------------------------ command


def test_rewrite_photo_url_base_no_candidates(fresh_db, cog):
    interaction = _FakeInteraction()
    asyncio.run(AdminTools.rewrite_photo_url_base.callback(cog, interaction, OLD_BASE, NEW_BASE, confirm=False))
    assert "nothing to rewrite" in interaction.followup.content


def test_rewrite_photo_url_base_preview_does_not_modify(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Preview Pallet", category_id=6, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_photo_public_urls(item_id, [OLD_BASE.rstrip("/") + "/items/1/a.jpg"])

    interaction = _FakeInteraction()
    asyncio.run(AdminTools.rewrite_photo_url_base.callback(cog, interaction, OLD_BASE, NEW_BASE, confirm=False))

    assert "Preview only" in interaction.followup.content
    assert "Example:" in interaction.followup.content
    import json
    urls = json.loads(fresh_db.get_item(item_id)["photo_public_urls"])
    assert urls[0].startswith(OLD_BASE.rstrip("/"))  # untouched


def test_rewrite_photo_url_base_confirm_rewrites(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Confirm Pallet", category_id=7, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_photo_public_urls(item_id, [OLD_BASE.rstrip("/") + "/items/1/a.jpg"])

    interaction = _FakeInteraction()
    asyncio.run(AdminTools.rewrite_photo_url_base.callback(cog, interaction, OLD_BASE, NEW_BASE, confirm=True))

    assert "Rewrote stored photo URLs for 1 item" in interaction.followup.content
    import json
    urls = json.loads(fresh_db.get_item(item_id)["photo_public_urls"])
    assert urls[0] == NEW_BASE.rstrip("/") + "/items/1/a.jpg"
