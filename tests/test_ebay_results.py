"""ebay_results.py's flexible header-matching against several differently-
shaped eBay-style results CSVs - it must never guess a success when the
listing ID or SKU can't be confidently matched."""
import ebay_results


def test_varied_column_names_and_explicit_failure():
    csv_bytes = (
        "Action,Custom Label (SKU),Item number,Status\n"
        "Add,pallet-1-item-1,110012345678,Success\n"
        "Add,pallet-1-item-2,,Error: category not allowed\n"
    ).encode("utf-8")
    result = ebay_results.parse_results(csv_bytes)
    assert result["succeeded"] == [{"custom_label": "pallet-1-item-1", "ebay_item_id": "110012345678"}]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["custom_label"] == "pallet-1-item-2"
    assert "category not allowed" in result["failed"][0]["reason"]


def test_compact_header_names():
    csv_bytes = (
        "CustomLabel,ItemID,Error/Warning\n"
        "pallet-2-item-5,220011122233,\n"
        "pallet-2-item-6,,No category specified\n"
    ).encode("utf-8")
    result = ebay_results.parse_results(csv_bytes)
    assert result["succeeded"] == [{"custom_label": "pallet-2-item-5", "ebay_item_id": "220011122233"}]
    assert result["failed"][0]["custom_label"] == "pallet-2-item-6"


def test_blank_sku_rows_are_skipped_not_guessed():
    csv_bytes = "CustomLabel,ItemID\n,12345\npallet-3-item-1,999\n".encode("utf-8")
    result = ebay_results.parse_results(csv_bytes)
    assert result["unparsed_rows"] == 1
    assert len(result["succeeded"]) == 1


def test_no_listing_id_never_counts_as_success():
    csv_bytes = "CustomLabel,ItemID,Status\npallet-1-item-9,,OK\n".encode("utf-8")
    result = ebay_results.parse_results(csv_bytes)
    assert result["succeeded"] == []
    assert result["failed"][0]["reason"] == "no listing ID returned"
