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


def test_item_ids() -> set:
    """Every test item ID - the registered ones plus all units of a test
    multi-unit group."""
    ids = set(test_skus().values())
    for group in db.all_unit_groups():
        # a test group, or a real-listing group whose SKU is in SHOP_TEST_SKUS
        # (for checking real eBay quantity writes on Stripe TEST keys)
        if group["is_test"] or group["anchor_item_id"] in ids:
            ids |= {u["id"] for u in db.unit_group_member_items(group["group_sku"])}
    return ids


def is_test_item(item_id: int) -> bool:
    return item_id in test_item_ids()


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


def _test_pallet_id(actor_id: int) -> int:
    pallet = db.get_pallet_by_name(PALLET_NAME)
    if pallet is not None:
        return pallet["id"]
    pallet_id = db.create_pallet(PALLET_NAME, category_id=db.unused_negative_category_id(), created_by=actor_id,
                                 notes="Website checkout testing only - not real stock.")
    db.archive_pallet(pallet_id)  # keeps it out of every pallet list and picker
    return pallet_id


MULTI_UNITS = 3


def create_multi(actor_id: int) -> dict:
    """The disposable MULTI-UNIT test listing: an anchor item plus two more
    units in a test unit group (3 sellable units, $1.00 each, no eBay
    listing - simulated like the single test item). Made once."""
    import unit_groups  # noqa: F401 (keeps the dependency obvious)
    for group in db.all_unit_groups():
        if group["is_test"]:
            return db.get_item(group["anchor_item_id"])
    pallet_id = _test_pallet_id(actor_id)
    anchor_id = db.create_item(pallet_id, "Website checkout MULTI-UNIT test item - not real stock.", [], actor_id)
    db.save_ebay_listing_data(anchor_id, TITLE + " (3 units)", "", "", PRICE, {}, actor_id=actor_id)
    db.update_status(anchor_id, db.STATUS_LISTED, actor_id=actor_id, note="Multi-unit checkout test item created")
    db.add_shop_test_item(anchor_id)
    anchor = db.get_item(anchor_id)
    sku = ebay_listing_rules.custom_label(anchor)
    db.create_unit_group(sku, anchor_id, None, "test", 0, is_test=True)
    for _ in range(MULTI_UNITS - 1):
        db.add_unit_group_member(sku, db.clone_item_as_unit(anchor_id, actor_id=actor_id))
    return anchor


def create(actor_id: int) -> dict:
    """The single-unit test item - made (with its own archived pallet) if there isn't one."""
    existing = [row for row in db.shop_test_items() if not db.unit_group_of(row["id"])]
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
    for sku, anchor_id in test_skus().items():
        group_sku = db.unit_group_of(anchor_id)
        unit_ids = [u["id"] for u in db.unit_group_member_items(group_sku)] if group_sku else [anchor_id]
        cancelled = 0
        for checkout in [c for item_id in unit_ids for c in db.open_stripe_checkouts_for_item(item_id)]:
            if test_mode() and checkout["session_id"].startswith("cs_test_"):
                try:
                    await stripe_api.expire_checkout_session(checkout["session_id"])
                except stripe_api.StripeError:
                    pass  # already expired/completed on Stripe's side
            db.update_stripe_checkout(checkout["session_id"], status="expired", note="Reset by /admin checkout-test")
            db.release_stripe_holds(checkout["session_id"])
            cancelled += 1
        for item_id in unit_ids:
            if db.get_item(item_id)["status"] != db.STATUS_LISTED:
                db.update_status(item_id, db.STATUS_LISTED, actor_id=actor_id, note="Checkout test item reset")
        units = f" ({len(unit_ids)} units)" if group_sku else ""
        lines.append(f"`{sku}`{units}: Listed again" + (f", {cancelled} open checkout(s) cancelled" if cancelled else ""))
    return lines


def retire(actor_id: int) -> list:
    """Soft-deletes the registered test item(s) (rows kept, like /item delete)."""
    done = []
    for row in db.shop_test_items():
        group_sku = db.unit_group_of(row["id"])
        for unit in (db.unit_group_member_items(group_sku) if group_sku else [row]):
            if unit["status"] != db.STATUS_DELETED:
                db.update_status(unit["id"], db.STATUS_DELETED, actor_id=actor_id, note="Checkout test item retired")
        done.append(ebay_listing_rules.custom_label(row))
    return done


def status_lines() -> list:
    lines = [f"Stripe test keys for the test item: **{'set' if test_mode() else 'NOT set'}** - it is "
             f"{'sellable' if test_mode() else 'hidden and refused'} right now."]
    for sku, item_id in test_skus().items():
        item = db.get_item(item_id)
        listing = db.get_ebay_listing_data(item_id) or {}
        group_sku = db.unit_group_of(item_id)
        if group_sku:
            import unit_groups
            units = db.unit_group_member_items(group_sku)
            lines.append(f"`{sku}` - {listing.get('ebay_title') or TITLE} - ${listing.get('price') or 0:.2f} - "
                         f"{len(unit_groups.sellable_units(group_sku))} of {len(units)} units sellable "
                         f"({', '.join(u['status'] for u in units)})")
            continue
        lines.append(f"`{sku}` - {listing.get('ebay_title') or TITLE} - ${listing.get('price') or 0:.2f} - "
                     f"status **{item['status']}**")
    if len(lines) == 1:
        lines.append("No test item yet - `/admin checkout-test action:create` (or `create-multi`).")
    return lines


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
