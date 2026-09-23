"""
get_photo_purge_candidates / clear_photo_public_urls (database.py) - the
retention-window R2 photo cleanup for sold items (/admin purge-old-photos),
mirroring get_buyer_data_purge_candidates/purge_buyer_data's shape. Also
covers wipe_database() dropping the pallet/item-scoped tables added since
it was first written (finance_transactions, pallet_costs,
awaiting_pallet_charges, ebay_batches) while preserving infrastructure/
company-level tables (shared_channels, quickbooks_connection,
quickbooks_seen_charges).
"""
from datetime import datetime, timedelta, timezone


def _mark_sold_days_ago(db, item_id, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET status = ?, sold_at = ? WHERE id = ?", (db.STATUS_SOLD, ts, item_id))


def test_photo_purge_candidates_respects_retention_window(fresh_db):
    pallet_id = fresh_db.create_pallet("Photo Pallet", category_id=1, created_by=1)

    old_item = fresh_db.create_item(pallet_id, "old item", [], 1)
    fresh_db.update_photo_public_urls(old_item, ["https://pub.r2.dev/items/1/a.jpg"])
    _mark_sold_days_ago(fresh_db, old_item, 45)

    recent_item = fresh_db.create_item(pallet_id, "recent item", [], 1)
    fresh_db.update_photo_public_urls(recent_item, ["https://pub.r2.dev/items/2/a.jpg"])
    _mark_sold_days_ago(fresh_db, recent_item, 5)

    candidates = fresh_db.get_photo_purge_candidates(30)
    assert [c["id"] for c in candidates] == [old_item]


def test_photo_purge_candidates_excludes_items_without_r2_urls(fresh_db):
    pallet_id = fresh_db.create_pallet("Photo Pallet 2", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "no r2 photos", [], 1)
    _mark_sold_days_ago(fresh_db, item_id, 45)
    # No update_photo_public_urls call - photo_public_urls stays NULL

    assert fresh_db.get_photo_purge_candidates(30) == []


def test_photo_purge_candidates_includes_shipped_items_too(fresh_db):
    pallet_id = fresh_db.create_pallet("Photo Pallet 3", category_id=3, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "shipped item", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://pub.r2.dev/items/3/a.jpg"])
    _mark_sold_days_ago(fresh_db, item_id, 45)
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET status = ? WHERE id = ?", (fresh_db.STATUS_SHIPPED, item_id))

    candidates = fresh_db.get_photo_purge_candidates(30)
    assert [c["id"] for c in candidates] == [item_id]


def test_clear_photo_public_urls_clears_only_r2_not_local(fresh_db):
    pallet_id = fresh_db.create_pallet("Photo Pallet 4", category_id=4, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://pub.r2.dev/items/4/a.jpg"])
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls = ? WHERE id = ?", ('["local/path/a.jpg"]', item_id))

    fresh_db.clear_photo_public_urls([item_id], actor_id=1)

    updated = fresh_db.get_item(item_id)
    assert updated["photo_public_urls"] is None
    assert updated["photo_urls"] == '["local/path/a.jpg"]'  # local copy untouched


def test_clear_photo_public_urls_logs_an_item_event(fresh_db):
    pallet_id = fresh_db.create_pallet("Photo Pallet 5", category_id=5, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://pub.r2.dev/items/5/a.jpg"])

    fresh_db.clear_photo_public_urls([item_id], actor_id=1)

    with fresh_db.get_conn() as conn:
        events = conn.execute(
            "SELECT * FROM item_events WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item_id,)
        ).fetchall()
    assert "R2-hosted photos purged" in events[0]["note"]


def test_photo_purge_candidates_after_clear_is_empty(fresh_db):
    pallet_id = fresh_db.create_pallet("Photo Pallet 6", category_id=6, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.update_photo_public_urls(item_id, ["https://pub.r2.dev/items/6/a.jpg"])
    _mark_sold_days_ago(fresh_db, item_id, 45)

    assert len(fresh_db.get_photo_purge_candidates(30)) == 1
    fresh_db.clear_photo_public_urls([item_id], actor_id=1)
    assert fresh_db.get_photo_purge_candidates(30) == []


# ------------------------------------------------------------- wipe_database


def test_wipe_database_clears_pallet_item_scoped_tables(fresh_db):
    pallet_id = fresh_db.create_pallet("Wipe Test Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "item", [], 1)
    fresh_db.set_pallet_cost(pallet_id, 100.0, actor_id=1)
    fresh_db.add_pallet_cost(pallet_id, "shipping", 5.0, source="pirateship")
    fresh_db.record_expense(pallet_id, 2.0, "tape", actor_id=1)
    fresh_db.create_awaiting_pallet_charge("txn-1", 10.0, "Merchant", "2026-01-01", allocated_by=1)

    fresh_db.wipe_database()

    assert fresh_db.get_pallet(pallet_id) is None
    assert fresh_db.get_item(item_id) is None
    with fresh_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM pallet_costs").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM finance_transactions").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM awaiting_pallet_charges").fetchone()["n"] == 0


def test_wipe_database_preserves_infrastructure_tables(fresh_db):
    fresh_db.set_shared_channel("queue-review", 12345)
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at="2099-01-01T00:00:00+00:00",
    )
    fresh_db.mark_quickbooks_txn_seen("txn-seen-1")

    fresh_db.wipe_database()

    assert fresh_db.get_shared_channel_id("queue-review") == 12345
    assert fresh_db.get_quickbooks_connection() is not None
    assert fresh_db.has_seen_quickbooks_txn("txn-seen-1") is True


def test_wipe_database_is_safe_to_call_with_nothing_to_wipe(fresh_db):
    fresh_db.wipe_database()  # should not raise on an already-empty database
