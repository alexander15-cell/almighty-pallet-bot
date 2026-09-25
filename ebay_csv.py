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
bid. Unlike eBay's separate AI-prefill "Prefill Listing" tool (still
available by hand - see /ebay fill-recommendations), this file is meant to
be ready to upload as-is: every column eBay actually requires is filled
from data Queue Review already captured, not left for a second suggest-
then-review round trip.

Shipping/return/payment use eBay's named Business Policies
(config.EBAY_SHIPPING_PROFILE_NAME/EBAY_RETURN_PROFILE_NAME/
EBAY_PAYMENT_PROFILE_NAME - Seller Hub's real saved policy names), not the
older manual ShippingType/ShippingService/ShippingPackage columns - eBay
matches a policy by exact name, so if any of these are ever renamed in
Seller Hub, update the matching config value to match. PostalCode
(config.EBAY_SHIP_FROM_POSTAL_CODE) is required once a Calculated-shipping
policy is attached (error 216007 otherwise), separately from *Location.

WeightMajor/WeightMinor come from the item's own real weight_lb
(ebay_listing_data.weight_lb - optional on EbayListingModal, item_flow.py)
when it has one; only falls back to a category-keyword estimate
(config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB) when it doesn't, since
Calculated shipping rejects a row with no weight at all (error 216121).
PackageLength/Width/Depth are real captured dimensions, left blank (not
estimated) when missing - shipping cost is still correct without them,
just less precisely calculated.

C:Brand always has a value - "Does Not Apply" (eBay's own standard
placeholder for unbranded items) when Queue Review didn't capture a real
brand, since eBay requires this field non-empty and a wrong guessed brand
would be worse than an honest placeholder. C:Type is inferred from the
title via a small keyword table (config.EBAY_TYPE_KEYWORDS) when missing,
since (unlike Brand) it's a real search/browse attribute where a wrong
guess actively hurts - see _infer_type, which logs a warning and leaves it
blank rather than guessing when nothing matches.

*ConditionID is checked against config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS
before being written - falls back to config.EBAY_DEFAULT_CONDITION_ID
(instead of submitting a value some categories reject, e.g. "1750" New with
defects - error 21916883) since this bot has no live per-category
condition-validity data (that needs eBay dev API access - see ebay_api.py).

PicURL supports up to 24 pipe-separated ("|") image URLs per item (eBay
File Exchange's own PicURL convention) - this uses every R2-hosted public
photo URL available (see r2_storage.py, config.R2_ENABLED), not just the
first one. If R2 isn't configured, this is left blank - photos are only
saved to local disk in that case, not to a public URL eBay's bulk upload
can fetch, so whoever processes the batch still needs to attach photos in
Seller Hub (or fill this in by hand) before uploading.

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

# Fixed columns every row has, in eBay File Exchange's expected order. The
# "Add" action line + these fields are enough for a fixed-price or auction
# listing on an account using Business Policies for shipping/returns/
# payment; anyone uploading the batch can still extend it with more of
# eBay's optional columns before uploading.
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
    "PostalCode",
    "WeightMajor",
    "WeightMinor",
    "PackageLength",
    "PackageWidth",
    "PackageDepth",
    "ShippingProfileName",
    "ReturnProfileName",
    "PaymentProfileName",
]

MAX_PHOTO_URLS = 24  # eBay's own per-item cap on pipe-separated PicURL links


def _split_weight_lb(weight_lb) -> tuple:
    """
    eBay's classic template wants whole pounds and remaining ounces as
    separate columns (WeightMajor/WeightMinor), not one decimal-pounds
    value.
    """
    major = int(weight_lb)
    minor = round((weight_lb - major) * 16)
    if minor == 16:  # rounding 0.999... lb up to the next whole pound
        major += 1
        minor = 0
    return str(major), str(minor)


def _estimated_weight_lb(category_path: str) -> float:
    """
    Fallback used only when an item has no real captured weight_lb (see
    this module's docstring) - a rough estimate, not real per-item data.
    Matches the first config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB keyword
    found in the category path, else config.EBAY_DEFAULT_WEIGHT_FALLBACK_LB.
    """
    haystack = (category_path or "").lower()
    for keyword, weight_lb in config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB.items():
        if keyword in haystack:
            return weight_lb
    return config.EBAY_DEFAULT_WEIGHT_FALLBACK_LB


def _infer_type(title: str) -> str:
    """
    Best-effort C:Type inference from an item's title when Queue Review
    didn't capture one in item_specifics - see this module's docstring for
    why this warns and leaves the column blank instead of guessing when
    nothing in config.EBAY_TYPE_KEYWORDS matches, unlike C:Brand's safe
    "Does Not Apply" placeholder.
    """
    haystack = (title or "").lower()
    for keyword, type_value in config.EBAY_TYPE_KEYWORDS.items():
        if keyword in haystack:
            return type_value
    return ""


def _resolved_condition_id(condition_id: str, item_label: str) -> str:
    """
    Falls back to config.EBAY_DEFAULT_CONDITION_ID when `condition_id` isn't
    in config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS - see this module's
    docstring (error 21916883).
    """
    if condition_id in config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS:
        return condition_id
    print(
        f"[ebay_csv] {item_label}: condition ID {condition_id!r} isn't in the broadly-accepted "
        f"set - falling back to {config.EBAY_DEFAULT_CONDITION_ID!r} to avoid a category rejection."
    )
    return config.EBAY_DEFAULT_CONDITION_ID


def custom_label(item: dict) -> str:
    """
    A stable, human-traceable SKU for this item - used as the key for
    "is this item already a row in the current batch" (so re-adding an item,
    e.g. after Queue Review data was corrected, replaces its row instead of
    duplicating it), and by cogs/ebay.py's import-results reconciliation to
    match a results CSV's "Custom Label (SKU)"-equivalent column ("CustomLabel"
    here) back to an item.
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
    # required column would otherwise keep missing it until the batch is
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
    label = custom_label(item)

    category_id = listing.get("category_id") or ""
    title = listing.get("ebay_title") or ""

    specifics = dict(listing.get("item_specifics") or {})
    # Ensure Brand/Type always end up with a value, without duplicating a
    # column if Queue Review already typed one under a different case
    # ("brand" vs "Brand") - see this module's docstring for why each is
    # handled differently.
    brand_key = next((k for k in specifics if k.strip().lower() == "brand"), None)
    if not (brand_key and specifics[brand_key]):
        specifics[brand_key or "Brand"] = "Does Not Apply"

    type_key = next((k for k in specifics if k.strip().lower() == "type"), None)
    if not (type_key and specifics[type_key]):
        inferred_type = _infer_type(title)
        specifics[type_key or "Type"] = inferred_type
        if not inferred_type:
            print(f"[ebay_csv] {label}: couldn't infer a C:Type from the title {title!r} - leaving it blank.")

    for key in specifics:
        field = f"C:{key}"
        if field not in fieldnames:
            fieldnames.append(field)

    public_urls = [u for u in json.loads(item.get("photo_public_urls") or "[]") if u]
    pic_url = "|".join(public_urls[:MAX_PHOTO_URLS])

    # Auction listings need an explicit *Duration (Days_3/5/7/10); fixed-
    # price ones always use "GTC" (Good 'Til Cancelled) - required for
    # Format=FixedPrice or eBay rejects the row (error 10009). *StartPrice
    # is reused for both - the fixed price, or the auction starting bid.
    listing_format = listing.get("listing_format") or "FixedPrice"
    duration = listing["auction_duration"] if listing_format == "Auction" else "GTC"

    weight_lb = listing.get("weight_lb")
    if weight_lb is None:
        category_path = ebay_taxonomy.get_path(category_id) or ""
        weight_lb = _estimated_weight_lb(category_path or title)
        print(f"[ebay_csv] {label}: no captured weight - estimating {weight_lb:g} lb.")
    weight_major, weight_minor = _split_weight_lb(weight_lb)

    condition_id = _resolved_condition_id(listing.get("condition_id") or config.EBAY_DEFAULT_CONDITION_ID, label)

    row = {field: "" for field in fieldnames}
    row.update({
        "Action(SiteID=US|Country=US|Currency=USD|Version=1193|CC=UTF-8)": "Add",
        "CustomLabel": label,
        "*Category": category_id,
        "*Title": title,
        "*ConditionID": condition_id,
        "PicURL": pic_url,
        "*Description": item.get("ai_description") or item.get("raw_description") or "",
        "*Format": listing_format,
        "*Duration": duration,
        "*StartPrice": f"{listing['price']:.2f}",
        "*Quantity": "1",
        "*Location": config.EBAY_ITEM_LOCATION,
        "PostalCode": config.EBAY_SHIP_FROM_POSTAL_CODE,
        "WeightMajor": weight_major,
        "WeightMinor": weight_minor,
        "PackageLength": f"{listing['length_in']:g}" if listing.get("length_in") is not None else "",
        "PackageWidth": f"{listing['width_in']:g}" if listing.get("width_in") is not None else "",
        "PackageDepth": f"{listing['height_in']:g}" if listing.get("height_in") is not None else "",
        "ShippingProfileName": config.EBAY_SHIPPING_PROFILE_NAME,
        "ReturnProfileName": config.EBAY_RETURN_PROFILE_NAME,
        "PaymentProfileName": config.EBAY_PAYMENT_PROFILE_NAME,
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
