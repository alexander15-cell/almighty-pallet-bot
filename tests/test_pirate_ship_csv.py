"""pirate_ship_csv.py's pure row-building (structured fields vs. the legacy
freeform-address fallback) and the buyer-data redaction used by
/pirate-ship purge-buyer-data."""
import csv

import pirate_ship_csv


def _fake_item(pallet_id=1, item_number=1, **overrides):
    item = {
        "pallet_id": pallet_id,
        "item_number": item_number,
        "recipient_name": "Jane Doe",
        "ai_title": "A widget",
        "ai_description": None,
        "raw_description": None,
        "sale_price": 25.0,
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "state": None,
        "postal_code": None,
        "country": None,
        "shipping_address": None,
    }
    item.update(overrides)
    return item


def test_order_number_format():
    item = _fake_item(pallet_id=4, item_number=2)
    assert pirate_ship_csv.order_number(item) == "pallet-4-item-2"


def test_structured_fields_used_directly():
    item = _fake_item(address_line1="123 Main St", address_line2="Apt 4", city="Springfield",
                       state="IL", postal_code="62701", country="US")
    row = pirate_ship_csv.build_export_rows([item])[0]
    assert row["Address Line 1"] == "123 Main St"
    assert row["City"] == "Springfield"
    assert row["Zip"] == "62701"
    assert row["Value (USD)"] == "25.00"


def test_legacy_freeform_address_falls_back_to_split():
    item = _fake_item(shipping_address="456 Old Rd\nOldtown, TX 75001")
    row = pirate_ship_csv.build_export_rows([item])[0]
    assert row["Address Line 1"] == "456 Old Rd"
    assert row["Address Line 2"] == "Oldtown, TX 75001"
    assert row["City"] == ""  # never guessed at for legacy rows


def test_redact_archived_buyer_data_only_touches_matching_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(pirate_ship_csv, "ARCHIVE_DIR", tmp_path)
    item_a = _fake_item(pallet_id=1, item_number=1, address_line1="1 A St", city="Aville")
    item_b = _fake_item(pallet_id=1, item_number=2, address_line1="2 B St", city="Bville")
    path = pirate_ship_csv.export_pending([item_a, item_b])

    redacted = pirate_ship_csv.redact_archived_buyer_data({pirate_ship_csv.order_number(item_a)})
    assert redacted == 1

    with path.open() as f:
        rows = {r["Order Number"]: r for r in csv.DictReader(f)}
    assert rows[pirate_ship_csv.order_number(item_a)]["Recipient Name"] == ""
    assert rows[pirate_ship_csv.order_number(item_a)]["Address Line 1"] == ""
    assert rows[pirate_ship_csv.order_number(item_b)]["Recipient Name"] == "Jane Doe"  # untouched
