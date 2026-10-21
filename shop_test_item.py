"""
The disposable website-checkout test item (/admin checkout-test).

One fake item - "TEST CHECKOUT ONLY", $1.00 - in its own archived,
channel-less pallet, registered in shop_test_items. It exists so the website
can run its checkout tests end to end without touching real stock:

  - It's only ever sellable while the bot runs on Stripe TEST keys
    (STRIPE_SECRET_KEY = sk_test_...). Then it's served with a simulated
    "live listing" - only in GET /shop/catalog?scope=test and in
    refresh/checkout requests that name it - never in the normal catalog.
  - While on test keys, website checkout refuses every OTHER item, so a
    test payment can't touch real inventory.
  - On live keys it's treated as an unknown SKU everywhere.
  - A test-mode payment just flips it to Sold - no Discord cards, no eBay,
    no sale log, no QuickBooks, no buyer messages (stripe_shop.fulfill).

reset() puts it back to Listed and cancels any open test checkout holding it.
"""
from datetime import datetime, timezone

import config
import database as db
import ebay_listing_rules
import stripe_api

PALLET_NAME = "TEST CHECKOUT ONLY"
TITLE = "TEST CHECKOUT ONLY"
PRICE = 1.00


def test_mode() -> bool:
    """Test keys available for the test item: the separate
    STRIPE_TEST_SECRET_KEY, or the whole bot running on a sk_test_ key."""
    return stripe_api.is_test_key(config.STRIPE_TEST_SECRET_KEY) or \
        stripe_api.is_test_key(config.STRIPE_SECRET_KEY)


def whole_bot_on_test_keys() -> bool:
    return stripe_api.is_test_key(config.STRIPE_SECRET_KEY)


def test_skus() -> dict:
    """{sku: item_id} for test items (registered, not deleted), plus any
    named in SHOP_TEST_SKUS."""
    out = {}
    for row in db.shop_test_items():
        out[ebay_listing_rules.custom_label(row)] = row["id"]
    for sku in config.SHOP_TEST_SKUS:
        if sku not in out:
            parsed = ebay_listing_rules.parse_custom_label(sku)
            item = db.get_item_by_pallet_and_number(*parsed) if parsed else None
            if item and item["status"] != db.STATUS_DELETED:
                out[sku] = item["id"]
    return out


def is_test_item(item_id: int) -> bool:
    return item_id in test_skus().values()


def simulated_listing(item_id: int):
    """The stand-in live listing for a test item, or None (live keys, no price)."""
    if not test_mode():
        return None
    listing = db.get_ebay_listing_data(item_id) or {}
    if not listing.get("price"):
        return None
    item = db.get_item(item_id)
    return {"listing_id": f"TEST-{ebay_listing_rules.custom_label(item)}", "price": listing["price"],
            "quantity": 1, "listing_type": "FixedPriceItem"}


def create(actor_id: int) -> dict:
    """The test item - made (with its own archived pallet) if there isn't one."""
    existing = db.shop_test_items()
    if existing:
        return db.get_item(existing[0]["id"])
    pallet = db.get_pallet_by_name(PALLET_NAME)
    if pallet is None:
        pallet_id = db.create_pallet(PALLET_NAME, category_id=db.unused_negative_category_id(), created_by=actor_id,
                                     notes="Website checkout testing only - not real stock.")
        db.archive_pallet(pallet_id)  # keeps it out of every pallet list and picker
    else:
        pallet_id = pallet["id"]
    item_id = db.create_item(pallet_id, "Website checkout test item - not real stock.", [], actor_id)
    db.save_ebay_listing_data(item_id, TITLE, "", "", PRICE, {}, actor_id=actor_id)
    db.update_status(item_id, db.STATUS_LISTED, actor_id=actor_id, note="Checkout test item created")
    db.add_shop_test_item(item_id)
    return db.get_item(item_id)


async def reset(actor_id: int) -> list:
    """Back to Listed, with any open checkout holding it cancelled. Returns
    one line per test item describing what was done."""
    lines = []
    for sku, item_id in test_skus().items():
        cancelled = 0
        for checkout in db.open_stripe_checkouts_for_item(item_id):
            if test_mode() and checkout["session_id"].startswith("cs_test_"):
                try:
                    await stripe_api.expire_checkout_session(checkout["session_id"])
                except stripe_api.StripeError:
                    pass  # already expired/completed on Stripe's side
            db.update_stripe_checkout(checkout["session_id"], status="expired", note="Reset by /admin checkout-test")
            db.release_stripe_holds(checkout["session_id"])
            cancelled += 1
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_LISTED:
            db.update_status(item_id, db.STATUS_LISTED, actor_id=actor_id, note="Checkout test item reset")
        lines.append(f"`{sku}`: Listed again" + (f", {cancelled} open checkout(s) cancelled" if cancelled else ""))
    return lines


def retire(actor_id: int) -> list:
    """Soft-deletes the registered test item(s) (rows kept, like /item delete)."""
    done = []
    for row in db.shop_test_items():
        db.update_status(row["id"], db.STATUS_DELETED, actor_id=actor_id, note="Checkout test item retired")
        done.append(ebay_listing_rules.custom_label(row))
    return done


def status_lines() -> list:
    lines = [f"Stripe test keys for the test item: **{'set' if test_mode() else 'NOT set'}** - it is "
             f"{'sellable' if test_mode() else 'hidden and refused'} right now."]
    for sku, item_id in test_skus().items():
        item = db.get_item(item_id)
        listing = db.get_ebay_listing_data(item_id) or {}
        lines.append(f"`{sku}` - {listing.get('ebay_title') or TITLE} - ${listing.get('price') or 0:.2f} - "
                     f"status **{item['status']}**")
    if len(lines) == 1:
        lines.append("No test item yet - `/admin checkout-test action:create`.")
    return lines


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
