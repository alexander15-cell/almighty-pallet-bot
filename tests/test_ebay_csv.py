"""ebay_csv.py's pure row-building - fixed price vs. auction *Format/
*Duration/*StartPrice, matching eBay's File Exchange conventions."""
import json

import ebay_csv


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


def test_fixed_price_row_uses_gtc_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item()
    listing = _fake_listing(listing_format="FixedPrice")
    ebay_csv.append_item_to_batch(item, listing)

    fieldnames, rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["*Format"] == "FixedPrice"
    assert row["*Duration"] == "GTC"
    assert row["*StartPrice"] == "19.99"
    assert row["C:Brand"] == "Test"


def test_auction_row_uses_its_own_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item()
    listing = _fake_listing(listing_format="Auction", auction_duration="Days_7", price=5.00)
    ebay_csv.append_item_to_batch(item, listing)

    _, rows = ebay_csv._read_existing_rows()
    row = rows[0]
    assert row["*Format"] == "Auction"
    assert row["*Duration"] == "Days_7"
    assert row["*StartPrice"] == "5.00"


def test_readding_same_item_replaces_its_row_not_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item()
    ebay_csv.append_item_to_batch(item, _fake_listing(price=10.00))
    ebay_csv.append_item_to_batch(item, _fake_listing(price=20.00))  # corrected price

    _, rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
    assert rows[0]["*StartPrice"] == "20.00"
