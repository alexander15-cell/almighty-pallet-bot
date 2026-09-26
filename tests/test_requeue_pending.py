"""
database.get_all_items_pending_ebay_upload and /ebay requeue-pending
(cogs/ebay.py) - the bulk version of /ebay retry-item, for re-queuing
EVERY item currently pending an eBay upload into a fresh live batch at
once (e.g. after correcting stored data - a wrong R2_PUBLIC_URL_BASE -
that already-exported items' CSV rows were built from; fixing the
database alone doesn't retroactively fix a CSV file already downloaded).
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import cogs.ebay as ebay_module
import ebay_csv
import pytest
from cogs.ebay import Ebay


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
    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


@pytest.fixture(autouse=True)
def bypass_admin_check(monkeypatch):
    monkeypatch.setattr(ebay_module, "_is_pallet_admin", lambda interaction: True)


@pytest.fixture
def cog():
    return Ebay.__new__(Ebay)


def _pending_item(db, pallet_id, item_number_note, price=25.0, ebay_batch_id=None):
    item_id = db.create_item(pallet_id, item_number_note, [], 1)
    db.save_ebay_listing_data(
        item_id, ebay_title="Widget", category_id="123", condition_id="1500",
        price=price, item_specifics={}, weight_lb=1.0, length_in=5, width_in=5, height_in=5,
    )
    db.update_status(item_id, db.STATUS_PENDING_EBAY_UPLOAD, actor_id=1)
    if ebay_batch_id is not None:
        with db.get_conn() as conn:
            conn.execute("UPDATE items SET ebay_batch_id = ? WHERE id = ?", (ebay_batch_id, item_id))
    return item_id


# --------------------------------------------------------------- DB layer


def test_get_all_items_pending_ebay_upload_includes_already_batched(fresh_db):
    pallet_id = fresh_db.create_pallet("Requeue Pallet", category_id=1, created_by=1)
    already_exported = _pending_item(fresh_db, pallet_id, "widget 1", ebay_batch_id=5)
    never_exported = _pending_item(fresh_db, pallet_id, "widget 2", ebay_batch_id=None)

    items = fresh_db.get_all_items_pending_ebay_upload()
    ids = {i["id"] for i in items}
    assert already_exported in ids
    assert never_exported in ids


def test_get_all_items_pending_ebay_upload_excludes_other_statuses(fresh_db):
    pallet_id = fresh_db.create_pallet("Requeue Pallet 2", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_LISTED, actor_id=1)

    assert fresh_db.get_all_items_pending_ebay_upload() == []


# ------------------------------------------------------------------ command


def test_requeue_pending_no_items(fresh_db, cog):
    interaction = _FakeInteraction()
    asyncio.run(Ebay.requeue_pending.callback(cog, interaction))
    assert "No items are currently pending" in interaction.response.content


def test_requeue_pending_appends_every_item_to_live_batch(fresh_db, cog, tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    pallet_id = fresh_db.create_pallet("Requeue Pallet 3", category_id=3, created_by=1)
    item_id_1 = _pending_item(fresh_db, pallet_id, "widget 1", ebay_batch_id=5)
    item_id_2 = _pending_item(fresh_db, pallet_id, "widget 2", ebay_batch_id=5)

    interaction = _FakeInteraction()
    asyncio.run(Ebay.requeue_pending.callback(cog, interaction))

    assert "Re-queued 2 item(s)" in interaction.followup.content
    _, rows = ebay_csv._read_existing_rows()
    assert len(rows) == 2

    for item_id in (item_id_1, item_id_2):
        assert fresh_db.get_item(item_id)["ebay_batch_id"] is None  # detached from the old batch


def test_requeue_pending_skips_items_with_no_listing_data(fresh_db, cog, tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    pallet_id = fresh_db.create_pallet("Requeue Pallet 4", category_id=4, created_by=1)
    good_item = _pending_item(fresh_db, pallet_id, "widget 1", ebay_batch_id=5)
    no_listing_item = fresh_db.create_item(pallet_id, "widget 2", [], 1)
    fresh_db.update_status(no_listing_item, fresh_db.STATUS_PENDING_EBAY_UPLOAD, actor_id=1)

    interaction = _FakeInteraction()
    asyncio.run(Ebay.requeue_pending.callback(cog, interaction))

    assert "Re-queued 1 item(s)" in interaction.followup.content
    assert "Skipped 1 item(s)" in interaction.followup.content
    _, rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
