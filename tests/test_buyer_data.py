"""Buyer-data retention: redacting old Pirate Ship CSV archives (buyer_data.py)."""
import csv

import config
import buyer_data

FIELDS = ["Order Number", "Recipient Name", "Address Line 1", "City", "Item Description"]


def test_redaction_only_touches_matching_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIRATE_SHIP_EXPORT_ARCHIVE_DIR", str(tmp_path))
    path = tmp_path / "pirate_ship_export_old.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow({"Order Number": "pallet-1-item-1", "Recipient Name": "Ann", "Address Line 1": "1 A St",
                         "City": "Aville", "Item Description": "Lamp"})
        writer.writerow({"Order Number": "pallet-1-item-2", "Recipient Name": "Jane Doe", "Address Line 1": "2 B St",
                         "City": "Bville", "Item Description": "Fan"})
    item_a = {"pallet_id": 1, "item_number": 1}
    assert buyer_data.order_number(item_a) == "pallet-1-item-1"
    assert buyer_data.redact_archived_buyer_data({buyer_data.order_number(item_a)}) == 1
    with path.open() as f:
        rows = {r["Order Number"]: r for r in csv.DictReader(f)}
    assert rows["pallet-1-item-1"]["Recipient Name"] == "" and rows["pallet-1-item-1"]["Address Line 1"] == ""
    assert rows["pallet-1-item-1"]["Item Description"] == "Lamp"
    assert rows["pallet-1-item-2"]["Recipient Name"] == "Jane Doe"  # untouched


def test_no_archive_folder_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PIRATE_SHIP_EXPORT_ARCHIVE_DIR", str(tmp_path / "missing"))
    assert buyer_data.redact_archived_buyer_data({"pallet-1-item-1"}) == 0
