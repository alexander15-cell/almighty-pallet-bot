"""
The website-shop feature integrated directly into this bot's own process
(cogs/website_shop.py) - as opposed to tests/test_combined_intake.py, which
covers the separate, isolated combined_bot.py deployment of the same
underlying combined_intake.py/combined_delivery.py modules.

Covers the three actual code changes this integration needed:
- combined_intake.IntakeAdapter accepting config.WEBSITE_SHOP_ENABLED as an
  alternative to combined_bot.py's COMBINED_MODE + PRESERVE_DISCORD_HISTORY.
- IntakeAdapter._owned_item() skipping the data-entry channel-identity check
  when "data-entry" isn't a key in settings.channels at all (this bot has one
  such channel per pallet, not one for the whole server).
- database.ensure_items_autoincrement_floor().
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

import backup
import config
import database as db
from combined_intake import IntakeAdapter, MIN_NEW_ITEM_ID
from shop_approval import ShopReviewStore

GUILD = "100000000000000001"
SHOP = "100000000000000002"
APP = "100000000000000003"
USER = "100000000000000004"
LISTED = "100000000000000006"
SOLD = "100000000000000007"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def integrated_case(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WEBSITE_SHOP_ENABLED", True)
    monkeypatch.setattr(config, "COMBINED_MODE", False, raising=False)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    photo_root = tmp_path / "photos"
    monkeypatch.setattr(config, "PHOTO_DIR", str(photo_root))
    fresh_db.set_shared_channel("listed", int(LISTED))
    fresh_db.set_shared_channel("sold", int(SOLD))

    # Two DIFFERENT pallets, each with its OWN distinct data-entry channel -
    # unlike combined_bot.py's isolated deployment (always exactly one
    # pallet/data-entry channel), this bot has many.
    pallet_a = fresh_db.create_pallet("Pallet A", category_id=111, created_by=int(USER))
    fresh_db.map_channel(pallet_a, "data-entry", 200000000000000001)
    pallet_b = fresh_db.create_pallet("Pallet B", category_id=112, created_by=int(USER))
    fresh_db.map_channel(pallet_b, "data-entry", 200000000000000002)

    db.ensure_items_autoincrement_floor(config.WEBSITE_SHOP_ITEM_ID_FLOOR)

    def make_listed_item(pallet_id):
        item_id = fresh_db.create_item(pallet_id, "Approved description", [], int(USER))
        photo = photo_root / str(item_id) / "photo.png"
        photo.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), "navy").save(photo)
        with fresh_db.get_conn() as conn:
            conn.execute("UPDATE items SET photo_urls=? WHERE id=?", (json.dumps([str(photo)]), item_id))
        fresh_db.save_ebay_listing_data(item_id, "Human approved title", "123", "3000", 25.99, {})
        fresh_db.update_status(item_id, "listed", new_message_id=500000000000000001)
        return item_id

    item_a = make_listed_item(pallet_a)
    item_b = make_listed_item(pallet_b)

    store = ShopReviewStore(tmp_path / "shop.sqlite", guild_id=GUILD, shop_channel_id=SHOP, operator_ids=[USER])
    bot = SimpleNamespace(get_channel=lambda channel_id: None, add_view=Mock())
    settings = SimpleNamespace(
        application_id=APP, guild_id=GUILD,
        channels={"website_shop": SHOP, "listed": LISTED, "sold": SOLD},
        item_id_floor=config.WEBSITE_SHOP_ITEM_ID_FLOOR, sku_prefix=config.WEBSITE_SHOP_SKU_PREFIX,
        photo_directory=photo_root,
    )
    adapter = IntakeAdapter(bot, settings, store)
    value = SimpleNamespace(adapter=adapter, store=store, db=fresh_db, item_a=item_a, item_b=item_b)
    yield value
    adapter.close()
    store.close()


def test_intake_adapter_accepts_website_shop_enabled_instead_of_combined_mode(integrated_case):
    # Construction alone (in the fixture) already proves the new gate works -
    # this asserts the actual per-item logic works too, for BOTH pallets.
    counts = run(integrated_case.adapter.reconcile())
    assert counts["created"] == 2
    assert integrated_case.store.system_get_review(str(integrated_case.item_a)) is not None
    assert integrated_case.store.system_get_review(str(integrated_case.item_b)) is not None


def test_owned_item_skips_data_entry_check_when_absent_from_settings(integrated_case):
    # Both items resolve despite each pallet having a DIFFERENT data-entry
    # channel and settings.channels having no "data-entry" key at all.
    item = integrated_case.adapter._owned_item(str(integrated_case.item_a))
    assert item is not None and item["id"] == integrated_case.item_a
    item = integrated_case.adapter._owned_item(str(integrated_case.item_b))
    assert item is not None and item["id"] == integrated_case.item_b


def test_legacy_item_below_floor_is_never_owned(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WEBSITE_SHOP_ENABLED", True)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    monkeypatch.setattr(config, "PHOTO_DIR", str(tmp_path / "photos"))
    fresh_db.set_shared_channel("listed", int(LISTED))
    fresh_db.set_shared_channel("sold", int(SOLD))
    pallet = fresh_db.create_pallet("Existing pallet", category_id=111, created_by=int(USER))
    fresh_db.map_channel(pallet, "data-entry", 200000000000000009)
    # Created BEFORE the floor is ever bumped - a genuine pre-existing item.
    legacy_id = fresh_db.create_item(pallet, "Pre-existing real item", [], int(USER))
    assert legacy_id < config.WEBSITE_SHOP_ITEM_ID_FLOOR

    db.ensure_items_autoincrement_floor(config.WEBSITE_SHOP_ITEM_ID_FLOOR)
    store = ShopReviewStore(tmp_path / "shop.sqlite", guild_id=GUILD, shop_channel_id=SHOP, operator_ids=[USER])
    bot = SimpleNamespace(get_channel=lambda channel_id: None, add_view=Mock())
    settings = SimpleNamespace(
        application_id=APP, guild_id=GUILD, channels={"website_shop": SHOP, "listed": LISTED, "sold": SOLD},
        item_id_floor=config.WEBSITE_SHOP_ITEM_ID_FLOOR, sku_prefix=config.WEBSITE_SHOP_SKU_PREFIX,
        photo_directory=tmp_path / "photos",
    )
    adapter = IntakeAdapter(bot, settings, store)
    try:
        with pytest.raises(Exception):
            adapter._owned_item(str(legacy_id))
    finally:
        adapter.close()
        store.close()


def test_ensure_items_autoincrement_floor_is_idempotent_and_never_lowers(fresh_db):
    assert db.ensure_items_autoincrement_floor(1_000_000_000) is True
    item_id = fresh_db.create_item(
        fresh_db.create_pallet("P", category_id=1, created_by=1), "Item", [], 1
    )
    assert item_id == 1_000_000_000
    # Already past the floor now (real row inserted above it) - must not
    # rewind the counter and risk id reuse.
    assert db.ensure_items_autoincrement_floor(1_000_000_000) is False
    next_id = fresh_db.create_item(
        fresh_db.get_pallet(fresh_db.get_item(item_id)["pallet_id"])["id"], "Item 2", [], 1
    )
    assert next_id == 1_000_000_001


def test_backup_includes_website_shop_databases_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "pallet_tracker.db"))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "PHOTO_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(config, "EBAY_BATCH_ARCHIVE_DIR", str(tmp_path / "ebay_archive"))
    monkeypatch.setattr(config, "FB_MARKETPLACE_BATCH_ARCHIVE_DIR", str(tmp_path / "fb_archive"))
    monkeypatch.setattr(config, "PIRATE_SHIP_EXPORT_ARCHIVE_DIR", str(tmp_path / "pirate_ship"))
    db.init_db()

    shop_db = tmp_path / "shop_approval.sqlite"
    journal_db = tmp_path / "website_journal.sqlite"
    ShopReviewStore(shop_db, guild_id=GUILD, shop_channel_id=SHOP, operator_ids=[USER]).close()
    from publisher.journal import Journal
    Journal(journal_db, "a" * 32, GUILD, "https://example.com", APP, LISTED, SOLD).close()

    monkeypatch.setattr(backup, "_WEBSITE_SHOP_DBS", {
        "shop_approval.sqlite": str(shop_db), "website_journal.sqlite": str(journal_db),
    })
    zip_path = backup.create_backup()
    problems = backup.verify_backup(zip_path)
    assert not problems
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "shop_approval.sqlite" in names
    assert "website_journal.sqlite" in names


def test_backup_omits_website_shop_databases_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATABASE_PATH", str(tmp_path / "pallet_tracker.db"))
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "PHOTO_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(config, "EBAY_BATCH_ARCHIVE_DIR", str(tmp_path / "ebay_archive"))
    monkeypatch.setattr(config, "FB_MARKETPLACE_BATCH_ARCHIVE_DIR", str(tmp_path / "fb_archive"))
    monkeypatch.setattr(config, "PIRATE_SHIP_EXPORT_ARCHIVE_DIR", str(tmp_path / "pirate_ship"))
    db.init_db()
    monkeypatch.setattr(backup, "_WEBSITE_SHOP_DBS", {
        "shop_approval.sqlite": str(tmp_path / "does-not-exist-1.sqlite"),
        "website_journal.sqlite": str(tmp_path / "does-not-exist-2.sqlite"),
    })
    zip_path = backup.create_backup()
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "shop_approval.sqlite" not in names
    assert "website_journal.sqlite" not in names


def test_bot_py_only_loads_website_shop_cog_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "WEBSITE_SHOP_ENABLED", False)
    import importlib
    import bot as bot_module
    importlib.reload(bot_module)
    try:
        assert "cogs.website_shop" not in bot_module.COGS
    finally:
        monkeypatch.setattr(config, "WEBSITE_SHOP_ENABLED", True)
        importlib.reload(bot_module)
        assert "cogs.website_shop" in bot_module.COGS
        monkeypatch.setattr(config, "WEBSITE_SHOP_ENABLED", False)
        importlib.reload(bot_module)
