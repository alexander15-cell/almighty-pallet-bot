"""fb_marketplace_csv.py's pure row-building for Facebook's bulk listing
template (Title/Price/Description/Photo URL)."""
import json
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import fb_marketplace_csv


def _fake_item(pallet_id=1, item_number=1, **overrides):
    item = {
        "pallet_id": pallet_id,
        "item_number": item_number,
        "ai_description": "A description",
        "raw_description": "raw note",
        "photo_public_urls": json.dumps([]),
    }
    item.update(overrides)
    return item


def _fake_listing(**overrides):
    listing = {
        "ebay_title": "A Title",
        "price": 19.99,
    }
    listing.update(overrides)
    return listing


def test_batch_file_header_matches_facebooks_required_columns(tmp_path, monkeypatch):
    # Facebook's own docs: "Should not remove or replace the headers in the
    # CSV file. This will cause an error and stop you from uploading."
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    fb_marketplace_csv.append_item_to_batch(_fake_item(), _fake_listing())

    lines = (tmp_path / "batch.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Title,Price,Description,Photo URL"


def test_row_includes_title_price_and_description(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item(ai_description="A cordless drill, tested working")
    listing = _fake_listing(ebay_title="DeWalt Cordless Drill", price=45.0)
    fb_marketplace_csv.append_item_to_batch(item, listing)

    rows = fb_marketplace_csv._read_existing_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["Title"] == "DeWalt Cordless Drill"
    assert row["Price"] == "45"
    assert row["Description"] == "A cordless drill, tested working"


def test_price_is_rounded_to_nearest_whole_number(tmp_path, monkeypatch):
    # Facebook's docs: "This will be rounded to the nearest whole number."
    # Rounded here (not left to Facebook) so the CSV shows the actual value.
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    fb_marketplace_csv.append_item_to_batch(_fake_item(), _fake_listing(price=19.6))

    rows = fb_marketplace_csv._read_existing_rows()
    assert rows[0]["Price"] == "20"


def test_description_falls_back_to_raw_note_when_no_ai_description(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item(ai_description="", raw_description="just a raw note")
    fb_marketplace_csv.append_item_to_batch(item, _fake_listing())

    rows = fb_marketplace_csv._read_existing_rows()
    assert rows[0]["Description"] == "just a raw note"


def test_row_uses_first_photo_url_when_available(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    urls = ["https://example.com/1.jpg", "https://example.com/2.jpg"]
    item = _fake_item(photo_public_urls=json.dumps(urls))
    fb_marketplace_csv.append_item_to_batch(item, _fake_listing())

    rows = fb_marketplace_csv._read_existing_rows()
    assert rows[0]["Photo URL"] == "https://example.com/1.jpg"


def test_row_leaves_photo_url_blank_when_none_available(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    fb_marketplace_csv.append_item_to_batch(_fake_item(photo_public_urls=json.dumps([])), _fake_listing())

    rows = fb_marketplace_csv._read_existing_rows()
    assert rows[0]["Photo URL"] == ""


def test_row_leaves_price_blank_when_none_captured(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    fb_marketplace_csv.append_item_to_batch(_fake_item(), _fake_listing(price=None))

    rows = fb_marketplace_csv._read_existing_rows()
    assert rows[0]["Price"] == ""


def test_multiple_items_each_get_their_own_row(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    fb_marketplace_csv.append_item_to_batch(_fake_item(item_number=1), _fake_listing(ebay_title="Item One"))
    fb_marketplace_csv.append_item_to_batch(_fake_item(item_number=2), _fake_listing(ebay_title="Item Two"))

    rows = fb_marketplace_csv._read_existing_rows()
    assert [r["Title"] for r in rows] == ["Item One", "Item Two"]


def test_export_and_archive_returns_none_for_empty_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(fb_marketplace_csv, "ARCHIVE_DIR", tmp_path / "archive")
    assert fb_marketplace_csv.export_and_archive() is None


def test_export_and_archive_clears_the_live_file(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(fb_marketplace_csv, "ARCHIVE_DIR", tmp_path / "archive")
    fb_marketplace_csv.append_item_to_batch(_fake_item(), _fake_listing())

    archived_path = fb_marketplace_csv.export_and_archive()
    assert archived_path is not None
    assert archived_path.exists()
    assert not (tmp_path / "batch.csv").exists()


def test_archived_file_still_has_facebooks_required_header(tmp_path, monkeypatch):
    monkeypatch.setattr(fb_marketplace_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(fb_marketplace_csv, "ARCHIVE_DIR", tmp_path / "archive")
    fb_marketplace_csv.append_item_to_batch(_fake_item(), _fake_listing(ebay_title="Saved Title"))

    archived_path = fb_marketplace_csv.export_and_archive()
    lines = archived_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "Title,Price,Description,Photo URL"
    assert "Saved Title" in lines[1]
