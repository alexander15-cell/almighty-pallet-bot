"""
CSV fallback for getting items onto eBay without a live API integration -
this is what "Add to eBay Batch" (cogs/item_flow.py's AwaitingListingView)
writes to, and what /ebay export-batch (cogs/ebay.py) hands over.

Matches eBay's newer AI-powered "Prefill Listing" bulk tool (its own
official template: "eBay-taxonomy-mapping-template_US"), not the older
classic File Exchange "Add" template this used to follow. It's a 3-step
workflow on eBay's side: (1) upload this lean file (SKU/photos/title/
category/aspects - all individually optional, per eBay's own template
instructions) via Seller Hub's Reports tab; (2) eBay's own AI processes it
and returns a file with suggested categories, condition, price, shipping,
etc. for each item; (3) a human reviews/edits eBay's suggestions on that
returned file and re-uploads it to actually create the listings. So unlike
the old format, this file never needs condition/price/shipping/quantity
fields at all - eBay fills those in itself downstream. (Price/condition/
weight/dimensions are still captured during Queue Review approval - see
EbayListingModal in item_flow.py - since that's useful data regardless,
and still feeds the Pirate Ship CSV export, just not this file anymore.)

The file itself has an unusual, fixed shape eBay's own template requires
("Do not change any formatting in the file"): two "#INFO" metadata rows
(version + which optional "input sets" are being used) before the real
header row. INFO_ROWS below is written verbatim to match.

Item Photo URL supports up to 24 pipe-separated ("|") image URLs per item -
this uses every R2-hosted public photo URL available (see r2_storage.py,
config.R2_ENABLED), not just the first one, since eBay's own AI apparently
uses the FIRST link to extract listing details but can use the rest too.
If R2 isn't configured, this is left blank - photos are only saved to
local disk in that case, not to a public URL eBay's bulk upload can fetch,
so whoever processes the batch still needs to add photos in Seller Hub
(or fill this in by hand) before uploading.

Category is free text here per eBay's own instructions ("your own category
... doesn't need to be mapped to eBay's taxonomy") - since Queue Review
already resolves a real eBay leaf category via ebay_taxonomy.py, that
resolved category's full breadcrumb path is used, which can only help
eBay's own AI make a better suggestion.

Aspects holds item specifics as pipe-separated "Key=Value" pairs (eBay's
own convention for this column, e.g. "Color=Red|Size=Small") - unlike the
old format's per-attribute "C:<Name>" columns, this is always exactly one
column since eBay's own template doesn't grow dynamic columns per category.

This lives outside any single cog, same as finance_utils.py, because both
item_flow.py (writes rows) and ebay.py (exports/archives the file) need the
same logic.
"""
import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import config
import ebay_taxonomy

BATCH_CSV_PATH = Path(config.EBAY_BATCH_CSV_PATH)
ARCHIVE_DIR = Path(config.EBAY_BATCH_ARCHIVE_DIR)

# eBay's own two metadata rows, required verbatim before the real header row
# (see this module's docstring) - Set A (photo/title) and Set B (category/
# aspects) are the two input types this bot actually fills in per item.
INFO_ROWS = [
    ["#INFO", "Version=1.0.0", "", "Template=eBay-taxonomy-mapping-template_US", ""],
    ["#INFO", "Set A", "", "Set B", ""],
]

FIELDS = ["Custom Label (SKU)", "Item Photo URL", "Title", "Category", "Aspects"]

MAX_PHOTO_URLS = 24  # eBay's own per-item cap on pipe-separated Item Photo URL links


def custom_label(item: dict) -> str:
    """
    A stable, human-traceable SKU for this item - used as the key for
    "is this item already a row in the current batch" (so re-adding an item,
    e.g. after Queue Review data was corrected, replaces its row instead of
    duplicating it), and by cogs/ebay.py's import-results reconciliation to
    match a results CSV's "Custom Label (SKU)" column back to an item.
    """
    return f"pallet-{item['pallet_id']}-item-{item['item_number']}"


def _read_existing_rows() -> list:
    """Returns the batch file's current data rows as FIELDS-keyed dicts,
    skipping the two #INFO rows and the header - [] if no file exists yet."""
    if not BATCH_CSV_PATH.exists():
        return []
    with BATCH_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        all_rows = list(csv.reader(f))
    data_rows = all_rows[len(INFO_ROWS) + 1:]  # + 1 for the header row itself
    return [dict(zip(FIELDS, row)) for row in data_rows]


def append_item_to_batch(item: dict, listing: dict) -> None:
    """
    Appends one row for `item` (an items table dict) using `listing` (an
    ebay_listing_data dict from database.get_ebay_listing_data) to the batch
    CSV, replacing any existing row for the same item (see custom_label).
    """
    rows = _read_existing_rows()

    public_urls = [u for u in json.loads(item.get("photo_public_urls") or "[]") if u]
    photo_urls = "|".join(public_urls[:MAX_PHOTO_URLS])

    category_path = ebay_taxonomy.get_path(listing.get("category_id")) or ""

    specifics = listing.get("item_specifics") or {}
    aspects = "|".join(f"{key}={value}" for key, value in specifics.items())

    label = custom_label(item)
    row = {
        "Custom Label (SKU)": label,
        "Item Photo URL": photo_urls,
        "Title": listing.get("ebay_title") or "",
        "Category": category_path,
        "Aspects": aspects,
    }

    rows = [r for r in rows if r.get("Custom Label (SKU)") != label]
    rows.append(row)

    BATCH_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for info_row in INFO_ROWS:
            writer.writerow(info_row)
        writer.writerow(FIELDS)
        for r in rows:
            writer.writerow([r.get(field, "") for field in FIELDS])


def export_and_archive() -> Path | None:
    """
    Returns a Path to a copy of the current batch CSV (for attaching to
    Discord), then archives it under ARCHIVE_DIR with a timestamped name and
    clears the live file so the next "Add to eBay Batch" click starts a
    fresh batch. Returns None if the batch is currently empty.
    """
    if not BATCH_CSV_PATH.exists() or BATCH_CSV_PATH.stat().st_size == 0:
        return None

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archived_path = ARCHIVE_DIR / f"ebay_batch_{timestamp}.csv"
    shutil.copyfile(BATCH_CSV_PATH, archived_path)
    BATCH_CSV_PATH.unlink()
    return archived_path
