"""ebay_csv.py's pure row-building - fixed price vs. auction *Format/
*Duration/*StartPrice, matching eBay's File Exchange conventions."""
import json

import config
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


def test_row_includes_configured_item_location(tmp_path, monkeypatch):
    # eBay rejects every row in a batch without *Location ("No <Item.Location>
    # exists") - a real upload failure this test guards against recurring.
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(config, "EBAY_ITEM_LOCATION", "Columbus, OH")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing())

    _, rows = ebay_csv._read_existing_rows()
    assert rows[0]["*Location"] == "Columbus, OH"


def test_existing_batch_file_gains_new_base_columns_on_next_append(tmp_path, monkeypatch):
    # A batch file already on disk from before *Location was added to
    # BASE_FIELDS must not keep missing it forever - the next append should
    # heal the header, not silently perpetuate the old (broken) one.
    batch_path = tmp_path / "batch.csv"
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", batch_path)
    batch_path.write_text("CustomLabel,*Category,*Title\npallet-1-item-1,999,Old Row\n", encoding="utf-8")

    monkeypatch.setattr(config, "EBAY_ITEM_LOCATION", "Columbus, OH")
    ebay_csv.append_item_to_batch(_fake_item(pallet_id=1, item_number=2), _fake_listing())

    fieldnames, rows = ebay_csv._read_existing_rows()
    assert "*Location" in fieldnames
    new_row = next(r for r in rows if r["CustomLabel"] == "pallet-1-item-2")
    assert new_row["*Location"] == "Columbus, OH"


def test_row_includes_configured_shipping_fields(tmp_path, monkeypatch):
    # eBay also rejects every row without a shipping service ("Please add
    # at least one valid shipping service option to your listing") - a
    # second real upload failure this test guards against recurring.
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    monkeypatch.setattr(config, "EBAY_SHIPPING_TYPE", "Flat")
    monkeypatch.setattr(config, "EBAY_SHIPPING_SERVICE", "USPSPriority")
    monkeypatch.setattr(config, "EBAY_SHIPPING_COST", "8.00")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing())

    _, rows = ebay_csv._read_existing_rows()
    row = rows[0]
    assert row["ShippingType"] == "Flat"
    assert row["ShippingService-1:Option"] == "USPSPriority"
    assert row["ShippingService-1:Cost"] == "8.00"
