"""
pirate_ship_import.parse_shipping_costs - pure CSV parsing/matching, no
database (matches ebay_results.py's test style). Covers the flexible
header-scanning matching (deliberately loose since no real Pirate Ship
export sample was available - see the module docstring).
"""
import pirate_ship_import as psi


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def test_matches_row_with_standard_column_names():
    csv_bytes = _csv(
        "Order Number,Recipient,Cost\n"
        "pallet-3-item-7,Jane Doe,5.43\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert result["matched"] == [
        {"pallet_id": 3, "item_number": 7, "amount": 5.43, "row_label": "pallet-3-item-7"}
    ]
    assert result["unmatched"] == []


def test_finds_reference_in_any_column_not_just_a_named_one():
    csv_bytes = _csv(
        "Ship Date,Tracking,Customer Note,Price\n"
        "2026-09-20,1Z999,ref: pallet-12-item-4,7.10\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert len(result["matched"]) == 1
    assert result["matched"][0] == {
        "pallet_id": 12, "item_number": 4, "amount": 7.10, "row_label": "2026-09-20"
    }


def test_finds_cost_via_header_hint_regardless_of_exact_name():
    csv_bytes = _csv(
        "Reference,Label Cost (USD)\n"
        "pallet-1-item-1,3.25\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert result["matched"][0]["amount"] == 3.25


def test_handles_dollar_signs_and_commas_in_cost():
    csv_bytes = _csv(
        "Reference,Total\n"
        "pallet-1-item-1,\"$1,234.56\"\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert result["matched"][0]["amount"] == 1234.56


def test_row_with_no_reference_is_unmatched():
    csv_bytes = _csv(
        "Order Number,Cost\n"
        "SOME-OTHER-ORDER-123,5.00\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert result["matched"] == []
    assert result["unmatched"] == [{"row_label": "SOME-OTHER-ORDER-123", "amount": 5.00}]


def test_row_with_no_parseable_cost_is_unmatched():
    csv_bytes = _csv(
        "Order Number,Cost\n"
        "pallet-2-item-9,N/A\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert result["matched"] == []
    assert result["unmatched"] == [{"row_label": "pallet-2-item-9", "amount": None}]


def test_blank_rows_are_skipped_entirely():
    csv_bytes = _csv(
        "Order Number,Cost\n"
        ",\n"
        "pallet-1-item-1,4.00\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert len(result["matched"]) == 1


def test_multiple_rows_matched_and_unmatched_together():
    csv_bytes = _csv(
        "Order Number,Cost\n"
        "pallet-1-item-1,4.00\n"
        "not-a-reference,9.00\n"
        "pallet-2-item-2,6.50\n"
    )
    result = psi.parse_shipping_costs(csv_bytes)
    assert len(result["matched"]) == 2
    assert len(result["unmatched"]) == 1
    assert result["unmatched"][0]["row_label"] == "not-a-reference"
