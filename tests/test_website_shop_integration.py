"""
The website-shop feature integrated directly into this bot's own process
(cogs/website_shop.py) - as opposed to tests/test_combined_intake.py, which
unit-tests combined_intake.py/combined_delivery.py's own COMBINED_MODE code
path directly, independent of any particular bot process.

Covers the three actual code changes this integration needed:
- combined_intake.IntakeAdapter accepting config.WEBSITE_SHOP_ENABLED as an
  alternative to COMBINED_MODE + PRESERVE_DISCORD_HISTORY.
- IntakeAdapter._owned_item() skipping the data-entry channel-identity check
  when "data-entry" isn't a key in settings.channels at all (this bot has one
  such channel per pallet, not one for the whole server).
- database.ensure_items_autoincrement_floor().
"""
import asyncio
import importlib
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

import backup
import config
import database as db
from cogs.website_shop import WebsiteShop
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
    # this bot has one per pallet, not a single fixed channel, which is why
    # IntakeAdapter._owned_item() only checks "data-entry" identity when
    # that key is actually present in settings.channels.
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


def test_ensure_items_autoincrement_floor_works_on_a_database_with_existing_items(fresh_db):
    """
    Regression guard: a real install always has real pre-existing items
    (and therefore an existing items.sqlite_sequence row) by the time this
    feature gets turned on - unlike test_..._is_idempotent_and_never_lowers
    above, which only exercises a brand-new database with zero items, where
    no such row exists yet. Those are different code paths against SQLite's
    special-cased sqlite_sequence bookkeeping table: "INSERT OR REPLACE"
    against an EXISTING row there silently fails to persist (confirmed by
    reproducing it directly against a scratch database), even though it
    reports success and raises nothing, while a plain INSERT (no existing
    row) works fine - which is exactly why the first test alone didn't
    catch this.
    """
    pallet_id = fresh_db.create_pallet("Existing Pallet", category_id=1, created_by=1)
    for i in range(5):
        fresh_db.create_item(pallet_id, f"Pre-existing item {i}", [], 1)

    assert db.ensure_items_autoincrement_floor(1_000_000_000) is True
    new_item_id = fresh_db.create_item(pallet_id, "New item after bump", [], 1)
    assert new_item_id >= 1_000_000_000


def test_item_flow_saves_absolute_photo_paths_even_when_config_path_is_relative(tmp_path, monkeypatch):
    """
    Regression guard: config.PHOTO_DIR defaults to a relative path
    ("data/photos" in production), and cogs/item_flow.py used to compute its
    own PHOTO_DIR constant as `Path(config.PHOTO_DIR)` unchanged - so every
    photo path saved into an item's photo_urls was a relative string.
    combined_intake.py's _photos() requires every stored path to be
    absolute - a real security boundary,
    not something to loosen - so every real item created through the normal
    Data Entry flow was silently rejected from #website_shop with
    "invalid_intake_photos", the very first time any item ever reached that
    check in production. This was masked by every existing test building
    its own already-absolute tmp_path photo paths by hand, never through
    item_flow.py's own PHOTO_DIR/photo_dir_for(). Confirmed live: after
    fixing database.ensure_items_autoincrement_floor (a separate bug), the
    first item to actually reach reconcile() was blocked with exactly
    "invalid_intake_photos".
    """
    original_env = os.environ.get("PHOTO_DIR")
    import cogs.item_flow as flow_module
    try:
        monkeypatch.chdir(tmp_path)
        os.environ["PHOTO_DIR"] = "data/photos"  # relative, matching production's default
        importlib.reload(config)
        importlib.reload(flow_module)

        assert flow_module.PHOTO_DIR.is_absolute()
        folder = flow_module.photo_dir_for(999)
        assert folder.is_absolute()
        assert folder == tmp_path / "data" / "photos" / "999"
    finally:
        if original_env is not None:
            os.environ["PHOTO_DIR"] = original_env
        importlib.reload(config)
        importlib.reload(flow_module)


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


def test_cog_load_does_not_touch_bot_user(monkeypatch):
    # cog_load() runs during bot.load_extension(), BEFORE bot.start() logs
    # in - self.bot.user is None at that point. Regression test for exactly
    # that: cog_load() must not read it (construction happens later, in
    # poll_loop's before_loop, once the bot has actually logged in).
    monkeypatch.setattr(config, "WEBSITE_SHOP_POLL_SECONDS", 3600)

    async def _never_ready():
        await asyncio.sleep(3600)

    fake_bot = SimpleNamespace(user=None, wait_until_ready=_never_ready)
    cog = WebsiteShop(fake_bot)
    run(cog.cog_load())
    try:
        assert cog.store is None  # not constructed yet - still pre-login
    finally:
        cog.poll_loop.cancel()


def test_ensure_ready_constructs_once_bot_is_logged_in(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WEBSITE_SHOP_ENABLED", True)
    monkeypatch.setattr(config, "WEBSITE_SHOP_CHANNEL_ID", int(SHOP))
    monkeypatch.setattr(config, "WEBSITE_SHOP_OPERATOR_IDS", (USER,))
    monkeypatch.setattr(config, "WEBSITE_SHOP_APPROVAL_DB_PATH", str(tmp_path / "shop_approval.sqlite"))
    monkeypatch.setattr(config, "WEBSITE_SHOP_JOURNAL_DB_PATH", str(tmp_path / "website_journal.sqlite"))
    monkeypatch.setattr(config, "WEBSITE_URL", "https://example.com")
    monkeypatch.setattr(config, "WEBSITE_SOURCE_ID", "a" * 32)
    monkeypatch.setattr(config, "WEBSITE_SECRET", "")
    monkeypatch.setattr(config, "WEBSITE_PUBLISH_ENABLED", False)
    monkeypatch.setattr(config, "GUILD_ID", int(GUILD))
    fresh_db.set_shared_channel("listed", int(LISTED))
    fresh_db.set_shared_channel("sold", int(SOLD))

    # bot.user is None until "login" happens - matches the real timing.
    fake_bot = SimpleNamespace(user=None, get_channel=lambda _id: None, add_view=Mock())
    cog = WebsiteShop(fake_bot)
    assert cog.store is None
    fake_bot.user = SimpleNamespace(id=int(APP))  # "login" completes
    run(cog._ensure_ready())
    try:
        assert cog.store is not None
        assert cog.adapter is not None
        assert cog.journal is not None
        assert cog.delivery is not None
        # Idempotent - a second call (e.g. a reconnect) must not rebuild.
        store_before = cog.store
        run(cog._ensure_ready())
        assert cog.store is store_before
    finally:
        cog.adapter.close()
        cog.store.close()
        cog.journal.close()
