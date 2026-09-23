"""
/admin purge-old-photos (cogs/admin_tools.py) and the R2-wipe step added to
/admin db-wipe's ConfirmWipeModal. Calls the command callback / modal
on_submit directly with minimal fake Discord objects, matching this
codebase's existing test style (see tests/test_pirate_ship_assignment.py).
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import cogs.admin_tools as admin_tools_module
import r2_storage
from cogs.admin_tools import AdminTools, ConfirmWipeModal


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
    def __init__(self, guild=None):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.user = type("U", (), {"id": 1})()
        self.guild = guild


class _FakeGuild:
    """No categories/channels - isolates the wipe modal's DB/R2 steps from
    its Discord channel-deletion loops, which are unrelated to this fix."""
    def get_channel(self, channel_id):
        return None


@pytest.fixture(autouse=True)
def bypass_admin_check(monkeypatch):
    monkeypatch.setattr(admin_tools_module, "_is_pallet_admin", lambda interaction: True)


@pytest.fixture
def cog():
    return AdminTools.__new__(AdminTools)


def _field(content, needle):
    return needle in (content or "")


# ---------------------------------------------------------- purge-old-photos


def test_purge_old_photos_when_r2_not_configured(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", False)
    interaction = _FakeInteraction()
    asyncio.run(AdminTools.purge_old_photos.callback(cog, interaction, days=None, confirm=False))
    assert "R2 isn't configured" in interaction.response.content


def test_purge_old_photos_negative_days_rejected(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", True)
    interaction = _FakeInteraction()
    asyncio.run(AdminTools.purge_old_photos.callback(cog, interaction, days=-1, confirm=False))
    assert "can't be negative" in interaction.response.content


def test_purge_old_photos_no_candidates(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", True)
    interaction = _FakeInteraction()
    asyncio.run(AdminTools.purge_old_photos.callback(cog, interaction, days=30, confirm=False))
    assert "No sold items" in interaction.response.content


def test_purge_old_photos_preview_does_not_delete(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", True)
    pallet_id = fresh_db.create_pallet("Purge Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://pub.r2.dev/items/1/a.jpg"])
    with fresh_db.get_conn() as conn:
        import datetime as dt
        old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat()
        conn.execute("UPDATE items SET status = ?, sold_at = ? WHERE id = ?", (fresh_db.STATUS_SOLD, old_ts, item_id))

    called = {"n": 0}
    monkeypatch.setattr(r2_storage, "delete_photos", lambda keys: called.__setitem__("n", called["n"] + 1))

    interaction = _FakeInteraction()
    asyncio.run(AdminTools.purge_old_photos.callback(cog, interaction, days=30, confirm=False))

    assert "Preview only" in interaction.response.content
    assert called["n"] == 0
    assert fresh_db.get_item(item_id)["photo_public_urls"] is not None


def test_purge_old_photos_confirm_deletes_and_clears(fresh_db, cog, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", True)
    monkeypatch.setattr(config, "R2_PUBLIC_URL_BASE", "https://pub.r2.dev")
    pallet_id = fresh_db.create_pallet("Purge Pallet 2", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://pub.r2.dev/items/2/a.jpg"])
    with fresh_db.get_conn() as conn:
        import datetime as dt
        old_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=45)).isoformat()
        conn.execute("UPDATE items SET status = ?, sold_at = ? WHERE id = ?", (fresh_db.STATUS_SOLD, old_ts, item_id))

    deleted_keys = []
    monkeypatch.setattr(r2_storage, "delete_photos", lambda keys: deleted_keys.extend(keys) or len(keys))

    interaction = _FakeInteraction()
    asyncio.run(AdminTools.purge_old_photos.callback(cog, interaction, days=30, confirm=True))

    assert deleted_keys == ["items/2/a.jpg"]
    assert fresh_db.get_item(item_id)["photo_public_urls"] is None
    assert "Deleted" in interaction.followup.content


# ------------------------------------------------------------------ db-wipe


def test_db_wipe_calls_r2_wipe_when_enabled(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", True)
    monkeypatch.setattr(config, "DB_WIPE_CONFIRMATION_PHRASE", "DELETE EVERYTHING")

    wipe_called = {"n": 0}
    monkeypatch.setattr(r2_storage, "wipe_all_photos", lambda: wipe_called.__setitem__("n", 7) or 7)

    cog = AdminTools.__new__(AdminTools)
    modal = ConfirmWipeModal(cog)
    modal.confirmation._value = "DELETE EVERYTHING"

    interaction = _FakeInteraction(guild=_FakeGuild())
    asyncio.run(modal.on_submit(interaction))

    assert wipe_called["n"] == 7
    assert "7 photo(s) from R2" in interaction.followup.content


def test_db_wipe_skips_r2_wipe_when_disabled(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", False)
    monkeypatch.setattr(config, "DB_WIPE_CONFIRMATION_PHRASE", "DELETE EVERYTHING")

    def fail_if_called():
        raise AssertionError("should not have called wipe_all_photos - R2 disabled")
    monkeypatch.setattr(r2_storage, "wipe_all_photos", fail_if_called)

    cog = AdminTools.__new__(AdminTools)
    modal = ConfirmWipeModal(cog)
    modal.confirmation._value = "DELETE EVERYTHING"

    interaction = _FakeInteraction(guild=_FakeGuild())
    asyncio.run(modal.on_submit(interaction))

    assert "R2" not in interaction.followup.content


def test_db_wipe_reports_r2_failure_without_crashing(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "R2_ENABLED", True)
    monkeypatch.setattr(config, "DB_WIPE_CONFIRMATION_PHRASE", "DELETE EVERYTHING")

    def broken_wipe():
        raise RuntimeError("R2 unreachable")
    monkeypatch.setattr(r2_storage, "wipe_all_photos", broken_wipe)

    cog = AdminTools.__new__(AdminTools)
    modal = ConfirmWipeModal(cog)
    modal.confirmation._value = "DELETE EVERYTHING"

    interaction = _FakeInteraction(guild=_FakeGuild())
    asyncio.run(modal.on_submit(interaction))

    assert "Failed to wipe R2 photos" in interaction.followup.content
    # The rest of the wipe still completed and reported success.
    assert "Database wiped" in interaction.followup.content
