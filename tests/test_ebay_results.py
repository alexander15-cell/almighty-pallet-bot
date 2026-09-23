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


def test_real_ebay_classic_format_prefers_errormessage_over_bare_status():
    # This is eBay's actual "classic File Exchange" results format,
    # confirmed against a real failed upload - it has BOTH a bare "Status"
    # column (just says "Failure") and a much more useful "ErrorMessage"
    # column with the real reason. Confirming the parser surfaces the
    # useful one, not "Failure".
    header = (
        "Line Number,Action,Status,ErrorCode,ErrorMessage,WarningCode,WarningMessage,Code,Message,"
        "ItemID,ReferenceID,ApplicationData,StartTime,EndTime,AuctionLengthFee,BoldFee,BorderFee,"
        "BuyItNowFee,CategoryFeaturedFee,CurrencyID,FeaturedFee,FeaturedGalleryFee,FixedPriceDurationFee,"
        "GalleryFee,GiftIconFee,HighlightFee,InsertionFee,InternationalInsertionFee,ListingDesignerFee,"
        "ListingFee,PhotoDisplayFee,PhotoFee,ProPackBundleFee,ReserveFee,SchedulingFee,SubtitleFee,"
        "CustomLabel,PrivateNotes,BasicUpgradePackBundleFee,ValuePackBundleFee,ProPackPlusBundleFee,"
        "SellerInventoryID,CrossBorderTradeNorthAmericaFee,CrossBorderTradeGBFee,RefundFromSeller,"
        "TotalRefundToBuyer,CorrelationID\n"
    )
    row = (
        "2,Add,Failure,10009,"
        '"Error - No <Item.Location> exists or <Item.Location> is specified as an empty tag in the request.|Item.Location|"'
        ",,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,pallet-1-item-1,,,,,,,,,,\n"
    )
    result = ebay_results.parse_results((header + row).encode("utf-8"))

    assert result["columns_found"]["status"] == "ErrorMessage"
    assert len(result["failed"]) == 1
    assert "Item.Location" in result["failed"][0]["reason"]
    assert result["failed"][0]["reason"] != "Failure"
