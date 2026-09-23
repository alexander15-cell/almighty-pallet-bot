"""
CSV batch for Facebook Marketplace's bulk-listing upload tool - this is
what "Add to FB Marketplace Batch" (cogs/item_flow.py's AwaitingListingView)
writes to, and what /fb-marketplace export-batch (cogs/fb_marketplace.py)
hands over. Mirrors ebay_csv.py's accumulate-then-export shape, but for a
much simpler file - Facebook's bulk template has no multi-step "eBay
processes it and hands it back" round trip, no fixed metadata rows, and no
per-item SKU column to key off, so rows are just appended in item order and
de-duplication isn't a concern the way it is for eBay's batch (an item can
only be added to this batch once, since doing so immediately moves it out
of Awaiting Listing).

Columns, per Facebook's own "Use a spreadsheet for multiple listings on
Facebook Marketplace" help article: Title (string), Price (number - rounded
to the nearest whole number by Facebook on their end, not here), and
Description (string) are the three columns Facebook's docs list as what
"your spreadsheet should include." Title/Price reuse the SAME data already
captured for every approved item during Queue Review's eBay listing form
(EbayListingModal - see database.save_ebay_listing_data's docstring: "every
approved item gets this captured here regardless of which platform it
eventually sells on"), so there's nothing extra to fill in at Queue Review
for a second platform. Description falls back through the AI-generated
description to the raw Data Entry note, same as pirate_ship_csv.py's Item
Description column.

Photo URL is also included here (using the same R2-hosted public photo
links the eBay batch uses - config.R2_ENABLED) even though Facebook's own
required-column list above doesn't mention it, since a listing with no
photo isn't realistically sellable and we already have the URLs on hand.
Facebook's real downloaded template may use a different header for this -
if Facebook's bulk uploader rejects or ignores this column, rename it here
to match Facebook's actual template exactly (do NOT remove/reorder Title/
Price/Description - Facebook's docs are explicit that changing those
headers breaks the upload).

Facebook auto-predicts each listing's category from Title/Description per
its own docs, so there's no Category column to fill in here at all.
"""
import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config

BATCH_CSV_PATH = Path(config.FB_MARKETPLACE_BATCH_CSV_PATH)
ARCHIVE_DIR = Path(config.FB_MARKETPLACE_BATCH_ARCHIVE_DIR)

FIELDS = ["Title", "Price", "Description", "Photo URL"]

# Only the first photo is included - unlike eBay's bulk template, Facebook's
# exact convention for multiple photos per row (additional columns? a
# delimiter?) isn't confirmed here, so this deliberately doesn't guess at
# one. One photo is enough for Facebook to accept the listing; add the rest
# by hand afterward, or extend this once Facebook's real multi-photo format
# is confirmed against an actual downloaded template.
MAX_PHOTO_URLS = 1


def _read_existing_rows() -> list:
    """Returns the batch file's current data rows as FIELDS-keyed dicts -
    [] if no file exists yet."""
    if not BATCH_CSV_PATH.exists():
        return []
    with BATCH_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def append_item_to_batch(item: dict, listing: dict) -> None:
    """
    Appends one row for `item` (an items table dict) using `listing` (an
    ebay_listing_data dict from database.get_ebay_listing_data - the same
    title/price captured for every approved item, not eBay-specific despite
    the table name) to the batch CSV.
    """
    rows = _read_existing_rows()

    public_urls = [u for u in json.loads(item.get("photo_public_urls") or "[]") if u]
    photo_url = public_urls[0] if public_urls[:MAX_PHOTO_URLS] else ""

    price = listing.get("price")
    description = item.get("ai_description") or item.get("raw_description") or ""

    row = {
        "Title": listing.get("ebay_title") or "",
        "Price": f"{round(price)}" if price is not None else "",
        "Description": description,
        "Photo URL": photo_url,
    }
    rows.append(row)

    BATCH_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def export_and_archive() -> Path | None:
    """
    Returns a Path to a copy of the current batch CSV (for attaching to
    Discord), then archives it under ARCHIVE_DIR with a timestamped name and
    clears the live file so the next "Add to FB Marketplace Batch" click
    starts a fresh batch. Returns None if the batch is currently empty.
    """
    if not BATCH_CSV_PATH.exists() or BATCH_CSV_PATH.stat().st_size == 0:
        return None

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archived_path = ARCHIVE_DIR / f"fb_marketplace_batch_{timestamp}.csv"
    shutil.copyfile(BATCH_CSV_PATH, archived_path)
    BATCH_CSV_PATH.unlink()
    return archived_path
