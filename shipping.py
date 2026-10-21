"""
What the bot charges to ship an item on website / payment-link orders.

SHIPPING_MODE=flat (the default) keeps one STRIPE_SHIPPING_PRICE per order
(0 = free shipping). SHIPPING_MODE=custom prices EACH item from its own
size and weight, the way carriers bill:

  billable lb = max(actual weight, L x W x H / SHIP_DIM_DIVISOR), rounded up
  price       = SHIP_FIRST_LB + SHIP_PER_EXTRA_LB x (billable lb - 1)
                + SHIP_HANDLING, at least SHIP_MIN (and at most SHIP_MAX if set)

Weight and dimensions come from the item's listing details (entered at
Queue Review / listing). Missing those, the AI's rough packaged estimate is
used; missing that too, SHIP_DEFAULT. Same price anywhere in the US (no
zones) - set the rates to cover the average.

An order ships for the sum of its items. Local pickup is always $0.
"""
import asyncio
import math
import re
import secrets
from datetime import datetime, timedelta, timezone

import config
import database as db
import ebay_listing_rules
import shippo_api


def _cents(dollars: float) -> int:
    return int(round(float(dollars) * 100))


def _measure(item: dict, listing: dict):
    """(weight_lb, (l, w, h) or None, source) - listing details first, then
    the AI's estimate. (None, None, None) when neither has a weight."""
    for source, prefix, data in (("listing", "", listing), ("estimate", "ai_suggested_", item)):
        weight = data.get(f"{prefix}weight_lb")
        if weight and weight > 0:
            dims = tuple(data.get(f"{prefix}{d}_in") for d in ("length", "width", "height"))
            return weight, dims if all(d and d > 0 for d in dims) else None, source
    return None, None, None


def billable_lb(weight_lb: float, dims) -> int:
    dim_weight = (dims[0] * dims[1] * dims[2]) / config.SHIP_DIM_DIVISOR if dims else 0
    return max(1, math.ceil(max(weight_lb, dim_weight) - 1e-9))


def item_shipping(item: dict, listing: dict = None) -> dict:
    """{'cents', 'billable_lb', 'source' ('listing'|'estimate'|'default'|'flat')}
    for shipping this one item (custom mode)."""
    listing = listing if listing is not None else (db.get_ebay_listing_data(item["id"]) or {})
    weight, dims, source = _measure(item, listing)
    if weight is None:
        return {"cents": _cents(config.SHIP_DEFAULT), "billable_lb": None, "source": "default"}
    lb = billable_lb(weight, dims)
    price = config.SHIP_FIRST_LB + config.SHIP_PER_EXTRA_LB * (lb - 1) + config.SHIP_HANDLING
    price = max(price, config.SHIP_MIN)
    if config.SHIP_MAX > 0:
        price = min(price, config.SHIP_MAX)
    return {"cents": _cents(price), "billable_lb": lb, "source": source}


def custom() -> bool:
    return config.SHIPPING_MODE == "custom"


def live_rates() -> bool:
    return config.SHIPPING_MODE == "shippo"


class QuoteError(Exception):
    """Why shipping couldn't be quoted - safe to show."""


def _default_parcel() -> dict:
    try:
        l, w, h, lb = (float(x) for x in re.split(r"[x,\s]+", config.SHIP_DEFAULT_PARCEL.lower()) if x)
    except ValueError:
        l, w, h, lb = 14.0, 12.0, 10.0, 8.0
    return {"length": l, "width": w, "height": h, "weight": lb}


def parcel_for(item: dict, listing: dict) -> tuple:
    """(parcel, source) - the item's packed size/weight, else the default box."""
    weight, dims, source = _measure(item, listing)
    default = _default_parcel()
    if weight is None:
        return default, "default"
    if not dims:
        dims = (default["length"], default["width"], default["height"])
    return {"length": dims[0], "width": dims[1], "height": dims[2], "weight": weight}, source


def _charge_cents(carrier_dollars: float) -> int:
    return int(round((carrier_dollars * (1 + config.SHIPPO_MARKUP_PERCENT / 100) + config.SHIPPO_HANDLING) * 100))


async def quote(items_with_listings: list, zip_code: str) -> dict:
    """
    Live shipping for these items to a US ZIP: each item's cheapest Shippo
    rate (one box per item) + markup/handling, summed. Saved so checkout can
    use exactly this amount. Returns {'quote_id', 'zip', 'shipping_cents',
    'expires_at', 'items': [{item_id, sku, shipping_cents, carrier,
    service, days, parcel}]}. Raises QuoteError.
    """
    zip5 = (zip_code or "").strip()[:5]
    if not re.fullmatch(r"\d{5}", zip5):
        raise QuoteError("Enter a 5-digit US ZIP code.")
    parcels = [parcel_for(item, listing) for item, listing in items_with_listings]
    try:
        all_rates = await asyncio.gather(*(shippo_api.get_rates(p, zip5) for p, _ in parcels))
    except shippo_api.ShippoError as e:
        raise QuoteError(f"Couldn't get shipping rates right now ({e}).")
    lines = []
    for (item, _), (parcel, source), rates in zip(items_with_listings, parcels, all_rates):
        best = rates[0]
        lines.append({"item_id": item["id"], "sku": ebay_listing_rules.custom_label(item),
                      "shipping_cents": _charge_cents(best["amount"]), "carrier": best["provider"],
                      "service": best["service"], "days": best["days"], "parcel": source})
    total = sum(line["shipping_cents"] for line in lines)
    quote_id = "shq_" + secrets.token_urlsafe(12)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=config.SHIPPO_QUOTE_MINUTES)).isoformat()
    db.save_shipping_quote(quote_id, [i["id"] for i, _ in items_with_listings], zip5, total, lines, expires_at)
    return {"quote_id": quote_id, "zip": zip5, "shipping_cents": total, "expires_at": expires_at, "items": lines}


def usable_quote(quote_id: str, item_ids: list) -> dict:
    """The saved quote, if it's still valid for exactly these items."""
    quote_row = db.get_shipping_quote(quote_id) if quote_id else None
    if not quote_row:
        raise QuoteError("Get a shipping quote for this cart first.")
    if quote_row["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise QuoteError("That shipping quote expired - get a new one.")
    if sorted(quote_row["item_ids"]) != sorted(item_ids):
        raise QuoteError("That shipping quote is for a different cart - get a new one.")
    return quote_row


def per_item_cents(item: dict, listing: dict = None):
    """What the website shows next to an item: its own shipping in custom
    mode, None in flat mode (the flat price is per order, not per item)."""
    return item_shipping(item, listing)["cents"] if custom() else None


def order_cents(items_with_listings: list, delivery: str) -> int:
    """Shipping for a whole order: [(item, listing)]."""
    if delivery == "pickup":
        return 0
    if live_rates():
        raise QuoteError("Shipping needs a live quote (ZIP code) first.")
    if not custom():
        return _cents(config.STRIPE_SHIPPING_PRICE)
    return sum(item_shipping(item, listing)["cents"] for item, listing in items_with_listings)


def label() -> str:
    if live_rates():
        return config.SHIP_LABEL
    if not custom():
        return config.STRIPE_SHIPPING_LABEL
    return config.SHIP_LABEL
