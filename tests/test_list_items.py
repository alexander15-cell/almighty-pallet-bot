"""
/list items (cogs/admin_tools.py) - browses every collected item across
every pallet, optionally filtered to one stage, with a thumbnail photo and
current stage per item, paginated with Previous/Next buttons
(ItemListPaginatorView/_build_item_list_page) since a shop can easily have
more items than fit in one Discord message. Also database.get_all_items,
the cross-pallet/cross-status query behind it.
"""
import asyncio
import json
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import cogs.admin_tools as admin_tools_module
import database as db
from cogs.admin_tools import AdminTools, ItemListPaginatorView, _build_item_list_page


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.embeds = None
        self.files = None
        self.view = None
        self.edited = []

    async def send_message(self, content=None, **kwargs):
        self.content = content
        self.embeds = kwargs.get("embeds")
        self.files = kwargs.get("files")
        self.view = kwargs.get("view")

    async def edit_message(self, content=None, **kwargs):
        self.edited.append((content, kwargs.get("embeds"), kwargs.get("attachments")))
        self.content = content
        self.embeds = kwargs.get("embeds")


class _FakeInteraction:
    def __init__(self):
        self.response = _FakeResponse()


# -------------------------------------------------------- database layer --


def test_get_all_items_returns_items_across_pallets(fresh_db):
    p1 = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    p2 = fresh_db.create_pallet("Pallet B", category_id=2, created_by=1)
    fresh_db.create_item(p1, "Widget", [], 1)
    fresh_db.create_item(p2, "Gadget", [], 1)

    items = fresh_db.get_all_items()

    assert len(items) == 2


def test_get_all_items_excludes_deleted_by_default(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    keep_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    deleted_id = fresh_db.create_item(pallet_id, "Gadget", [], 1)
    fresh_db.soft_delete_item(deleted_id, actor_id=1)

    items = fresh_db.get_all_items()

    assert [i["id"] for i in items] == [keep_id]


def test_get_all_items_filters_by_status(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item1 = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item2 = fresh_db.create_item(pallet_id, "Gadget", [], 1)
    fresh_db.update_status(item2, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1)

    items = fresh_db.get_all_items(status=fresh_db.STATUS_QUEUE_REVIEW)

    assert [i["id"] for i in items] == [item2]


def test_get_all_items_explicit_deleted_status_returns_deleted_items(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.soft_delete_item(item_id, actor_id=1)

    items = fresh_db.get_all_items(status=fresh_db.STATUS_DELETED)

    assert [i["id"] for i in items] == [item_id]


def test_get_all_items_include_deleted_true(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    keep_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    deleted_id = fresh_db.create_item(pallet_id, "Gadget", [], 1)
    fresh_db.soft_delete_item(deleted_id, actor_id=1)

    items = fresh_db.get_all_items(include_deleted=True)

    assert {i["id"] for i in items} == {keep_id, deleted_id}


# ------------------------------------------------------------- pagination --


def test_build_item_list_page_slices_correctly(fresh_db):
    items = [{"id": i, "pallet_id": 1, "item_number": i, "status": "listed", "ai_title": None,
              "photo_urls": None, "photo_public_urls": None} for i in range(1, 20)]
    page0 = _build_item_list_page(items, page=0)
    page1 = _build_item_list_page(items, page=1)

    assert len(page0["embeds"]) == 8
    assert len(page1["embeds"]) == 8
    assert page0["max_page"] == 2  # 19 items / 8 per page -> pages 0,1,2


def test_build_item_list_page_prefers_hosted_photo_url(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://cdn.example.com/photo.jpg"])
    item = fresh_db.get_item(item_id)

    page = _build_item_list_page([item], page=0)

    assert page["embeds"][0].thumbnail.url == "https://cdn.example.com/photo.jpg"
    assert page["files"] == []


def test_build_item_list_page_falls_back_to_local_file(fresh_db, tmp_path):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    photo_path = tmp_path / "photo.jpg"
    photo_path.write_bytes(b"fake-image-bytes")
    item_id = fresh_db.create_item(pallet_id, "Widget", [str(photo_path)], 1)
    item = fresh_db.get_item(item_id)

    page = _build_item_list_page([item], page=0)

    assert len(page["files"]) == 1
    assert page["embeds"][0].thumbnail.url == f"attachment://{page['files'][0].filename}"


def test_build_item_list_page_no_photo_has_no_thumbnail(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)

    page = _build_item_list_page([item], page=0)

    assert page["embeds"][0].thumbnail.url is None
    assert page["files"] == []


def test_build_item_list_page_shows_pallet_and_stage(fresh_db):
    items = [{"id": 1, "pallet_id": 999, "item_number": 5, "status": "awaiting_listing",
              "ai_title": "Drill", "photo_urls": None, "photo_public_urls": None}]
    page = _build_item_list_page(items, page=0)
    assert "Awaiting Listing" in page["embeds"][0].description
    assert "#5" in page["embeds"][0].description


def test_paginator_view_disables_previous_on_first_page(fresh_db):
    items = [{"id": i, "pallet_id": 1, "item_number": i, "status": "listed", "ai_title": None,
              "photo_urls": None, "photo_public_urls": None} for i in range(1, 5)]
    view = ItemListPaginatorView(items)
    assert view.previous.disabled is True
    assert view.next.disabled is True  # only 4 items, fits on one page


def test_paginator_view_next_and_previous_navigate(fresh_db):
    items = [{"id": i, "pallet_id": 1, "item_number": i, "status": "listed", "ai_title": None,
              "photo_urls": None, "photo_public_urls": None} for i in range(1, 20)]
    view = ItemListPaginatorView(items)
    assert view.next.disabled is False
    interaction = _FakeInteraction()

    asyncio.run(view.next.callback(interaction))
    assert view.page == 1
    assert "Page 2/3" in interaction.response.content

    asyncio.run(view.previous.callback(interaction))
    assert view.page == 0
    assert "Page 1/3" in interaction.response.content


# -------------------------------------------------------------- command --


def test_list_items_command_empty_state(fresh_db):
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction()

    asyncio.run(AdminTools.list_items.callback(cog, interaction, None))

    assert "No items found" in interaction.response.content


def test_list_items_command_shows_first_page(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.create_item(pallet_id, "Gadget", [], 1)
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction()

    asyncio.run(AdminTools.list_items.callback(cog, interaction, None))

    assert isinstance(interaction.response.view, ItemListPaginatorView)
    assert len(interaction.response.embeds) == 2
    assert "2 item(s)" in interaction.response.content


def test_list_items_command_with_status_filter(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.create_item(pallet_id, "Widget", [], 1)
    sold_id = fresh_db.create_item(pallet_id, "Gadget", [], 1)
    fresh_db.update_status(sold_id, fresh_db.STATUS_SOLD, actor_id=1)
    cog = AdminTools.__new__(AdminTools)
    interaction = _FakeInteraction()
    choice = discord.app_commands.Choice(name="Sold", value=fresh_db.STATUS_SOLD)

    asyncio.run(AdminTools.list_items.callback(cog, interaction, choice))

    assert len(interaction.response.embeds) == 1
    assert "stage: Sold" in interaction.response.content
