"""
The website's view of what's for sale ("ebay-catalog-v1" - see
docs/website-checkout.md section 8). Active eBay listings are the source of
truth for listing ID and price, matched to bot items by Custom label
(pallet-N-item-M) - never by an old listing ID. The bot's own status and
checkout holds decide whether anything is actually available:

  - only a Listed item, live on eBay as a fixed-price listing, not held by a
    checkout, reports available_quantity > 0;
  - sold / shipped / on-hold items report 0 even while eBay still shows the
    listing; any other bot state is reported as "unavailable";
  - an ended or missing listing reports ebay_active=false with no listing ID
    or price - an ended listing's price is never used as current.

Everything is read from the last COMPLETE active-listings pass
(price_sync.link_listings - every page fetched). ebay_checked_at is that
pass's real time; when it's older than EBAY_CATALOG_MAX_AGE_MINUTES, or no
pass has ever succeeded, CatalogUnavailable is raised (the route answers 503).
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import config
import database as db
import price_sync

log = logging.getLogger(__name__)

CONTRACT = "ebay-catalog-v1"
SKU_RE = re.compile(r"^pallet-(\d+)-item-(\d+)$")
MAX_REFRESH_ITEMS = 20
REFRESH_TIMEOUT_SECONDS = 25
REFRESH_REUSE_SECONDS = 30      # a pass this fresh is reused by /refresh (its true time is reported)
CHECKOUT_REUSE_SECONDS = 120    # ...and by checkout's price check
BOT_STATUS = {db.STATUS_LISTED: "listed", db.STATUS_SOLD: "sold", db.STATUS_SHIPPED: "shipped",
              db.STATUS_ON_HOLD: "on_hold"}

_refresh_lock = asyncio.Lock()


class CatalogUnavailable(Exception):
    """eBay couldn't be checked, or the last check is too old to trust."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(checked_at: str):
    if not checked_at:
        return None
    return (_now() - datetime.fromisoformat(checked_at)).total_seconds()


def canonical_sku(value) -> str:
    """'pallet-N-item-M' with no leading zeros, or None if it isn't one."""
    match = SKU_RE.match(value.strip().lower()) if isinstance(value, str) else None
    return f"pallet-{int(match.group(1))}-item-{int(match.group(2))}" if match else None


async def refresh_snapshot(reuse_seconds: float = 0) -> str:
    """Runs a complete active-listings pass (unless one finished within
    `reuse_seconds`), bounded in time. Returns the pass's checked_at; raises
    CatalogUnavailable. Concurrent callers share one pass."""
    async with _refresh_lock:
        checked_at = db.snapshot_checked_at()
        age = _age_seconds(checked_at)
        if age is not None and age <= reuse_seconds:
            return checked_at
        try:
            await asyncio.wait_for(price_sync.link_listings(), timeout=REFRESH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            db.record_snapshot_failure("timed out")
            log.warning("eBay active-listings check timed out")
            raise CatalogUnavailable("eBay couldn't be checked right now.")
        except Exception as e:
            log.warning("eBay active-listings check failed: %s", e)
            raise CatalogUnavailable("eBay couldn't be checked right now.")
        return db.snapshot_checked_at()


def _fresh_checked_at() -> str:
    checked_at = db.snapshot_checked_at()
    age = _age_seconds(checked_at)
    if age is None or age > config.EBAY_CATALOG_MAX_AGE_MINUTES * 60:
        raise CatalogUnavailable("The eBay check is out of date - try again shortly.")
    return checked_at


def _record(sku: str, item, listing, held: bool, checked_at) -> dict:
    status = BOT_STATUS.get(item["status"], "unavailable") if item else "unavailable"
    fixed_price = listing is not None and listing.get("listing_type") != "Chinese"
    price = listing.get("price") if fixed_price else None
    quantity = listing.get("quantity") if listing else None
    available = 0
    if status == "listed" and fixed_price and price and not held:
        available = max(0, int(quantity)) if quantity is not None else 0
    return {
        "sku": sku,
        "ebay_listing_id": str(listing["listing_id"]) if listing else None,
        "ebay_active": listing is not None,
        "price_cents": int(round(float(price) * 100)) if price else None,
        "currency": "usd",
        "available_quantity": available,
        "bot_status": status,
        "ebay_checked_at": checked_at if item else None,
    }


def _items_by_sku() -> dict:
    """{sku: item}, preferring a live item over a deleted one with the same SKU."""
    out = {}
    for item in db.catalog_items():
        sku = f"pallet-{item['pallet_id']}-item-{item['item_number']}"
        if sku not in out or out[sku]["status"] == db.STATUS_DELETED:
            out[sku] = item
    return out


def _test_listing(sku: str, item):
    """A stand-in "live listing" for a disposable test item - only while the
    bot runs on Stripe TEST keys, and only for SKUs named in SHOP_TEST_SKUS.
    Lets the website test checkout end to end without a real eBay listing."""
    if not item or sku not in config.SHOP_TEST_SKUS or not config.STRIPE_SECRET_KEY.startswith("sk_test_"):
        return None
    listing = db.get_ebay_listing_data(item["id"]) or {}
    if not listing.get("price"):
        return None
    return {"listing_id": "TEST", "price": listing["price"], "quantity": 1, "listing_type": "FixedPriceItem"}


def _records(skus: list, checked_at: str) -> list:
    items = _items_by_sku()
    active = db.current_active_listings()
    for sku in skus:
        test = _test_listing(sku, items.get(sku))
        if test:
            active[sku] = test
    held = db.active_stripe_holds([items[s]["id"] for s in skus if s in items])
    return [_record(s, items.get(s), active.get(s), s in items and items[s]["id"] in held, checked_at)
            for s in skus]


def catalog() -> dict:
    """GET /shop/catalog - every tracked item. Raises CatalogUnavailable."""
    checked_at = _fresh_checked_at()
    skus = list(_items_by_sku())
    return {"catalog_contract": CONTRACT, "complete": True, "generated_at": _now().isoformat(),
            "items": _records(skus, checked_at)}


def parse_skus(values) -> list:
    """1-20 unique canonical SKUs from a request, or ValueError."""
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_REFRESH_ITEMS:
        raise ValueError(f"items must be a list of 1-{MAX_REFRESH_ITEMS} SKUs.")
    skus = [canonical_sku(v) for v in values]
    if None in skus:
        raise ValueError("Every item must be a SKU like \"pallet-1-item-96\".")
    if len(set(skus)) != len(skus):
        raise ValueError("Each SKU may appear only once.")
    return skus


async def refresh(skus: list) -> dict:
    """POST /shop/catalog/refresh - a fresh eBay check for these SKUs (one
    result each, unknown ones as unavailable). Also brings the bot's own
    price for them in line with eBay. Raises CatalogUnavailable."""
    checked_at = await refresh_snapshot(REFRESH_REUSE_SECONDS)
    records = _records(skus, checked_at)
    await _sync_bot_prices(records)
    return {"catalog_contract": CONTRACT, "scope": "requested_items", "generated_at": _now().isoformat(),
            "items": records}


async def _sync_bot_prices(records: list):
    """Copies eBay's current price into the bot for Listed items whose
    price differs (same as the hourly price sync)."""
    for record in records:
        if record["bot_status"] != "listed" or record["price_cents"] is None:
            continue
        pallet_id, number = map(int, SKU_RE.match(record["sku"]).groups())
        item = db.get_item_by_pallet_and_number(pallet_id, number)
        listing = db.get_ebay_listing_data(item["id"]) if item else None
        new = record["price_cents"] / 100
        if not listing or (listing.get("price") is not None and round(listing["price"], 2) == new):
            continue
        db.update_listing_price(item["id"], new, listing.get("price"), "Price synced from eBay")
        db.queue_price_card_update(item["id"], new)


async def verify_for_checkout(skus: list, website_prices: dict) -> dict:
    """
    Fresh eBay + bot check before a website checkout. Returns
    {'unavailable': [sku], 'price_changes': [{sku, website_cents,
    current_cents}], 'prices': {sku: current cents}}. A SKU is unavailable
    unless it's Listed, live on eBay at a fixed price with exactly one unit,
    and not held. Raises CatalogUnavailable.
    """
    checked_at = await refresh_snapshot(CHECKOUT_REUSE_SECONDS)
    records = _records(skus, checked_at)
    await _sync_bot_prices(records)
    unavailable, changes, prices = [], [], {}
    for record in records:
        sku = record["sku"]
        if record["available_quantity"] != 1 or record["price_cents"] is None:
            unavailable.append(sku)
            continue
        prices[sku] = record["price_cents"]
        if website_prices.get(sku) != record["price_cents"]:
            changes.append({"sku": sku, "website_cents": website_prices.get(sku),
                            "current_cents": record["price_cents"]})
    return {"unavailable": unavailable, "price_changes": changes, "prices": prices}
