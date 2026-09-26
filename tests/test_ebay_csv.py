"""ebay_csv.py's pure row-building for eBay's classic File Exchange "Add"
template (Action/CustomLabel/*Category/*Title/*ConditionID/.../
ShippingProfileName/ReturnProfileName/PaymentProfileName, plus dynamic
C:<Specific> columns)."""
import json
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

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
        "item_specifics": {"Brand": "Test", "Type": "Widget"},
        "weight_lb": 2.0,
        "length_in": 10,
        "width_in": 8,
        "height_in": 4,
    }
    listing.update(overrides)
    return listing


def _row(tmp_path, monkeypatch, listing_overrides=None, item_overrides=None):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(**(item_overrides or {})), _fake_listing(**(listing_overrides or {})))
    _, rows = ebay_csv._read_existing_rows()
    return rows[0]


def test_custom_label_format():
    item = _fake_item(pallet_id=3, item_number=7)
    assert ebay_csv.custom_label(item) == "pallet-3-item-7"


def test_row_includes_core_add_template_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item(pallet_id=2, item_number=5)
    listing = _fake_listing(category_id="176937", ebay_title="A Nice Ceiling Fan", price=45.5)
    ebay_csv.append_item_to_batch(item, listing)

    fieldnames, rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8)"] == "Add"
    assert row["CustomLabel"] == "pallet-2-item-5"
    assert row["*Category"] == "176937"
    assert row["*Title"] == "A Nice Ceiling Fan"
    assert row["*StartPrice"] == "45.50"
    assert row["*Quantity"] == "1"


def test_duration_is_gtc_for_fixed_price(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"listing_format": "FixedPrice"})
    assert row["*Format"] == "FixedPrice"
    assert row["*Duration"] == "GTC"


def test_duration_uses_auction_duration_for_auctions(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"listing_format": "Auction", "auction_duration": "Days_7"})
    assert row["*Format"] == "Auction"
    assert row["*Duration"] == "Days_7"


def test_row_includes_business_policy_names(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EBAY_SHIPPING_PROFILE_NAME", "Shipping")
    monkeypatch.setattr(config, "EBAY_RETURN_PROFILE_NAME", "Returns")
    monkeypatch.setattr(config, "EBAY_PAYMENT_PROFILE_NAME", "Payment")
    row = _row(tmp_path, monkeypatch)
    assert row["ShippingProfileName"] == "Shipping"
    assert row["ReturnProfileName"] == "Returns"
    assert row["PaymentProfileName"] == "Payment"


def test_row_includes_postal_code(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EBAY_SHIP_FROM_POSTAL_CODE", "47380")
    row = _row(tmp_path, monkeypatch)
    assert row["PostalCode"] == "47380"


def test_row_uses_real_captured_weight(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"weight_lb": 5.5})
    assert row["WeightMajor"] == "5"
    assert row["WeightMinor"] == "8"


def test_row_estimates_weight_when_missing_by_category_keyword(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB", {"ceiling fan": 8.0})
    monkeypatch.setattr(config, "EBAY_DEFAULT_WEIGHT_FALLBACK_LB", 2.0)
    row = _row(tmp_path, monkeypatch, {"weight_lb": None, "category_id": "176937"})  # a real Ceiling Fans leaf ID
    assert row["WeightMajor"] == "8"
    assert row["WeightMinor"] == "0"
    assert "no captured weight" in capsys.readouterr().out


def test_row_falls_back_to_generic_weight_when_no_category_keyword_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB", {"ceiling fan": 8.0})
    monkeypatch.setattr(config, "EBAY_DEFAULT_WEIGHT_FALLBACK_LB", 3.0)
    row = _row(tmp_path, monkeypatch, {"weight_lb": None, "category_id": "not-a-real-id"}, {})
    assert row["WeightMajor"] == "3"
    assert row["WeightMinor"] == "0"


def test_row_includes_real_package_dimensions(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"length_in": 12, "width_in": 6, "height_in": 3})
    assert row["PackageLength"] == "12"
    assert row["PackageWidth"] == "6"
    assert row["PackageDepth"] == "3"


def test_row_leaves_dimensions_blank_when_missing(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"length_in": None, "width_in": None, "height_in": None})
    assert row["PackageLength"] == ""
    assert row["PackageWidth"] == ""
    assert row["PackageDepth"] == ""


def test_brand_defaults_to_does_not_apply_when_missing(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"item_specifics": {}})
    assert row["C:Brand"] == "Does Not Apply"


def test_brand_preserves_real_value_when_present(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"item_specifics": {"Brand": "DeWalt"}})
    assert row["C:Brand"] == "DeWalt"


def test_brand_not_duplicated_when_already_lowercase(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing(item_specifics={"brand": "DeWalt"}))
    fieldnames, rows = ebay_csv._read_existing_rows()
    assert "C:Brand" not in fieldnames
    assert rows[0]["C:brand"] == "DeWalt"


def test_mpn_defaults_to_does_not_apply_when_missing(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"item_specifics": {}})
    assert row["C:MPN"] == "Does Not Apply"


def test_mpn_preserves_real_value_when_present(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"item_specifics": {"MPN": "ABC-123"}})
    assert row["C:MPN"] == "ABC-123"


def test_mpn_not_duplicated_when_already_lowercase(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(), _fake_listing(item_specifics={"mpn": "ABC-123"}))
    fieldnames, rows = ebay_csv._read_existing_rows()
    assert "C:MPN" not in fieldnames
    assert rows[0]["C:mpn"] == "ABC-123"


def test_type_inferred_from_title_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EBAY_TYPE_KEYWORDS", {"pendant": "Pendant"})
    row = _row(tmp_path, monkeypatch, {"item_specifics": {}, "ebay_title": "Nice Pendant Light Fixture"})
    assert row["C:Type"] == "Pendant"


def test_type_falls_back_to_placeholder_and_warns_when_not_inferable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "EBAY_TYPE_KEYWORDS", {"pendant": "Pendant"})
    row = _row(tmp_path, monkeypatch, {"item_specifics": {}, "ebay_title": "Mystery Object"})
    assert row["C:Type"] == "Does Not Apply"
    assert "couldn't infer a C:Type" in capsys.readouterr().out


def test_type_preserves_real_value_when_present(tmp_path, monkeypatch):
    row = _row(tmp_path, monkeypatch, {"item_specifics": {"Type": "Chandelier"}, "ebay_title": "A Pendant"})
    assert row["C:Type"] == "Chandelier"


def test_condition_id_kept_when_broadly_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EBAY_BROADLY_ACCEPTED_CONDITION_IDS", {"1000", "1500", "3000"})
    row = _row(tmp_path, monkeypatch, {"condition_id": "3000"})
    assert row["*ConditionID"] == "3000"


def test_condition_id_falls_back_when_not_broadly_accepted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "EBAY_BROADLY_ACCEPTED_CONDITION_IDS", {"1000", "1500", "3000"})
    monkeypatch.setattr(config, "EBAY_DEFAULT_CONDITION_ID", "1500")
    row = _row(tmp_path, monkeypatch, {"condition_id": "1750"})
    assert row["*ConditionID"] == "1500"
    assert "1750" in capsys.readouterr().out


def test_row_formats_dynamic_specifics_as_c_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    listing = _fake_listing(item_specifics={"Brand": "DeWalt", "Voltage": "20V", "Type": "Drill"})
    ebay_csv.append_item_to_batch(_fake_item(), listing)

    fieldnames, rows = ebay_csv._read_existing_rows()
    assert "C:Voltage" in fieldnames
    assert rows[0]["C:Voltage"] == "20V"


def test_row_joins_all_photo_urls_with_pipe(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    urls = ["https://example.com/1.jpg", "https://example.com/2.jpg", "https://example.com/3.jpg"]
    item = _fake_item(photo_public_urls=json.dumps(urls))
    ebay_csv.append_item_to_batch(item, _fake_listing())

    _, rows = ebay_csv._read_existing_rows()
    assert rows[0]["PicURL"] == "|".join(urls)


def test_row_caps_photo_urls_at_ebays_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    urls = [f"https://example.com/{i}.jpg" for i in range(30)]
    item = _fake_item(photo_public_urls=json.dumps(urls))
    ebay_csv.append_item_to_batch(item, _fake_listing())

    _, rows = ebay_csv._read_existing_rows()
    assert rows[0]["PicURL"].count("|") == ebay_csv.MAX_PHOTO_URLS - 1


def test_row_leaves_photo_url_blank_when_none_available(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    ebay_csv.append_item_to_batch(_fake_item(photo_public_urls=json.dumps([])), _fake_listing())

    _, rows = ebay_csv._read_existing_rows()
    assert rows[0]["PicURL"] == ""


def test_readding_same_item_replaces_its_row_not_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(ebay_csv, "BATCH_CSV_PATH", tmp_path / "batch.csv")
    item = _fake_item()
    ebay_csv.append_item_to_batch(item, _fake_listing(ebay_title="First Title"))
    ebay_csv.append_item_to_batch(item, _fake_listing(ebay_title="Corrected Title"))

    _, rows = ebay_csv._read_existing_rows()
    assert len(rows) == 1
    assert rows[0]["*Title"] == "Corrected Title"


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
