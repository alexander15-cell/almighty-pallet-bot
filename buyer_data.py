"""
Buyer-data retention (/admin purge-buyer-data).

Shipped items keep the buyer's name/address only as long as needed. The
purge clears them from the database and also redacts the same buyers in
the old Pirate Ship CSV exports still archived under
PIRATE_SHIP_EXPORT_ARCHIVE_DIR (the bot no longer makes those - labels go
through Shippo - but the files from before are kept, and backed up, so
their buyer data still has to be reachable by the purge).
"""
import csv
from pathlib import Path

import config

BUYER_FIELDS_TO_REDACT = ("Recipient Name", "Address Line 1", "Address Line 2", "City", "State", "Zip", "Country")


def order_number(item: dict) -> str:
    """The per-item key the old exports used as their Order Number."""
    return f"pallet-{item['pallet_id']}-item-{item['item_number']}"


def redact_archived_buyer_data(order_numbers) -> int:
    """Blanks the buyer columns of matching rows in every archived export
    CSV. Returns how many rows were redacted; untouched files stay as-is."""
    order_numbers = set(order_numbers)
    archive_dir = Path(config.PIRATE_SHIP_EXPORT_ARCHIVE_DIR)
    if not order_numbers or not archive_dir.exists():
        return 0
    redacted_count = 0
    for csv_path in archive_dir.glob("*.csv"):
        with csv_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            rows = list(reader)
        if not fieldnames:
            continue
        changed = False
        for row in rows:
            if row.get("Order Number") in order_numbers:
                for field in BUYER_FIELDS_TO_REDACT:
                    if row.get(field):
                        row[field] = ""
                changed = True
                redacted_count += 1
        if changed:
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
    return redacted_count
