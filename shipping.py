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
import math

import config
import database as db


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


def per_item_cents(item: dict, listing: dict = None):
    """What the website shows next to an item: its own shipping in custom
    mode, None in flat mode (the flat price is per order, not per item)."""
    return item_shipping(item, listing)["cents"] if custom() else None


def order_cents(items_with_listings: list, delivery: str) -> int:
    """Shipping for a whole order: [(item, listing)]."""
    if delivery == "pickup":
        return 0
    if not custom():
        return _cents(config.STRIPE_SHIPPING_PRICE)
    return sum(item_shipping(item, listing)["cents"] for item, listing in items_with_listings)


def label() -> str:
    if not custom():
        return config.STRIPE_SHIPPING_LABEL
    return config.SHIP_LABEL
