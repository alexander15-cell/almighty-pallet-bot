"""Public, versioned Discord listing contract. No database, Discord or network IO.

Only the explicitly approved listing fields are exported. Internal flags, costs,
staff details, finance records and shipping/buyer details are never consulted.
The caller supplies the destination status because cards are posted before the
source database's status transition is committed.
"""
from decimal import Decimal, InvalidOperation, localcontext
import re

FOOTER_PREFIX = "4G1P-WEBSITE/1"
STATUSES = frozenset({"listed", "sold", "shipped"})
CONDITIONS = {
    "1000": ("new", "New"),
    "1500": ("open_box", "New other (see details)"),
    # Known-faulty stock must not become the website's generic untested label.
    "3000": ("used", "Used"),
}


def _identifier(value):
    value = str(value)
    if not re.fullmatch(r"[1-9]\d{0,18}", value):
        raise ValueError("Invalid website item identity")
    return value


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        return None
    return value.strip()


def _price_cents(value):
    # SQLite stores this legacy field as REAL. Decimal(str(...)) preserves the
    # human decimal representation without multiplying binary floats or rounding
    # a fractional cent into a different approved selling price.
    if isinstance(value, bool) or value is None:
        return None
    try:
        text = str(value)
        if len(text) > 128:
            return None
        amount = Decimal(text)
        if not amount.is_finite() or not Decimal("0.01") <= amount <= Decimal("21474836.47"):
            return None
        with localcontext() as context:
            context.prec = max(28, len(amount.as_tuple().digits) + 2)
            cents = amount * 100
            if cents != cents.to_integral_value():
                return None
            return int(cents)
    except (InvalidOperation, ValueError, TypeError):
        return None


def build_website_contract(item, listing, *, status, photo_count, photo_error=False, sku_prefix=""):
    """Return a primary embed dictionary; missing listing data fails closed.

    Identity/status corruption raises ValueError so the caller can omit the
    machine-readable marker without interrupting the warehouse workflow.
    photo_count means files actually attached, not stale paths in a record.
    """
    if status not in STATUSES:
        raise ValueError("Unsupported website destination status")
    item_id = _identifier(item.get("id"))
    pallet_id = _identifier(item.get("pallet_id"))
    item_number = _identifier(item.get("item_number"))
    if not isinstance(sku_prefix, str) or (
        sku_prefix and not re.fullmatch(r"[A-Z][A-Z0-9]{0,15}-", sku_prefix)
    ):
        raise ValueError("Invalid website SKU prefix")
    sku = f"{sku_prefix}PALLET-{pallet_id}-ITEM-{item_number}"
    hold = item.get("on_hold", False)
    if hold not in (False, True, 0, 1):
        raise ValueError("Invalid website hold state")
    hold = bool(hold)
    listing = listing if isinstance(listing, dict) else {}
    title = _text(listing.get("ebay_title"), 80)
    description = _text(item.get("ai_description") or item.get("raw_description"), 4000)
    price = _price_cents(listing.get("price"))
    condition = CONDITIONS.get(str(listing.get("condition_id", "")))
    issue = "none"
    if status == "listed" and not hold:
        if not title or not description:
            issue = "missing_approved_text"
        elif listing.get("listing_format") == "Auction":
            issue = "auction_listing"
        elif listing.get("listing_format") != "FixedPrice" or price is None:
            issue = "missing_price"
        elif condition is None:
            issue = "unsupported_condition"
        elif photo_error or not isinstance(photo_count, int) or isinstance(photo_count, bool) or photo_count > 10:
            issue = "photo_error"
        elif photo_count < 1:
            issue = "missing_photos"

    fields = [
        {"name": "Website item ID", "value": item_id, "inline": True},
        {"name": "Website SKU", "value": sku, "inline": True},
        {"name": "Website status", "value": status, "inline": True},
        {"name": "Website hold", "value": str(hold).lower(), "inline": True},
        {"name": "Website issue", "value": issue, "inline": True},
    ]
    if status == "listed" and not hold and issue == "none":
        fields.extend([
            {"name": "Website price cents", "value": str(price), "inline": True},
            {"name": "Website condition", "value": condition[0], "inline": True},
            {"name": "Website condition notes", "value": condition[1], "inline": False},
        ])
    return {
        "title": title or f"Item #{item_number}",
        "description": description or "Listing details need review before website publication.",
        "footer": {"text": FOOTER_PREFIX},
        "fields": fields,
    }
