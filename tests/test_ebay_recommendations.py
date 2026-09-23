"""
ebay_recommendations.py - filling in eBay's returned recommendations file
with data already captured during Queue Review approval. Builds a small
synthetic workbook matching the real shape observed in an actual eBay
export (two #INFO rows, a header row, then data rows in a "Cat-*" sheet) -
see this module's own docstring for the full context - rather than
depending on an external file.
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import openpyxl
import pytest

import config
import ebay_recommendations


HEADER = [
    "*Action(SiteID=US|Country=US|Currency=USD|Version=1193)", "Custom Label (SKU)", "Category ID",
    "TrackingId (Do Not Change)", "Category Name", "Title", "Original Row Number", "Schedule Time",
    "P:UPC", "P:EPID", "Start price", "Quantity", "Item photo URL", "VideoID", "Condition ID",
    "Description", "Format", "Duration", "Buy It Now price", "Best Offer Enabled",
    "Best Offer Auto Accept Price", "Minimum Best Offer Price", "Immediate pay required", "Location",
    "Shipping service 1 option",
]


def _make_workbook(rows: list, sheet_name: str = "Cat-TestCategory...") -> bytes:
    import io
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(["#INFO", "Created="])
    ws.append(["#INFO", "Version=1.0"])
    ws.append(["#INFO"])
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _row_for(sku: str, **overrides) -> list:
    row = [""] * len(HEADER)
    row[1] = sku  # Custom Label (SKU)
    row[4] = "Some Category"
    row[5] = "Some Title"
    for col_name, value in overrides.items():
        row[HEADER.index(col_name)] = value
    return row


@pytest.fixture
def item_with_listing(fresh_db):
    pallet_id = fresh_db.create_pallet("Rec Test Pallet", category_id=1, created_by=1)
    fresh_db.create_item(pallet_id, "filler item", [], 1)  # so item_number=2 below isn't #1
    item_id = fresh_db.create_item(pallet_id, "a widget", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, "A Widget", "12345", "1000", 49.99, {}, listing_format="FixedPrice", actor_id=1,
    )
    return pallet_id, item_id


def test_fills_price_quantity_condition_format_for_a_matched_item(item_with_listing):
    pallet_id, item_id = item_with_listing
    raw = _make_workbook([_row_for(f"pallet-{pallet_id}-item-2")])

    path, summary = ebay_recommendations.fill_recommendations_file(raw)
    try:
        assert summary.filled_count == 1
        assert summary.sheet_count == 1
        assert summary.unmatched_skus == []

        wb = openpyxl.load_workbook(path)
        row = list(wb["Cat-TestCategory..."].iter_rows(min_row=5, max_row=5, values_only=True))[0]
        assert row[HEADER.index("Start price")] == "49.99"
        assert row[HEADER.index("Quantity")] == "1"
        assert row[HEADER.index("Condition ID")] == "1000"
        assert row[HEADER.index("Format")] == "FixedPrice"
        assert row[HEADER.index("Duration")] in (None, "")  # FixedPrice items never get a Duration
    finally:
        path.unlink(missing_ok=True)


def test_fills_duration_for_an_auction_item(fresh_db):
    pallet_id = fresh_db.create_pallet("Auction Pallet", category_id=2, created_by=1)
    fresh_db.create_item(pallet_id, "filler", [], 1)
    item_id = fresh_db.create_item(pallet_id, "an auction item", [], 1)
    fresh_db.save_ebay_listing_data(
        item_id, "Auction Item", "12345", "1000", 10.0, {}, listing_format="Auction",
        auction_duration="Days_7", actor_id=1,
    )
    raw = _make_workbook([_row_for(f"pallet-{pallet_id}-item-2")])

    path, summary = ebay_recommendations.fill_recommendations_file(raw)
    try:
        wb = openpyxl.load_workbook(path)
        row = list(wb["Cat-TestCategory..."].iter_rows(min_row=5, max_row=5, values_only=True))[0]
        assert row[HEADER.index("Format")] == "Auction"
        assert row[HEADER.index("Duration")] == "Days_7"
    finally:
        path.unlink(missing_ok=True)


def test_never_overwrites_a_cell_that_already_has_a_value(item_with_listing):
    pallet_id, item_id = item_with_listing
    raw = _make_workbook([_row_for(f"pallet-{pallet_id}-item-2", **{"Start price": "999.00"})])

    path, summary = ebay_recommendations.fill_recommendations_file(raw)
    try:
        wb = openpyxl.load_workbook(path)
        row = list(wb["Cat-TestCategory..."].iter_rows(min_row=5, max_row=5, values_only=True))[0]
        assert row[HEADER.index("Start price")] == "999.00"  # untouched
        assert row[HEADER.index("Quantity")] == "1"  # still filled, since it was blank
    finally:
        path.unlink(missing_ok=True)


def test_reports_unmatched_sku(fresh_db):
    raw = _make_workbook([_row_for("pallet-9999-item-9999")])
    path, summary = ebay_recommendations.fill_recommendations_file(raw)
    try:
        assert summary.filled_count == 0
        assert summary.unmatched_skus == ["pallet-9999-item-9999"]
    finally:
        path.unlink(missing_ok=True)


def test_ignores_sheets_not_named_cat_prefixed(item_with_listing):
    pallet_id, item_id = item_with_listing
    raw = _make_workbook([_row_for(f"pallet-{pallet_id}-item-2")], sheet_name="Listings")
    path, summary = ebay_recommendations.fill_recommendations_file(raw)
    try:
        assert summary.filled_count == 0
        assert summary.sheet_count == 0
    finally:
        path.unlink(missing_ok=True)


def test_location_and_shipping_service_only_filled_when_configured(item_with_listing, monkeypatch):
    pallet_id, item_id = item_with_listing
    raw = _make_workbook([_row_for(f"pallet-{pallet_id}-item-2")])

    monkeypatch.setattr(config, "EBAY_ITEM_LOCATION", "")
    monkeypatch.setattr(config, "EBAY_SHIPPING_SERVICE", "")
    path, summary = ebay_recommendations.fill_recommendations_file(raw)
    try:
        assert summary.touched_location is False
        assert summary.touched_shipping_service is False
        wb = openpyxl.load_workbook(path)
        row = list(wb["Cat-TestCategory..."].iter_rows(min_row=5, max_row=5, values_only=True))[0]
        assert row[HEADER.index("Location")] in (None, "")
    finally:
        path.unlink(missing_ok=True)

    monkeypatch.setattr(config, "EBAY_ITEM_LOCATION", "Columbus, OH")
    monkeypatch.setattr(config, "EBAY_SHIPPING_SERVICE", "USPSPriority")
    path2, summary2 = ebay_recommendations.fill_recommendations_file(raw)
    try:
        assert summary2.touched_location is True
        assert summary2.touched_shipping_service is True
        wb2 = openpyxl.load_workbook(path2)
        row2 = list(wb2["Cat-TestCategory..."].iter_rows(min_row=5, max_row=5, values_only=True))[0]
        assert row2[HEADER.index("Location")] == "Columbus, OH"
        assert row2[HEADER.index("Shipping service 1 option")] == "USPSPriority"
    finally:
        path2.unlink(missing_ok=True)


def test_sanitizes_out_of_range_font_family_before_loading():
    # A real eBay-generated file has been observed with <family val="34"/>
    # in its style XML - outside OOXML's valid 0-14 range, which makes a
    # strict reader refuse to open the file at all ("Max value is 14").
    import io
    import zipfile

    raw = _make_workbook([])
    zin = zipfile.ZipFile(io.BytesIO(raw))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/styles.xml":
                # openpyxl itself writes <family val="2" /> (with a space) -
                # bump it out of the valid 0-14 range to simulate the bug.
                text = data.decode("utf-8").replace('<family val="2" />', '<family val="34" />', 1)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    corrupted = buf.getvalue()

    # Sanity check: openpyxl actually refuses the corrupted file directly...
    with pytest.raises(Exception):
        openpyxl.load_workbook(io.BytesIO(corrupted))

    # ...but fill_recommendations_file works around it.
    path, summary = ebay_recommendations.fill_recommendations_file(corrupted)
    path.unlink(missing_ok=True)


def test_raises_recommendations_file_error_for_garbage_input():
    with pytest.raises(ebay_recommendations.RecommendationsFileError):
        ebay_recommendations.fill_recommendations_file(b"not a real xlsx file")
