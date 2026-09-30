"""
manifest_import.parse_manifest - pure CSV/XLSX parsing, no database (matches
pirate_ship_import.py's test style). Covers the flexible header-scanning
matching (different liquidators use different column names/formats - see
the module docstring) and the real quirks found in an actual liquidator
manifest: a repeated SKU across separate quantity-1 rows, a bare-number
"Product" field, and a totals/footer row that must be excluded.
"""
import io

import openpyxl
import pytest

import manifest_import as mi


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def _xlsx(rows: list) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parses_standard_columns():
    csv_bytes = _csv(
        'Product,Quantity,"Retail Price","Total Retail Price",SKU\n'
        "Widget,2,$10.00,$20.00,A1\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 2
    assert result["total_retail_value"] == 20.00
    assert result["lines"] == [
        {"sku": "A1", "product": "Widget", "retail_price": 10.00},
        {"sku": "A1", "product": "Widget", "retail_price": 10.00},
    ]
    assert result["columns_used"]["sku"] == "SKU"
    assert result["columns_used"]["retail_price"] == "Retail Price"


def test_skips_totals_footer_row_with_blank_product_and_sku():
    csv_bytes = _csv(
        'Product,Quantity,"Retail Price","Total Retail Price",SKU\n'
        "Widget,1,$10.00,$10.00,A1\n"
        ' ,2," ","$10.00", \n'
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 1
    assert result["total_retail_value"] == 10.00


def test_declared_total_mismatch_produces_a_warning_not_a_rejection():
    csv_bytes = _csv(
        'Product,"Retail Price","Total Retail Price",SKU\n'
        "Widget,$10.00,$999.00,A1\n"
        " , ,$999.00, \n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_retail_value"] == 10.00
    assert any("doesn't match" in w for w in result["warnings"])


def test_same_sku_as_separate_rows_stays_two_lines_not_merged():
    """Real liquidator quirk: the same SKU can appear as multiple distinct
    qty-1 rows instead of one row with a summed quantity - both must
    survive as independent manifest lines."""
    csv_bytes = _csv(
        "Product,SKU,Retail Price\n"
        "Bulb,1011799948,$5.00\n"
        "Bulb,1011799948,$5.00\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 2
    assert result["lines"][0]["sku"] == result["lines"][1]["sku"] == "1011799948"


def test_bare_number_product_with_real_sku_is_not_treated_as_a_footer_row():
    csv_bytes = _csv(
        "Product,SKU,Retail Price\n"
        "12191,1009204832,$8.00\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 1
    assert result["lines"][0]["product"] == "12191"


def test_different_header_names_still_detected_by_keyword():
    """A different liquidator's header naming ("Unit Price"/"Item Name"
    instead of "Retail Price"/"Product") must still be picked up - this
    module never assumes one fixed schema."""
    csv_bytes = _csv(
        "Item Name,SKU #,Unit Price,Qty\n"
        "Drill,B2,$45.00,3\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 3
    assert result["lines"][0]["product"] == "Drill"
    assert result["lines"][0]["retail_price"] == 45.00


def test_missing_quantity_column_defaults_to_one_each():
    csv_bytes = _csv(
        "Product,SKU,Retail Price\n"
        "Widget,A1,$10.00\n"
        "Gadget,B2,$20.00\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 2


def test_row_with_unreadable_price_is_skipped_with_a_warning():
    csv_bytes = _csv(
        "Product,SKU,Retail Price\n"
        "Widget,A1,$10.00\n"
        "Mystery,B2,n/a\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 1
    assert any("Row 3" in w for w in result["warnings"])


def test_no_retail_price_column_at_all_is_rejected_with_a_clear_reason():
    csv_bytes = _csv(
        "Product,SKU,Notes\n"
        "Widget,A1,fragile\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is False
    assert "price" in result["error"].lower()


def test_no_sku_column_at_all_is_rejected_with_a_clear_reason():
    csv_bytes = _csv(
        "Product,Retail Price\n"
        "Widget,$10.00\n"
    )
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is False
    assert "sku" in result["error"].lower()


def test_empty_file_is_rejected():
    result = mi.parse_manifest(b"", "manifest.csv")
    assert result["ok"] is False


def test_every_row_blank_or_priceless_is_rejected():
    csv_bytes = _csv("Product,SKU,Retail Price\n ,, \n")
    result = mi.parse_manifest(csv_bytes, "manifest.csv")
    assert result["ok"] is False


def test_unrecognized_extension_is_rejected():
    result = mi.parse_manifest(b"whatever", "manifest.pdf")
    assert result["ok"] is False
    assert "pdf" in result["error"].lower() or ".csv" in result["error"]


def test_xlsx_is_parsed_the_same_as_csv():
    xlsx_bytes = _xlsx([
        ["Product", "Quantity", "Retail Price", "SKU"],
        ["Widget", 2, 10.0, "A1"],
    ])
    result = mi.parse_manifest(xlsx_bytes, "manifest.xlsx")
    assert result["ok"] is True
    assert result["total_units"] == 2
    assert result["total_retail_value"] == 20.0
    assert result["lines"][0]["sku"] == "A1"


def test_corrupted_xlsx_is_rejected_not_raised():
    result = mi.parse_manifest(b"not a real xlsx file", "manifest.xlsx")
    assert result["ok"] is False


def test_real_manifest_sample_matches_known_totals():
    """Regression guard against the real 6-pallet liquidation manifest this
    feature was built from: 310 item rows exploding to 359 physical units
    totaling $30,876.77 retail, per its own footer row."""
    with open("tests/fixtures/sample_manifest.csv", "rb") as f:
        data = f.read()
    result = mi.parse_manifest(data, "sample_manifest.csv")
    assert result["ok"] is True
    assert result["total_units"] == 359
    assert result["total_retail_value"] == 30876.77
    assert result["warnings"] == []
