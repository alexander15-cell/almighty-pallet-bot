"""ebay_csv.py's pure row-building for eBay's AI-prefill bulk template
(Custom Label (SKU) / Item Photo URL / Title / Category / Aspects, plus the
two required #INFO metadata rows before the header)."""
import json
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import ebay_csv
import ebay_taxonomy


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
        "category_id": "12345",
        "condition_id": "1500",
        "ebay_title": "A Title",
        "price": 19.99,
        "listing_format": "FixedPrice",
        "auction_duration": None,
        "item_specifics": {"Brand": "Test"},
    }
    listing.update(overrides)
    return listing


def test_custom_label_format():
    item = _fake_item(pallet_id=3, item_number=7)
    assert ebay_csv.custom_label(item) == "pallet-3-item-7"


def test_batch_file_starts_with_ebays_required_info_rows(tmp_path, monkeypatch):
    # eBay's own template instructions say "Do not change any formatting in
    # the file" - these two rows (version + which input sets are used) must
    # come before the header exactly as eBay's own template has them.
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing())

    lines = (tmp_path / "batch.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#INFO,Version=1.0.0,,Template=eBay-taxonomy-mapping-template_US,"
    assert lines[1] == "#INFO,Set A,,Set B,"
    assert lines[2] == "Custom Label (SKU),Item Photo URL,Title,Category,Aspects"


def test_row_includes_sku_title_and_resolved_category(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item(pallet_id=2, item_number=5)
    listing = _fake_listing(category_id="176937", ebay_title="A Nice Ceiling Fan")
    ebay_csv.append_item_to_batch(item, listing)

    rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["Custom Label (SKU)"] == "pallet-2-item-5"
    assert row["Title"] == "A Nice Ceiling Fan"
    assert row["Category"] == ebay_taxonomy.get_path("176937")
    assert "Ceiling Fans" in row["Category"]


def test_row_leaves_category_blank_for_unresolved_id(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    listing = _fake_listing(category_id="not-a-real-id")
    ebay_csv.append_item_to_batch(_fake_item(), listing)

    rows = ebay_csv._read_existing_rows()
    assert rows[0]["Category"] == ""


def test_row_formats_aspects_as_pipe_separated_key_value_pairs(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    listing = _fake_listing(item_specifics={"Brand": "DeWalt", "Voltage": "20V"})
    ebay_csv.append_item_to_batch(_fake_item(), listing)

    rows = ebay_csv._read_existing_rows()
    assert rows[0]["Aspects"] == "Brand=DeWalt|Voltage=20V"


def test_row_leaves_aspects_blank_when_no_specifics(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing(item_specifics={}))

    rows = ebay_csv._read_existing_rows()
    assert rows[0]["Aspects"] == ""


def test_row_joins_all_photo_urls_with_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    urls = ["https://example.com/1.jpg", "https://example.com/2.jpg", "https://example.com/3.jpg"]
    item = _fake_item(photo_public_urls=json.dumps(urls))
    ebay_csv.append_item_to_batch(item, _fake_listing())

    rows = ebay_csv._read_existing_rows()
    assert rows[0]["Item Photo URL"] == "|".join(urls)


def test_row_caps_photo_urls_at_ebays_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    urls = [f"https://example.com/{i}.jpg" for i in range(30)]
    item = _fake_item(photo_public_urls=json.dumps(urls))
    ebay_csv.append_item_to_batch(item, _fake_listing())

    rows = ebay_csv._read_existing_rows()
    assert rows[0]["Item Photo URL"].count("|") == ebay_csv.MAX_PHOTO_URLS - 1


def test_row_leaves_photo_url_blank_when_none_available(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(photo_public_urls=json.dumps([])), _fake_listing())

    rows = ebay_csv._read_existing_rows()
    assert rows[0]["Item Photo URL"] == ""


def test_readding_same_item_replaces_its_row_not_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item()
    ebay_csv.append_item_to_batch(item, _fake_listing(ebay_title="First Title"))
    ebay_csv.append_item_to_batch(item, _fake_listing(ebay_title="Corrected Title"))

    rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
    assert rows[0]["Title"] == "Corrected Title"


def test_export_and_archive_returns_none_for_empty_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(ebay_csv, "ARCHIVE_DIR", tmp_path / "archive")
    assert ebay_csv.export_and_archive() is None


def test_export_and_archive_clears_the_live_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(ebay_csv, "ARCHIVE_DIR", tmp_path / "archive")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing())

    archived_path = ebay_csv.export_and_archive()
    assert archived_path is not None
    assert archived_path.exists()
    assert not (tmp_path / "batch.csv").exists()
