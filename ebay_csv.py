"""
CSV fallback for getting items onto eBay without a live API integration -
this is what "Add to eBay Batch" (cogs/item_flow.py's AwaitingListingView)
writes to, and what /ebay export-batch (cogs/ebay.py) hands over.

Columns follow eBay's classic File Exchange "Add" template
(Action/CustomLabel/Category/Title/ConditionID/.../Quantity), for either a
fixed-price listing (*Format=FixedPrice, *Duration=GTC) or an auction
(*Format=Auction, *Duration=Days_3/5/7/10) - decided per item during Queue
Review approval, see EbayFormatSelectView in item_flow.py. *StartPrice is
reused for both, holding either the fixed price or the auction starting
bid. Item specifics are appended as dynamic "C:<Name>" columns - eBay's own
File Exchange convention for per-listing item specifics, since which
attributes apply (brand, size, color, ...) varies by category and can't be
fixed columns.

PicURL uses the item's first Cloudflare R2 public photo URL (see
r2_storage.py, config.R2_ENABLED) if one was uploaded at Data Entry time.
If R2 isn't configured, PicURL is left blank - photos are only saved to
local disk in that case, not to a public URL eBay's bulk upload can fetch,
so whoever processes the batch still needs to attach photos in Seller Hub
(or fill PicURL in by hand) before uploading.

*Location is your seller account's real ship-from location
(config.EBAY_ITEM_LOCATION) - unlike PicURL, eBay rejects EVERY row
without it ("No <Item.Location> exists"), so cogs/ebay.py's export-batch
command refuses to export at all until this is configured, rather than
producing a batch that's guaranteed to fail on every row.

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

BATCH_CSV_PATH = Path(config.EBAY_BATCH_CSV_PATH)
ARCHIVE_DIR = Path(config.EBAY_BATCH_ARCHIVE_DIR)

# Fixed columns every row has, in eBay File Exchange's expected order. The
# "Add" action line + these fields are enough for a basic fixed-price
# listing; anyone uploading the batch can extend it with more of eBay's
# optional columns (shipping profile, store category, etc.) before uploading.
BASE_FIELDS = [
    "Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8)",
    "CustomLabel",
    "*Category",
    "*Title",
    "*ConditionID",
    "PicURL",
    "*Description",
    "*Format",
    "*Duration",
    "*StartPrice",
    "*Quantity",
    "*Location",
]


def custom_label(item: dict) -> str:
    """
    A stable, human-traceable SKU for this item - used as the key for
    "is this item already a row in the current batch" (so re-adding an item,
    e.g. after Queue Review data was corrected, replaces its row instead of
    duplicating it), and by cogs/ebay.py's import-results reconciliation to
    match a results CSV's "Custom Label (SKU)" column back to an item.
    """
    return f"pallet-{item['pallet_id']}-item-{item['item_number']}"


def _read_existing_rows() -> tuple[list, list]:
    if not BATCH_CSV_PATH.exists():
        return list(BASE_FIELDS), []
    with BATCH_CSV_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or BASE_FIELDS)
        rows = list(reader)
    # A batch file already sitting on disk from before BASE_FIELDS grew a new
    # required column (e.g. *Location, added after *Location's absence broke
    # a real upload) would otherwise keep missing it until the batch is
    # exported and a fresh file started - self-heal by adding any BASE_FIELDS
    # not already in this file's header, same as new C:<Specific> columns
    # already get added below.
    for field in BASE_FIELDS:
        if field not in fieldnames:
            fieldnames.append(field)
    return fieldnames, rows


def append_item_to_batch(item: dict, listing: dict) -> None:
    """
    Appends one row for `item` (an items table dict) using `listing` (an
    ebay_listing_data dict from database.get_ebay_listing_data) to the batch
    CSV. Grows the header with new C:<Specific> columns as needed so every
    row written so far still lines up under the same columns.
    """
    fieldnames, rows = _read_existing_rows()

    specifics = listing.get("item_specifics") or {}
    for key in specifics:
        field = f"C:{key}"
        if field not in fieldnames:
            fieldnames.append(field)

    public_urls = json.loads(item.get("photo_public_urls") or "[]")
    pic_url = next((u for u in public_urls if u), "")

    # Auction listings need an explicit *Duration (Days_3/5/7/10); fixed-price
    # ones always use "GTC" (Good 'Til Cancelled). *StartPrice is reused for
    # both - the fixed price, or the auction starting bid - matching eBay's
    # own File Exchange convention.
    listing_format = listing.get("listing_format") or "FixedPrice"
    duration = listing["auction_duration"] if listing_format == "Auction" else "GTC"

    label = custom_label(item)
    row = {field: "" for field in fieldnames}
    row.update({
        "Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8)": "Add",
        "CustomLabel": label,
        "*Category": listing["category_id"],
        "*Title": listing["ebay_title"],
        "*ConditionID": listing["condition_id"],
        "PicURL": pic_url,
        "*Description": item.get("ai_description") or item.get("raw_description") or "",
        "*Format": listing_format,
        "*Duration": duration,
        "*StartPrice": f"{listing['price']:.2f}",
        "*Quantity": "1",
        "*Location": config.EBAY_ITEM_LOCATION,
    })
    for key, value in specifics.items():
        row[f"C:{key}"] = value

    rows = [r for r in rows if r.get("CustomLabel") != label]
    rows.append(row)

    BATCH_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BATCH_CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({field: r.get(field, "") for field in fieldnames})


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
