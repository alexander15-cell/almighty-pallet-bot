"""
Parses an eBay Seller Hub / File Exchange bulk-upload RESULTS CSV (what you
download from Seller Hub after uploading a /ebay export-batch CSV) to figure
out which rows actually went live and which failed - used by
/ebay import-results (cogs/ebay.py) to reconcile a batch without requiring
someone to manually confirm every item one at a time.

eBay's exact results-CSV column names vary by upload path and have changed
over time, so this matches header names loosely (case-insensitive, ignoring
punctuation/spacing) instead of expecting one fixed schema:
  - the SKU column: a header containing "customlabel" or "sku"
  - the listing ID column: a header containing "itemnumber" or "itemid"
  - an optional status/error column: a header containing "error", "status",
    or "message"

A row only counts as a confirmed success if it has a non-empty listing ID
and (when a status/error column exists) that column doesn't look like a
failure. Anything else - a missing listing ID, explicit error text, or a
SKU that isn't recognized - is reported back as unresolved rather than
guessed at: silently mis-confirming a listing that didn't actually go live
is worse than asking someone to check a few rows by hand.
"""
import csv
import io
import re

_FAILURE_WORDS = ("error", "fail", "reject", "invalid")


def _normalize(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _find_column(fieldnames, *substrings):
    for name in fieldnames:
        normalized = _normalize(name)
        if any(sub in normalized for sub in substrings):
            return name
    return None


def parse_results(csv_bytes: bytes) -> dict:
    """
    Returns:
      {
        "succeeded": [{"custom_label": str, "ebay_item_id": str}, ...],
        "failed": [{"custom_label": str, "reason": str}, ...],
        "unparsed_rows": int,   # rows with no usable SKU column value at all
        "columns_found": {"label": str|None, "item_id": str|None, "status": str|None},
      }
    """
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []

    label_col = _find_column(fieldnames, "customlabel", "sku")
    item_id_col = _find_column(fieldnames, "itemnumber", "itemid")
    status_col = _find_column(fieldnames, "error", "status", "message")

    succeeded, failed = [], []
    unparsed_rows = 0
    for row in reader:
        label = (row.get(label_col) or "").strip() if label_col else ""
        if not label:
            unparsed_rows += 1
            continue

        item_id_value = (row.get(item_id_col) or "").strip() if item_id_col else ""
        status_value = (row.get(status_col) or "").strip() if status_col else ""
        looks_like_failure = any(word in status_value.lower() for word in _FAILURE_WORDS)

        if item_id_value and not looks_like_failure:
            succeeded.append({"custom_label": label, "ebay_item_id": item_id_value})
        elif looks_like_failure and status_value:
            # An explicit error/failure message is the most useful reason
            # to show, whether or not a listing ID also happened to be
            # present.
            failed.append({"custom_label": label, "reason": status_value})
        elif not item_id_value:
            # No listing ID and no explicit failure text (e.g. a stray
            # "OK" in the status column that doesn't actually mean this
            # row succeeded) - the missing ID is the decisive, honest
            # reason, not whatever unrelated text happened to be there.
            failed.append({"custom_label": label, "reason": "no listing ID returned"})
        else:
            failed.append({"custom_label": label, "reason": status_value or "unrecognized result"})

    return {
        "succeeded": succeeded,
        "failed": failed,
        "unparsed_rows": unparsed_rows,
        "columns_found": {"label": label_col, "item_id": item_id_col, "status": status_col},
    }
