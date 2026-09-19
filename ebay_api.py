"""
Direct eBay API listing path - the "List on eBay (API)" button on Awaiting
Listing cards, gated behind config.EBAY_ENABLED (see item_flow.list_on_ebay_api).

This is intentionally a stub. The developer API application isn't approved
yet, so there's nothing to call - config.EBAY_ENABLED stays False (no
EBAY_APP_ID/EBAY_CERT_ID/EBAY_DEV_ID/EBAY_USER_TOKEN set) and the button
doesn't even show up. The eBay CSV batch path (ebay_csv.py, /ebay
export-batch) is the working fallback in the meantime.

This module is kept as the one place a real Sell/Inventory API integration
gets wired in later - once that happens, create_listing() below should
actually create the listing and return its eBay item ID, instead of raising.
"""
import config


async def create_listing(item: dict, listing: dict) -> dict:
    """
    Would create a live eBay listing from `item` (items table dict) and
    `listing` (ebay_listing_data dict) via eBay's API, returning at least
    {"ebay_item_id": ...} on success. Not implemented yet - always raises.
    """
    if not config.EBAY_ENABLED:
        raise NotImplementedError("Direct eBay API listing is disabled (config.EBAY_ENABLED is False).")
    raise NotImplementedError(
        "Direct eBay API listing isn't implemented yet - use the eBay CSV batch path "
        "(Add to eBay Batch) instead until this is built out."
    )
