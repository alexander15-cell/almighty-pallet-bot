"""
CSV export for shipping "Other"-platform sales (FB Marketplace, website,
etc.) through Pirate Ship's batch/spreadsheet order import.

eBay sales deliberately never appear here - Pirate Ship pulls those
directly via its own native eBay integration, so exporting them again here
would just be a duplicate. This only covers items sold somewhere Pirate
Ship can't see on its own.

Unlike ebay_csv.py, there's no separate "add to batch" step: /pirate-ship
export-batch (cogs/pirate_ship.py) queries the database directly, each time
it's run, for every STATUS_SOLD item with a non-eBay sale_platform that
hasn't been exported yet (see database.get_unexported_other_platform_sales),
so nothing needs accumulating on disk between exports.

Recipient name/address are freeform text captured via /finance
set-shipping-info - decoupled from the sale-price recording the same way
Mark as Sold is decoupled from price recording, since a buyer's address
often isn't known until after the price is agreed on. Weight/dimensions
aren't tracked anywhere in this bot yet, so those columns are left blank
for whoever processes the batch to fill in by hand before uploading -
same "leave it blank, fill in by hand" precedent as ebay_csv.py's PicURL.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path

import config

ARCHIVE_DIR = Path(config.PIRATE_SHIP_EXPORT_ARCHIVE_DIR)

# A reasonably standard set of columns for Pirate Ship's CSV/spreadsheet
# batch order import - it lets you map your own header names at upload
# time, so exact naming isn't rigid, but these line up with the fields it
# asks for (recipient, address, description, declared value, weight/size).
FIELDS = [
    "Order Number",
    "Recipient Name",
    "Address Line 1",
    "Address Line 2",
    "City",
    "State",
    "Zip",
    "Country",
    "Item Description",
    "Value (USD)",
    "Weight (lb)",
    "Weight (oz)",
    "Length (in)",
    "Width (in)",
    "Height (in)",
]


def _split_address(shipping_address: str) -> dict:
    """
    shipping_address is captured as one freeform multi-line block (see
    /finance set-shipping-info) - not broken into structured street/city/
    state/zip fields, since this bot doesn't validate addresses. Splits it
    on newlines into Address Line 1/2 best-effort; whoever processes the
    batch should double-check City/State/Zip/Country before uploading,
    since those are left blank here rather than guessed at from free text.
    """
    lines = [line.strip() for line in (shipping_address or "").splitlines() if line.strip()]
    return {
        "Address Line 1": lines[0] if lines else "",
        "Address Line 2": " / ".join(lines[1:]) if len(lines) > 1 else "",
    }


def build_export_rows(items: list) -> list:
    """Builds one CSV row per item - pure function so it's easy to test
    without touching the filesystem."""
    rows = []
    for item in items:
        row = {field: "" for field in FIELDS}
        row.update({
            "Order Number": f"pallet-{item['pallet_id']}-item-{item['item_number']}",
            "Recipient Name": item.get("recipient_name") or "",
            "Country": "US",
            "Item Description": item.get("ai_title") or item.get("ai_description") or item.get("raw_description") or "",
            "Value (USD)": f"{item['sale_price']:.2f}" if item.get("sale_price") is not None else "",
        })
        row.update(_split_address(item.get("shipping_address")))
        rows.append(row)
    return rows


def export_pending(items: list) -> Path:
    """
    Writes `items` (from database.get_unexported_other_platform_sales) to a
    fresh, timestamped CSV under ARCHIVE_DIR and returns its path. Doesn't
    touch the database - the caller (cogs/pirate_ship.py) marks the items
    exported only after successfully attaching this file to a Discord
    message, so a failed send doesn't silently lose an item from the batch.
    """
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = ARCHIVE_DIR / f"pirate_ship_export_{timestamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(build_export_rows(items))
    return path
