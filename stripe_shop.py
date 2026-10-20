"""
Website sales through Stripe Checkout (hosted page) with Stripe Tax.

  start_checkout()   - the website's Checkout button (and the dashboard's
                       "Send payment link"): checks each item is still for
                       sale, uses the bot's OWN price (never the browser's),
                       holds the items, and returns Stripe's checkout URL.
  handle_event()     - Stripe webhooks (signature-checked by the caller):
                       paid -> fulfill(); expired/failed -> release the hold.
  fulfill()          - marks the items Sold (same ItemFlow.do_mark_sold as
                       the Discord button), saves the buyer's shipping
                       address for the Pirate Ship export, ends the eBay
                       listings, logs the sale (non-taxable Sales Receipt to
                       Website Sales), and books Stripe's sales tax to the
                       tax-payable account and Stripe's fee to the fees
                       account in one Journal Entry - so Undeposited Funds
                       holds exactly what Stripe pays out. Every step is
                       recorded, so the retry loop never repeats one.

One-of-a-kind stock: an item is only sellable while it's Listed and not
held by another open checkout. If it sells elsewhere (eBay) while a buyer is
on the Stripe page, fulfill() flags the order for a refund instead of
marking anything twice.
"""
import asyncio
import json
import re
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
import database as db
import ebay_api
import ebay_listing_rules
import finance_utils
import quickbooks
import stripe_api

log = logging.getLogger(__name__)
SYSTEM_ACTOR = 0  # item_events.actor_id for steps taken by the bot itself
CONFLICT = "REFUND NEEDED:"  # note prefix for an order whose item sold elsewhere

_locks = defaultdict(asyncio.Lock)


class CheckoutError(Exception):
    """Why a checkout can't start - safe to show the buyer. `skus`: the
    items it's about, when it's about particular items."""

    def __init__(self, message: str, skus: list = None):
        super().__init__(message)
        self.skus = skus or []


def _cents(dollars: float) -> int:
    return int(round(float(dollars) * 100))


def resolve_items(refs: list) -> list:
    """Bot items for the website's references - an item ID, or the bot's
    SKU ("pallet-N-item-M")."""
    items = []
    for ref in refs:
        item = None
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            item = db.get_item(int(ref))
        elif isinstance(ref, str):
            # The bot's SKU, or the website's product slug that ends with it
            # ("ebay-318921853363-pallet-1-item-100").
            match = re.search(r"pallet-\d+-item-\d+$", ref.strip())
            parsed = ebay_listing_rules.parse_custom_label(match.group(0)) if match else None
            if parsed:
                pallet_id, number = parsed
                item = db.get_item_by_pallet_and_number(pallet_id, number)
        if item is None:
            raise CheckoutError(f"Item {ref!r} isn't for sale.")
        items.append(item)
    if len({i["id"] for i in items}) != len(items):
        raise CheckoutError("The same item is in the cart twice - each one is one of a kind.")
    return items


def check_sellable(items: list) -> list:
    """[(item, listing)] if every item can be sold right now, else CheckoutError."""
    held = db.active_stripe_holds([i["id"] for i in items])
    out = []
    for item in items:
        listing = db.get_ebay_listing_data(item["id"]) or {}
        title = listing.get("ebay_title") or item.get("ai_title") or f"Item #{item['item_number']}"
        sku = ebay_listing_rules.custom_label(item)
        if item["status"] != db.STATUS_LISTED:
            raise CheckoutError(f"Sorry - \"{title}\" just sold.", skus=[sku])
        if item["id"] in held:
            raise CheckoutError(f"Someone is checking out \"{title}\" right now - try again in a little while.",
                                skus=[sku])
        if not listing.get("price") or listing["price"] <= 0:
            raise CheckoutError(f"\"{title}\" doesn't have a price yet.")
        out.append((item, listing))
    return out


def _photo(item: dict):
    try:
        urls = json.loads(item.get("photo_public_urls") or "[]")
    except ValueError:
        urls = []
    return [u for u in urls if u][:1]


def build_session_params(sellable: list, delivery: str, expires_at: datetime, reference: str,
                         unit_amounts: dict = None) -> dict:
    """unit_amounts: {item_id: cents} to charge instead of the bot's price
    (the website's own prices - see start_checkout)."""
    line_items = []
    for item, listing in sellable:
        title = (listing.get("ebay_title") or item.get("ai_title") or f"Item #{item['item_number']}")[:250]
        product = {"name": title, "tax_code": config.STRIPE_TAX_CODE,
                   "metadata": {"item_id": str(item["id"]), "sku": ebay_listing_rules.custom_label(item)}}
        images = _photo(item)
        if images:
            product["images"] = images
        line_items.append({"quantity": 1, "price_data": {
            "currency": "usd",
            "unit_amount": (unit_amounts or {}).get(item["id"], _cents(listing["price"])),
            "tax_behavior": "exclusive",
            "product_data": product}})
    params = {
        "mode": "payment",
        "line_items": line_items,
        "automatic_tax": {"enabled": True},
        "expires_at": int(expires_at.timestamp()),
        "success_url": config.STRIPE_SUCCESS_URL + ("&" if "?" in config.STRIPE_SUCCESS_URL else "?")
        + "session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": config.STRIPE_CANCEL_URL,
        "client_reference_id": reference,
        "metadata": {"source": "pallet-bot", "item_ids": ",".join(str(i["id"]) for i, _ in sellable)},
        "payment_intent_data": {"description": "4 Guys 1 Pallet Overstock order"},
    }
    if delivery == "ship":
        params["shipping_address_collection"] = {"allowed_countries": ["US"]}
        params["shipping_options"] = [{"shipping_rate_data": {
            "type": "fixed_amount", "display_name": config.STRIPE_SHIPPING_LABEL[:100],
            "fixed_amount": {"amount": _cents(config.STRIPE_SHIPPING_PRICE), "currency": "usd"},
            "tax_behavior": "exclusive"}}]
    else:
        # Local pickup: tax from the billing address (a local buyer).
        params["billing_address_collection"] = "required"
    return params


MAX_UNIT_CENTS = 2_000_000  # $20,000 - anything above is treated as a mistake


def _website_unit_amounts(refs: list, items: list, website_prices) -> dict:
    """{item_id: cents} from the website's own prices, which must cover
    exactly the requested items with positive whole-cent USD amounts."""
    if not isinstance(website_prices, dict):
        raise CheckoutError("website_prices must be an object of item -> cents.")
    if set(map(str, website_prices)) != set(map(str, refs)) or len(website_prices) != len(refs):
        raise CheckoutError("website_prices must list exactly the items in the cart.")
    amounts = {}
    for ref, item in zip(refs, items):
        cents = website_prices.get(ref, website_prices.get(str(ref)))
        if isinstance(cents, bool) or not isinstance(cents, int) or not 0 < cents <= MAX_UNIT_CENTS:
            raise CheckoutError(f"The price for {ref!r} must be a positive whole number of cents.")
        amounts[item["id"]] = cents
    return amounts


async def start_checkout(refs: list, source: str = "website", delivery: str = "ship", actor_id: int = None,
                         website_prices: dict = None) -> dict:
    """
    Holds the items and creates the Stripe Checkout page. Returns
    {'url', 'session_id', 'expires_at', 'subtotal_cents', 'unit_amounts'
    ({ref: cents actually sent to Stripe})}; raises CheckoutError.

    website_prices ({ref: cents}) - only ever passed for a request the
    website's server authenticated with the shared key (webapp/shop.py) -
    charges the website's own prices instead of the bot's. Every other
    check (Listed, not held, one of each) still applies.
    """
    if not config.STRIPE_ENABLED:
        raise CheckoutError("Online checkout isn't available right now.")
    if not refs or len(refs) > 20:
        raise CheckoutError("The cart needs between 1 and 20 items.")
    items = resolve_items(refs)
    unit_amounts = _website_unit_amounts(refs, items, website_prices) if website_prices is not None else None
    key = ",".join(sorted(str(i["id"]) for i in items))
    async with _locks[key]:
        sellable = check_sellable(items)
        now = datetime.now(timezone.utc)
        if source == "link":
            expires_at = now + timedelta(hours=config.STRIPE_LINK_HOURS) - timedelta(minutes=1)
        else:
            expires_at = now + timedelta(minutes=config.STRIPE_HOLD_MINUTES, seconds=30)
        params = build_session_params(sellable, delivery, expires_at, reference=f"{source}-{key}",
                                      unit_amounts=unit_amounts)
        sent = {item["id"]: line["price_data"]["unit_amount"]
                for (item, _), line in zip(sellable, params["line_items"])}
        try:
            session = await stripe_api.create_checkout_session(params)
        except stripe_api.StripeError as e:
            log.error("Stripe checkout creation failed: %s", e)
            raise CheckoutError("Checkout couldn't start - please try again in a minute.")
        try:
            db.create_stripe_checkout(session["id"], source, [i["id"] for i in items], session.get("url"), delivery,
                                      expires_at.isoformat(), created_by=actor_id)
        except sqlite3.IntegrityError:
            # Lost a race with another buyer - cancel the page we just made.
            try:
                await stripe_api.expire_checkout_session(session["id"])
            except stripe_api.StripeError:
                log.exception("Couldn't expire Stripe session %s after losing an item race", session["id"])
            raise CheckoutError("Someone else is checking out one of these items right now.",
                                skus=[ebay_listing_rules.custom_label(i) for i in items])
        # What Stripe will actually charge before tax/shipping - from Stripe's
        # own answer, not just what we asked for.
        subtotal = session.get("amount_subtotal")
        if subtotal is not None and subtotal != sum(sent.values()):
            await _cancel(session["id"])
            raise CheckoutError("Checkout prices didn't come out right - please try again.")
    return {"url": session.get("url"), "session_id": session["id"], "expires_at": expires_at.isoformat(),
            "livemode": session.get("livemode", not session["id"].startswith("cs_test_")),
            "subtotal_cents": subtotal if subtotal is not None else sum(sent.values()),
            "unit_amounts": {ref: sent[item["id"]] for ref, item in zip(refs, items)}}


async def _cancel(session_id: str):
    try:
        await stripe_api.expire_checkout_session(session_id)
    except stripe_api.StripeError:
        log.exception("Couldn't expire Stripe session %s", session_id)
    db.update_stripe_checkout(session_id, status="expired", note="Cancelled: amounts didn't match")
    db.release_stripe_holds(session_id)


def session_state(session_id: str):
    """For the website's server: where a checkout stands, or None."""
    checkout = db.get_stripe_checkout(session_id)
    if not checkout:
        return None
    status = checkout["status"]
    refund = (checkout.get("note") or "").startswith(CONFLICT)
    if refund:
        state = "refund_pending"
    elif status in ("paid", "fulfilled", "attention"):
        state = "paid"
    else:
        state = status  # open / processing / expired / failed
    items = [db.get_item(i) for i in checkout["item_ids"]]
    total = checkout.get("amount_total")
    return {"session_id": session_id, "state": state, "paid": state == "paid",
            "items": [ebay_listing_rules.custom_label(i) for i in items if i],
            "amount_total_cents": round(total * 100) if total is not None else None}


# ================================================================ webhooks

async def handle_event(bot, event: dict) -> str:
    """Acts on one verified Stripe event. Returns a short description."""
    event_type = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    session_id = obj.get("id") if obj.get("object") == "checkout.session" else None
    if not session_id or not db.get_stripe_checkout(session_id):
        return "ignored (not a bot checkout)"
    if event.get("id") and not db.record_stripe_event(event["id"], event_type):
        return "duplicate"
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("payment_status") in ("paid", "no_payment_required"):
            db.update_stripe_checkout(session_id, status="paid")
            asyncio.get_running_loop().create_task(_fulfill_logged(bot, session_id))
            return "paid - fulfilling"
        db.update_stripe_checkout(session_id, status="processing")  # e.g. a bank payment still clearing
        return "processing"
    if event_type in ("checkout.session.expired", "checkout.session.async_payment_failed"):
        status = "expired" if event_type.endswith("expired") else "failed"
        checkout = db.get_stripe_checkout(session_id)
        if checkout["status"] in ("open", "processing"):
            db.update_stripe_checkout(session_id, status=status)
            db.release_stripe_holds(session_id)
        return f"{status} - items released"
    return "ignored"


async def _fulfill_logged(bot, session_id: str):
    try:
        await fulfill(bot, session_id)
    except Exception:
        log.exception("Stripe order %s fulfillment failed - the retry loop will pick it up", session_id)


# ============================================================= fulfillment

def _addr(session: dict):
    details = ((session.get("collected_information") or {}).get("shipping_details")
               or session.get("shipping_details") or {})
    address = details.get("address") or {}
    if not address.get("line1"):
        return None
    return {"name": details.get("name") or (session.get("customer_details") or {}).get("name") or "",
            "line1": address.get("line1"), "line2": address.get("line2") or "", "city": address.get("city") or "",
            "state": address.get("state") or "", "postal_code": address.get("postal_code") or "",
            "country": address.get("country") or "US"}


def _fee(session: dict):
    pi = session.get("payment_intent")
    if not isinstance(pi, dict):
        return None
    charge = pi.get("latest_charge")
    bt = charge.get("balance_transaction") if isinstance(charge, dict) else None
    return round(bt["fee"] / 100, 2) if isinstance(bt, dict) and bt.get("fee") is not None else None


async def fulfill(bot, session_id: str) -> dict:
    """Idempotent: safe to call again after a partial failure."""
    async with _locks[f"fulfill:{session_id}"]:
        checkout = db.get_stripe_checkout(session_id)
        if not checkout or checkout["status"] not in ("paid", "attention"):
            return {"ok": False, "message": "Nothing to fulfill."}
        if (checkout.get("note") or "").startswith(CONFLICT):
            return {"ok": False, "message": "Needs a refund - see the order notice."}
        session = await stripe_api.retrieve_checkout_session(session_id)
        if session.get("payment_status") not in ("paid", "no_payment_required"):
            return {"ok": False, "message": "Not paid yet."}
        totals = session.get("total_details") or {}
        tax = round((totals.get("amount_tax") or 0) / 100, 2)
        shipping = round((totals.get("amount_shipping") or 0) / 100, 2)
        total = round((session.get("amount_total") or 0) / 100, 2)
        income = round(total - tax, 2)  # items + shipping charged, before tax
        fee = _fee(session)
        db.update_stripe_checkout(session_id, amount_total=total, amount_tax=tax, amount_shipping=shipping,
                                  stripe_fee=fee)
        items = [db.get_item(i) for i in checkout["item_ids"]]
        notes, problems = [], []
        if session.get("livemode") is False:
            return await _fulfill_test_payment(bot, checkout, session, items, total, tax, fee)

        # 1. Mark Sold (exactly like the Discord button), save the shipping address, end eBay.
        if not checkout["sold_marked"]:
            flow = bot.get_cog("ItemFlow") if bot else None
            address = _addr(session) if checkout["delivery"] == "ship" else None
            for item in items:
                if item["status"] == db.STATUS_LISTED:
                    if flow is None:
                        raise RuntimeError("ItemFlow isn't loaded yet")
                    await flow.do_mark_sold(item["id"], "Website", SYSTEM_ACTOR)
                elif item.get("sale_platform") != "Website":
                    problems.append(f"Item #{item['item_number']} was already sold elsewhere ({item['status']}) - "
                                    "refund this buyer in Stripe.")
                    continue
                if address:
                    db.set_shipping_info(item["id"], address["name"], address["line1"], address["line2"],
                                         address["city"], address["state"], address["postal_code"],
                                         address["country"], actor_id=SYSTEM_ACTOR)
                listing = db.get_ebay_listing_data(item["id"]) or {}
                if listing.get("ebay_item_id"):
                    try:
                        notes.append(f"#{item['item_number']}: " + await ebay_api.end_listing_for_item(item))
                    except ebay_api.EbayAPIError as e:
                        notes.append(f"#{item['item_number']}: couldn't end the eBay listing ({e}) - end it in "
                                     "Seller Hub NOW so it can't sell twice.")
            db.update_stripe_checkout(session_id, sold_marked=1)
            db.release_stripe_holds(session_id)
        if problems:
            db.update_stripe_checkout(session_id, status="attention", note=CONFLICT + " " + " ".join(problems))
            await _notify(bot, checkout, session, total, tax, fee, notes + problems, attention=True)
            return {"ok": False, "message": " ".join(problems)}

        # 2. Log the sale + Sales Receipt (Website Sales, no QuickBooks tax on top).
        sale_id = checkout["sale_id"]
        if not sale_id:
            date = datetime.fromtimestamp(session.get("created") or datetime.now().timestamp(),
                                          timezone.utc).strftime("%Y-%m-%d")
            prices = [(db.get_ebay_listing_data(i["id"]) or {}).get("price") or 0 for i in items]
            shares = _split(income, prices)
            sale_id = db.create_sale("Website", date, income, False, created_by=SYSTEM_ACTOR)
            for item, share in zip(items, shares):
                db.add_sale_item(sale_id, item["id"], share)
                db.record_item_sale(item["id"], share, "Website", actor_id=SYSTEM_ACTOR)
            db.update_stripe_checkout(session_id, sale_id=sale_id)
            result = await finance_utils.post_sale_to_quickbooks(sale_id, "Website", income, False, date)
            await finance_utils.update_accounting_entries(
                bot, sale_id, receipt_doc=result.get("sales_receipt_doc"), error=result.get("error"))
            if not result["ok"]:
                notes.append(f"QuickBooks Sales Receipt failed ({result['error']}) - "
                             f"`/finance retry-sale sale_id:{sale_id}`.")

        # 3. Sales tax -> tax payable, Stripe fee -> fees, against Undeposited Funds.
        if not checkout["fees_booked"]:
            booked = await _book_tax_and_fee(session_id, tax, fee, notes)
            if booked:
                db.update_stripe_checkout(session_id, fees_booked=1)

        for pallet_id in {i["pallet_id"] for i in items}:
            await finance_utils.refresh_finance_message(bot, pallet_id)
        checkout = db.get_stripe_checkout(session_id)
        status = "fulfilled" if checkout["fees_booked"] else "attention"
        db.update_stripe_checkout(session_id, status=status, note=" ".join(notes) or None)
        if not checkout["notified"]:
            await _notify(bot, checkout, session, total, tax, fee, notes, attention=status != "fulfilled")
            db.update_stripe_checkout(session_id, notified=1)
        return {"ok": True, "sale_id": sale_id, "status": status}


async def _fulfill_test_payment(bot, checkout, session, items, total, tax, fee) -> dict:
    """A Stripe TEST-mode payment (docs/website-checkout.md section 7): only
    the checkout test item may be affected, and only by flipping it to Sold.
    No Discord cards, no eBay, no sale log, no QuickBooks, no buyer messages."""
    import shop_test_item
    session_id = checkout["session_id"]
    test_ids = set(shop_test_item.test_skus().values())
    stray = [i for i in items if i["id"] not in test_ids]
    if stray:
        note = ("TEST MODE payment for real item(s) " + ", ".join(f"#{i['item_number']}" for i in stray) +
                " - left untouched.")
        db.update_stripe_checkout(session_id, status="attention", note=note)
        log.error(note)
        return {"ok": False, "message": note, "test_mode": True}
    for item in items:
        if item["status"] == db.STATUS_LISTED:
            db.update_status(item["id"], db.STATUS_SOLD, actor_id=SYSTEM_ACTOR, note="TEST checkout paid")
    db.update_stripe_checkout(session_id, status="fulfilled", sold_marked=1,
                              note="TEST MODE payment - test item marked sold, nothing booked")
    db.release_stripe_holds(session_id)
    log.info("Test-mode checkout %s fulfilled (test item only, nothing booked)", session_id)
    return {"ok": True, "sale_id": None, "status": "fulfilled", "test_mode": True}


def _split(total: float, weights: list) -> list:
    weights = [max(w or 0, 0) for w in weights] or [1]
    if not sum(weights):
        weights = [1] * len(weights)
    cents = round(total * 100)
    out, used = [], 0
    for i, w in enumerate(weights):
        part = cents - used if i == len(weights) - 1 else round(cents * w / sum(weights))
        used += part
        out.append(part / 100)
    return out


async def _book_tax_and_fee(session_id: str, tax: float, fee, notes: list) -> bool:
    if fee is None:
        notes.append("Stripe hadn't reported its fee yet - the retry loop will book it.")
        return False
    if tax and not config.QUICKBOOKS_SALES_TAX_PAYABLE_ACCOUNT_ID:
        notes.append("QUICKBOOKS_SALES_TAX_PAYABLE_ACCOUNT_ID isn't set - the sales tax wasn't booked.")
        return False
    fee_account = config.QUICKBOOKS_STRIPE_FEES_ACCOUNT_ID or config.QUICKBOOKS_EBAY_FEES_ACCOUNT_ID
    lines = []
    if tax:
        lines.append({"account_id": config.QUICKBOOKS_SALES_TAX_PAYABLE_ACCOUNT_ID, "amount": -tax,
                      "description": f"Sales tax collected by Stripe - {session_id}"})
    if fee:
        if not fee_account:
            notes.append("No QuickBooks account for Stripe's fee (QUICKBOOKS_STRIPE_FEES_ACCOUNT_ID).")
            return False
        lines.append({"account_id": fee_account, "amount": fee, "description": f"Stripe processing fee - {session_id}"})
    if not lines:
        return True
    try:
        await quickbooks.create_balanced_journal_entry(
            lines, config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            f"Stripe website order {session_id}: sales tax and processing fee")
    except quickbooks.QuickBooksError as e:
        notes.append(f"QuickBooks didn't take the tax/fee entry ({e}) - will retry.")
        return False
    return True


async def _notify(bot, checkout: dict, session: dict, total: float, tax: float, fee, notes: list, attention=False):
    items = [db.get_item(i) for i in checkout["item_ids"]]
    names = ", ".join(f"{(db.get_pallet(i['pallet_id']) or {}).get('name', '?')} #{i['item_number']}" for i in items)
    address = _addr(session)
    where = (f"ship to {address['name']}, {address['city']}, {address['state']} (in the Pirate Ship export)"
             if address else "local pickup")
    head = "⚠️ **Website order needs attention**" if attention else "🛒 **Website order paid**"
    text = (f"{head} - {names} - ${total:.2f} (tax ${tax:.2f}"
            + (f", Stripe fee ${fee:.2f}" if fee is not None else "") + f") - {where}.")
    if notes:
        text += "\n" + "\n".join(f"• {n}" for n in notes)
    channel_id = db.get_shared_channel_id("ebay-orders")
    channel = bot.get_channel(channel_id) if (bot and channel_id) else None
    if channel is not None:
        try:
            await channel.send(text[:1900])
        except Exception:
            log.exception("Couldn't post the website order notice")
    await finance_utils.post_to_finance_audit_log(bot, text[:1900])


async def retry_pending(bot) -> int:
    """Re-runs fulfillment for paid orders that didn't finish (QuickBooks
    or Discord was down, the fee wasn't ready yet)."""
    done = 0
    for checkout in db.list_stripe_checkouts(["paid", "attention"], limit=50):
        if (checkout.get("note") or "").startswith(CONFLICT):
            continue  # sold-elsewhere conflict - needs a person
        try:
            result = await fulfill(bot, checkout["session_id"])
            done += bool(result.get("ok"))
        except Exception:
            log.exception("Retrying Stripe order %s failed", checkout["session_id"])
    return done
