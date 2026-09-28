"""Offline recovery guarantees; no Discord connection or real data."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import config
import backup
import database as db
import finance_utils
import recovery_safety
import cogs.item_flow as flow
import cogs.admin_tools as admin
import cogs.pallet_setup as setup
import cogs.finance as finance
from website_contract import build_website_contract


@pytest.fixture(autouse=True)
def protection(monkeypatch):
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", True)
    monkeypatch.setattr(config, "GUILD_ID", 100)


def interaction(message_id=201):
    return SimpleNamespace(
        message=SimpleNamespace(id=message_id, delete=AsyncMock(), edit=AsyncMock()),
        guild=SimpleNamespace(id=100), user=SimpleNamespace(id=42),
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        client=None,
    )


def record(fresh_db, status=db.STATUS_LISTED):
    pallet_id = fresh_db.create_pallet("Recovered", 101, 42)
    item_id = fresh_db.create_item(pallet_id, "Verified visible description", [], 42)
    fresh_db.update_status(item_id, status, new_message_id=201)
    fresh_db.set_shared_channel("listed", 301)
    fresh_db.set_shared_channel("sold", 302)
    fresh_db.set_shared_channel("queue-review", 303)
    return fresh_db.get_item(item_id)


def test_real_default_is_unprotected_and_bad_switch_fails(tmp_path):
    # The bot's real default is UNprotected (matches its behavior before
    # this recovery mode existed) - set PRESERVE_DISCORD_HISTORY=true
    # explicitly to turn protection on for a recovery deployment.
    env = {**os.environ, "PYTHON_DOTENV_DISABLED": "1"}
    env.pop("PRESERVE_DISCORD_HISTORY", None)
    result = subprocess.run([sys.executable, "-c", "import config; assert not config.PRESERVE_DISCORD_HISTORY"],
                            cwd=Path(__file__).parents[1], env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    env["PRESERVE_DISCORD_HISTORY"] = "maybe"
    result = subprocess.run([sys.executable, "-c", "import config"], cwd=Path(__file__).parents[1],
                            env=env, capture_output=True)
    assert result.returncode != 0
    assert b"PRESERVE_DISCORD_HISTORY" in result.stderr


def test_old_card_cleanup_has_zero_discord_writes():
    message = interaction().message
    asyncio.run(flow.ItemFlow(None)._clear_old_card(message, 1, "recovery"))
    message.delete.assert_not_awaited()
    message.edit.assert_not_awaited()


def test_startup_does_not_start_loops(monkeypatch):
    for loop in (flow.ItemFlow.stale_check_loop, finance.Finance.credit_card_poll_loop,
                 admin.AdminTools.backup_loop):
        monkeypatch.setattr(loop, "start", Mock(side_effect=AssertionError("no timer")))
    fake = SimpleNamespace(add_dynamic_items=Mock(), user=SimpleNamespace(id=1), tree=Mock())
    async def run():
        await flow.ItemFlow(fake).cog_load()
        finance.Finance(fake)
        admin.AdminTools(fake)
    asyncio.run(run())


def test_resubmission_retains_original_discord_and_photo(fresh_db, tmp_path, monkeypatch):
    item = record(fresh_db, db.STATUS_REJECTED)
    fresh_db.map_channel(item["pallet_id"], "data-entry", 401)
    monkeypatch.setattr(flow, "PHOTO_DIR", tmp_path)
    folder = tmp_path / str(item["id"])
    folder.mkdir()
    old_photo = folder / "photo_0.jpg"
    old_photo.write_bytes(b"original-photo")
    old_card = interaction().message
    channel = SimpleNamespace(id=401, fetch_message=AsyncMock(return_value=old_card),
                              send=AsyncMock(), reply=AsyncMock())
    message = SimpleNamespace(
        id=501, channel=channel, author=SimpleNamespace(bot=False, id=42),
        reference=SimpleNamespace(message_id=201), content="Updated photo submission",
        attachments=[SimpleNamespace(filename="photo.jpg", read=AsyncMock(return_value=b"new-photo"))],
        delete=AsyncMock(),
    )
    cog = flow.ItemFlow(SimpleNamespace(get_channel=lambda _id: None))
    asyncio.run(cog.on_message(message))
    assert old_photo.read_bytes() == b"original-photo"
    assert (folder / "submission-501" / "photo_0.jpg").read_bytes() == b"new-photo"
    message.delete.assert_not_awaited()
    old_card.delete.assert_not_awaited()
    old_card.edit.assert_not_awaited()


@pytest.mark.parametrize("old_id,status,guild", [(200, db.STATUS_LISTED, 100),
                                               (201, db.STATUS_SOLD, 100),
                                               (201, db.STATUS_LISTED, 999)])
def test_current_card_guard_rejects_stale_wrong_status_wrong_guild(old_id, status, guild):
    event = interaction(old_id)
    event.guild.id = guild
    allowed = asyncio.run(recovery_safety.require_current_card(
        event, {"current_message_id": 201, "status": status}, db.STATUS_LISTED))
    assert allowed is False
    event.response.send_message.assert_awaited_once()


def test_modal_checks_captured_source_not_ephemeral_message():
    event = interaction(999)
    item = {"current_message_id": 201, "status": db.STATUS_QUEUE_REVIEW}
    assert asyncio.run(recovery_safety.require_current_card(
        event, item, db.STATUS_QUEUE_REVIEW, source_message_id=201))
    assert not asyncio.run(recovery_safety.require_current_card(
        event, item, db.STATUS_QUEUE_REVIEW, source_message_id=200))


@pytest.mark.parametrize("name", ["item_delete", "pallet_archive", "db_wipe"])
def test_destructive_commands_block_before_database_access(name, monkeypatch):
    event = interaction()
    monkeypatch.setattr(admin, "_require_admin", AsyncMock(side_effect=AssertionError("must block first")))
    method = getattr(admin.AdminTools, name)
    args = [object(), event] + ([1] if name == "item_delete" else [])
    asyncio.run(method.callback(*args))
    event.response.send_message.assert_awaited_once()


def test_previously_open_wipe_modal_is_also_blocked():
    async def run():
        event = interaction()
        modal = admin.ConfirmWipeModal(object())
        await modal.on_submit(event)
        event.response.send_message.assert_awaited_once()
    asyncio.run(run())


def test_information_refresh_is_blocked():
    event = interaction()
    asyncio.run(setup.PalletSetup.setup_info_channel.callback(object(), event))
    event.response.send_message.assert_awaited_once()


def test_backup_retention_never_removes_snapshot(tmp_path, monkeypatch):
    saved = tmp_path / "backup_20000101_000000.zip"
    saved.write_bytes(b"immutable recovery snapshot")
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(config, "BACKUP_KEEP_COUNT", 0)
    backup._apply_retention()
    assert saved.read_bytes() == b"immutable recovery snapshot"


def test_background_loops_and_finance_refresh_return_before_io(monkeypatch):
    monkeypatch.setattr(db, "get_stale_listed_items", Mock(side_effect=AssertionError("no read/write")))
    monkeypatch.setattr(db, "get_pallet", Mock(side_effect=AssertionError("no refresh")))
    monkeypatch.setattr(finance.quickbooks, "is_connected", Mock(side_effect=AssertionError("no API")))
    monkeypatch.setattr(backup, "create_backup", Mock(side_effect=AssertionError("no backup task")))
    async def run():
        await flow.ItemFlow.stale_check_loop.coro(object())
        await finance.Finance.credit_card_poll_loop.coro(object())
        await admin.AdminTools.backup_loop.coro(object())
        await finance_utils.refresh_finance_message(None, 1)
    asyncio.run(run())


def test_stale_mark_sold_cannot_mutate_item(fresh_db, monkeypatch):
    item = record(fresh_db)
    cog = flow.ItemFlow(None)
    monkeypatch.setattr(cog, "_require_role", AsyncMock(return_value=True))
    event = interaction(200)
    asyncio.run(cog.move_to_sold(event, item["id"]))
    assert db.get_item(item["id"])["status"] == db.STATUS_LISTED
    event.message.delete.assert_not_awaited()
    event.message.edit.assert_not_awaited()


def test_shipping_appends_and_keeps_original(fresh_db, monkeypatch):
    item = record(fresh_db, db.STATUS_SOLD)
    event = interaction()
    channel = object()
    bot = SimpleNamespace(get_channel=lambda _id: channel)
    cog = flow.ItemFlow(bot)
    monkeypatch.setattr(cog, "_require_role", AsyncMock(return_value=True))
    sender = AsyncMock(return_value=SimpleNamespace(id=202))
    monkeypatch.setattr(flow, "send_item_card", sender)
    asyncio.run(cog.mark_shipped(event, item["id"]))
    assert db.get_item(item["id"])["status"] == db.STATUS_SHIPPED
    assert db.get_item(item["id"])["current_message_id"] == 202
    assert sender.await_args.kwargs["destination_status"] == db.STATUS_SHIPPED
    event.message.edit.assert_not_awaited()
    event.message.delete.assert_not_awaited()


def test_edit_appends_and_stale_modal_cannot_overwrite(fresh_db, monkeypatch):
    item = record(fresh_db, db.STATUS_QUEUE_REVIEW)
    event = interaction()
    cog = SimpleNamespace(_require_role=AsyncMock(return_value=True))
    event.client = SimpleNamespace(get_cog=lambda _name: cog, get_channel=lambda _id: object())
    sender = AsyncMock(return_value=SimpleNamespace(id=202))
    monkeypatch.setattr(flow, "send_item_card", sender)
    async def run():
        modal = flow.EditDescriptionModal(item["id"], source_message_id=201)
        modal.new_description._value = "Reviewed new description"
        await modal.on_submit(event)
        assert db.get_item(item["id"])["current_message_id"] == 202
        assert db.get_item(item["id"])["ai_description"] == "Reviewed new description"
        modal.new_description._value = "Stale description"
        await modal.on_submit(event)
        assert db.get_item(item["id"])["ai_description"] == "Reviewed new description"
    asyncio.run(run())
    sender.assert_awaited_once()
    event.message.edit.assert_not_awaited()
    event.message.delete.assert_not_awaited()


def test_delayed_approval_rejects_changed_source_before_saving(fresh_db, monkeypatch):
    item = record(fresh_db, db.STATUS_QUEUE_REVIEW)
    event = interaction(999)
    cog = flow.ItemFlow(None)
    monkeypatch.setattr(cog, "_require_role", AsyncMock(return_value=True))
    asyncio.run(cog.finalize_ebay_approval(
        event, item["id"], "3000", "1", "Title", 19.99, {},
        source_message_id=200,
    ))
    assert db.get_ebay_listing_data(item["id"]) is None
    assert db.get_item(item["id"])["current_message_id"] == 201


def test_review_origin_survives_format_to_modal(fresh_db):
    item = record(fresh_db, db.STATUS_QUEUE_REVIEW)
    async def run():
        event = interaction(999)
        view = flow.EbayFormatSelectView(item["id"], "3000", "1", source_message_id=201)
        view.select._values = ["FixedPrice"]
        await view._on_select(event)
        modal = event.response.send_modal.await_args.args[0]
        assert modal.source_message_id == 201
    asyncio.run(run())


@pytest.mark.parametrize("condition", ["1750", "7000", "", "mystery"])
def test_faulty_or_unknown_condition_never_becomes_untested_listing(condition):
    result = build_website_contract(
        {"id": 1, "pallet_id": 1, "item_number": 1, "raw_description": "Verified"},
        {"ebay_title": "Title", "price": "15.00", "condition_id": condition, "listing_format": "FixedPrice"},
        status="listed", photo_count=1, sku_prefix="MAIN-",
    )
    fields = {f["name"]: f["value"] for f in result["fields"]}
    assert fields["Website issue"] == "unsupported_condition"
    assert fields["Website SKU"] == "MAIN-PALLET-1-ITEM-1"
    assert "Website price cents" not in fields


@pytest.mark.parametrize("prefix", ["../", "main-", "MAIN SPACE-", None])
def test_invalid_prefix_fails_closed(prefix):
    with pytest.raises(ValueError):
        build_website_contract({"id": 1, "pallet_id": 1, "item_number": 1}, {},
                               status="listed", photo_count=1, sku_prefix=prefix)
