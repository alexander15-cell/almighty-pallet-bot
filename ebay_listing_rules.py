"""
Field-level eBay listing rules shared by the direct API listing path
(ebay_api.py's create_listing) and everything that maps an eBay SKU back
to a tracked item (order sync, order automation, fee reporting, the
website catalog).

These used to live in ebay_csv.py alongside the CSV batch upload path,
which has since been removed (every eBay listing now goes through the
API). The rules themselves are unchanged - each was tuned against a real
eBay rejection:

  - custom_label / parse_custom_label: this bot's SKU, "pallet-N-item-M",
    and its reverse. Every listing carries it, so every eBay order line
    can be matched back to a tracked item.
  - resolved_condition_id: falls back to config.EBAY_CONDITION_FALLBACK_ID
    for a condition ID outside config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS
    (error 21916883 - e.g. "1500"/"1750" on Lights/Lamps). The API path
    first asks eBay's live per-category policy (ebay_api.
    resolve_condition_id_with_live_check) and only uses this if that
    lookup fails.
  - infer_type: a best-effort "Type" item specific from the title
    (config.EBAY_TYPE_KEYWORDS) - some categories reject a listing with no
    Type at all (error 21919303), so the caller falls back to "Does Not
    Apply" when this finds nothing.
  - estimated_weight_lb: a category-keyword weight estimate
    (config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB) for an item with no real
    captured weight - Calculated shipping rejects a listing with none.
"""
import re

import config

_CUSTOM_LABEL_RE = re.compile(r"^pallet-(\d+)-item-(\d+)$")


def custom_label(item: dict) -> str:
    """A stable, human-traceable SKU for this item - "pallet-N-item-M"."""
    return f"pallet-{item['pallet_id']}-item-{item['item_number']}"


def parse_custom_label(sku: str):
    """
    Reverses custom_label() - the (pallet_id, item_number) pair a SKU
    encodes, as a tuple of ints, or None if `sku` doesn't match this bot's
    own format at all (a listing created some other way, or a stale/
    malformed SKU on an eBay order).
    """
    if not sku:
        return None
    match = _CUSTOM_LABEL_RE.match(sku.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def resolved_condition_id(condition_id: str, item_label: str) -> str:
    """
    Falls back to config.EBAY_CONDITION_FALLBACK_ID when `condition_id`
    isn't in config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS - see this module's
    docstring and that config constant's own comment (error 21916883).
    """
    if condition_id in config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS:
        return condition_id
    print(
        f"[ebay_listing_rules] {item_label}: condition ID {condition_id!r} isn't in the broadly-accepted "
        f"set - falling back to {config.EBAY_CONDITION_FALLBACK_ID!r} to avoid a category rejection."
    )
    return config.EBAY_CONDITION_FALLBACK_ID


def infer_type(title: str) -> str:
    """
    Best-effort "Type" item specific from an item's title when Queue Review
    didn't capture one. Returns "" (not a placeholder) when nothing in
    config.EBAY_TYPE_KEYWORDS matches - the caller logs that and falls back
    to "Does Not Apply", since eBay requires the specific non-empty in some
    categories regardless.
    """
    haystack = (title or "").lower()
    for keyword, type_value in config.EBAY_TYPE_KEYWORDS.items():
        if keyword in haystack:
            return type_value
    return ""


def estimated_weight_lb(category_path: str) -> float:
    """
    Fallback used only when an item has no real captured weight_lb - a
    rough estimate, not real per-item data. Matches the first
    config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB keyword found in the category
    path, else config.EBAY_DEFAULT_WEIGHT_FALLBACK_LB.
    """
    haystack = (category_path or "").lower()
    for keyword, weight_lb in config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB.items():
        if keyword in haystack:
            return weight_lb
    return config.EBAY_DEFAULT_WEIGHT_FALLBACK_LB
