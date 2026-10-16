"""
eBay -> bot price sync. Every EBAY_PRICE_SYNC_MINUTES (and /ebay
sync-prices) each Listed item with a live fixed-price eBay listing has its
current eBay price read (ebay_api.get_live_listing_price - the Browse API,
so hand-made Seller Hub listings work too). When it differs from the bot's
price:

  - ebay_listing_data.price is updated, with the change in the item's
    history - which is what website checkout (Stripe), the web dashboard and
    the pallet's "value still to sell" all read;
  - the item's #listed card is edited in place (its 💲 Price field, plus the
    website publisher's price field when website markers are on, so the
    website follows too). Card edits are queued (price_card_updates) and
    worked off a few at a time by update_queued_cards - Discord only allows
    so many edits to older messages per hour, and stops a bulk change cold;
  - the pallet's pinned card is refreshed.

Only the price changes - nothing else about the item, and never an item
that isn't Listed. An eBay listing that's ended or unreachable is left alone
(the order automation handles sales).
"""
import asyncio
import logging

import config
import database as db
import ebay_api
import finance_utils

log = logging.getLogger(__name__)
CONCURRENCY = 5
WEBSITE_PRICE_FIELD = "Website price cents"
CARD_EDITS_PER_RUN = 5
CARD_EDIT_GAP_SECONDS = 3
OLD_MESSAGE_EDIT_LIMIT = 30046  # Discord: "Maximum number of edits to messages older than 1 hour reached"


async def _fetch(item: dict, sem: asyncio.Semaphore):
    async with sem:
        try:
            return item, await ebay_api.get_live_listing_price(item["ebay_item_id"]), None
        except ebay_api.EbayAPIError as e:
            return item, None, str(e)


async def update_card_price(bot, item: dict, price: float):
    """Edits the item's #listed card so its price fields show `price`.
    True when edited, False when there's no card to edit, "limited" when
    Discord refused because of its old-message edit limit (try later)."""
    from cogs.item_flow import PRICE_FIELD  # avoid importing the cog at module load
    flow = bot.get_cog("ItemFlow") if bot else None
    if flow is None or config.PRESERVE_DISCORD_HISTORY:
        return False
    message = await flow._fetch_current_card(item, "listed")
    if message is None or not message.embeds:
        return False
    embeds = list(message.embeds)
    main = embeds[0]
    fields = [(f.name, f.value, f.inline) for f in main.fields]
    shown = f"${price:,.2f}"
    names = [name for name, _, _ in fields]
    if PRICE_FIELD in names:
        fields = [(n, shown if n == PRICE_FIELD else v, i) for n, v, i in fields]
    else:
        status_at = names.index("Status") + 1 if "Status" in names else len(fields)
        fields.insert(status_at, (PRICE_FIELD, shown, True))
    fields = [(n, str(round(price * 100)) if n == WEBSITE_PRICE_FIELD else v, i) for n, v, i in fields]
    main.clear_fields()
    for name, value, inline in fields:
        main.add_field(name=name, value=value, inline=inline)
    try:
        await message.edit(embeds=embeds)
    except Exception as e:
        if getattr(e, "code", None) == OLD_MESSAGE_EDIT_LIMIT:
            return "limited"
        log.exception("Couldn't update the Discord card price for item %s", item["id"])
        return False
    return True


_card_lock = asyncio.Lock()


async def update_queued_cards(bot, limit: int = None, gap: float = None) -> dict:
    """Works off up to `limit` queued card price edits, spaced out. Stops at
    Discord's old-message edit limit and leaves the rest queued. Returns
    {'edited', 'remaining', 'limited'}."""
    limit = CARD_EDITS_PER_RUN if limit is None else limit
    gap = CARD_EDIT_GAP_SECONDS if gap is None else gap
    edited, limited = 0, False
    if _card_lock.locked():
        return {"edited": 0, "remaining": db.count_price_card_updates(), "limited": False}
    async with _card_lock:
        for n, row in enumerate(db.pending_price_card_updates(limit)):
            item = db.get_item(row["item_id"])
            if item is None or item["status"] != db.STATUS_LISTED:
                db.clear_price_card_update(row["item_id"], row["price"])
                continue
            if n and gap:
                await asyncio.sleep(gap)
            outcome = await update_card_price(bot, item, row["price"])
            if outcome == "limited":
                limited = True
                break
            db.clear_price_card_update(row["item_id"], row["price"])  # edited, or no card to edit
            edited += outcome is True
    if limited:
        log.info("Discord's edit limit reached - the remaining card price updates will be retried later")
    return {"edited": edited, "remaining": db.count_price_card_updates(), "limited": limited}


async def link_listings() -> dict:
    """
    Matches every active eBay listing to its bot item by the Custom label
    (the bot's "pallet-N-item-M" SKU) and records the listing ID wherever
    it's missing - so the price sync covers listings made by hand in Seller
    Hub or before the bot recorded IDs. Never overwrites a different ID
    already on file (reported as a conflict). Returns counts and lists.
    """
    import ebay_listing_rules
    result = {"listings": 0, "created": [], "linked": [], "already": 0, "conflicts": [], "unmatched": [],
              "not_listed": []}
    listings = await ebay_api.get_active_listings()
    result["listings"] = len(listings)
    for listing in listings:
        parsed = ebay_listing_rules.parse_custom_label(listing["sku"])
        item = db.get_item_by_pallet_and_number(*parsed) if parsed else None
        if item is None or item["status"] == db.STATUS_DELETED:
            result["unmatched"].append(listing)
            continue
        outcome = db.link_ebay_listing(item["id"], listing)
        if outcome == "already":
            result["already"] += 1
        elif outcome == "conflict":
            result["conflicts"].append((item, listing))
        else:
            result[outcome].append((item, listing))
        if item["status"] != db.STATUS_LISTED:
            result["not_listed"].append((item, listing))
    return result


async def sync_prices(bot, link_first: bool = True) -> dict:
    """One pass. Returns {'checked', 'changed': [(item, old, new)], 'errors',
    'linked' (link_listings' result, or None)}."""
    if not config.EBAY_ENABLED:
        return {"checked": 0, "changed": [], "errors": ["eBay API isn't enabled."], "linked": None}
    linked = None
    if link_first:
        try:
            linked = await link_listings()
        except ebay_api.EbayAPIError as e:
            log.warning("Couldn't match eBay listings to items (%s) - syncing the ones already linked", e)
    items = db.items_for_price_sync()
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_fetch(item, sem) for item in items))
    changed, errors, pallets = [], [], set()
    for item, live, error in results:
        if error:
            errors.append(f"#{item['item_number']}: {error}")
            continue
        if not live or live["currency"] != "USD":
            continue
        old = item["list_price"]
        new = live["price"]
        if old is not None and round(old, 2) == new:
            continue
        current = db.get_item(item["id"])
        if current["status"] != db.STATUS_LISTED:
            continue  # sold or moved while we were checking
        db.update_listing_price(item["id"], new, old, "Price synced from eBay")
        db.queue_price_card_update(item["id"], new)
        changed.append((current, old, new))
        pallets.add(current["pallet_id"])
    for pallet_id in pallets:
        await finance_utils.refresh_finance_message(bot, pallet_id)
    if changed:
        log.info("eBay price sync: %d price(s) updated", len(changed))
    cards = await update_queued_cards(bot)
    return {"checked": len(items), "changed": changed, "errors": errors, "linked": linked,
            "cards_pending": cards["remaining"]}
