"""
eBay fee expenses - book what eBay keeps from each sale into QuickBooks and
onto the pallet's cost, so recorded profit matches what eBay actually pays.

The gap this closes: an eBay sale's Sales Receipt ("Confirm & log sale",
cogs/ebay.py) books the buyer's full order total into Undeposited Funds.
eBay then deposits LESS - it keeps its selling fees, the cost of any
shipping label bought through eBay, and promoted-listing ad fees. Nothing
recorded those, so Undeposited Funds never matched real payouts and every
pallet's profit was overstated.

For each eBay order, once its sale is logged, a "💸 eBay fees" card in
#ebay-orders shows every not-yet-booked eBay charge on it (from the Finances
API mirror that /ebay sync-finances and the hourly background sync fill).
Clicking "Confirm & log fees" (Finance Management):

  1. re-pulls that order's charges from eBay, so it acts on current data,
  2. posts ONE QuickBooks Journal Entry - debit eBay fees / shipping / ads
     expense accounts, credit Undeposited Funds (quickbooks.
     create_balanced_journal_entry) - so Undeposited Funds is left holding
     exactly the payout eBay sends,
  3. records each eBay charge in the ebay_fee_postings ledger (keyed on
     eBay's own transaction ID - the same charge can never be booked
     twice) and adds the amounts to each item's pallet cost basis
     (pallet_costs, source 'ebay'), split by item price across the order.

Nothing is sent to QuickBooks without that click. A charge that shows up
after the card was confirmed (a label bought later, a voided label credited
back) gets its own follow-up card. Refunds/returns are NOT handled here -
they change the sale itself (/finance refund) rather than being a fee.

Which charges count, from eBay's transaction types:
  - SALE            -> its totalFeeAmount (final value fee, per-order fee,
                       international fee, ...) as "fees"
  - SHIPPING_LABEL  -> "shipping" (a voided label's credit books negative)
  - NON_SALE_CHARGE -> "ads" for promoted-listing fees (fee type containing
                       AD), otherwise "fees" (insertion/listing upgrades) -
                       when tied to this order, or to one of its listings
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import config
import database as db
import ebay_api
import ebay_finances
import quickbooks

log = logging.getLogger(__name__)

CATEGORY_LABELS = {"fees": "eBay fees", "shipping": "eBay shipping labels", "ads": "eBay ad/listing fees"}
# pallet_costs.cost_type for each category - 'shipping' is the existing
# type, so eBay labels sit alongside Pirate Ship shipping in pallet-summary.
COST_TYPES = {"fees": "ebay_fees", "shipping": "shipping", "ads": "ebay_ads"}

# One click at a time per order, so a double-click can't post two Journal
# Entries before the ledger write lands.
_order_locks = defaultdict(asyncio.Lock)


def _cost(txn: dict) -> float:
    """What a transaction cost the seller: + for a DEBIT, - for a CREDIT."""
    amount = txn.get("amount") or 0.0
    return amount if txn.get("booking_entry") == "DEBIT" else -amount


def _charge_for(txn: dict, order_id: str):
    """A transaction as a bookable charge dict, or None if it isn't one."""
    ttype = txn["transaction_type"]
    if ttype == "SALE":
        amount, category = txn.get("total_fee_amount") or 0.0, "fees"
    elif ttype == "SHIPPING_LABEL":
        amount, category = _cost(txn), "shipping"
    elif ttype == "NON_SALE_CHARGE":
        amount = _cost(txn)
        category = "ads" if "AD" in (txn.get("fee_type") or "").upper().split("_") else "fees"
    else:
        return None
    if round(amount, 2) == 0:
        return None
    listing = None
    if txn.get("order_id") != order_id and not any(
        r["type"] == "ORDER_ID" and r["id"] == order_id for r in txn.get("references") or []
    ):
        listing = next((r["id"] for r in txn.get("references") or [] if r["type"] == "ITEM_ID"), None)
    return {
        "transaction_id": txn["transaction_id"],
        "transaction_type": ttype,
        "category": category,
        "amount": round(amount, 2),
        "date": (txn.get("transaction_date") or "")[:10],
        "listing_id": listing,  # None = applies to the whole order
        "fee_type": txn.get("fee_type"),
    }


def order_fee_breakdown(order_id: str) -> dict:
    """
    Everything needed to show or book one order's unbooked eBay charges.
    Pure read. Keys: order_id, lines (order lines, each with its matched
    'item' or None), items (matched items), charges (unbooked), totals
    ({fees, shipping, ads, total}), sale_logged (every matched item's sale
    has been logged - required before booking).
    """
    lines = db.get_ebay_order_lines_for_order(order_id)
    for line in lines:
        line["item"] = ebay_finances.match_line_to_item(line)
    items = [line["item"] for line in lines if line["item"]]
    listing_ids = [line["legacy_item_id"] for line in lines if line.get("legacy_item_id")]

    posted = db.get_posted_ebay_fee_keys()
    charges = []
    for txn in db.get_ebay_fee_source_transactions(order_id, listing_ids):
        if (txn["transaction_id"], txn["transaction_type"]) in posted:
            continue
        charge = _charge_for(txn, order_id)
        if charge:
            charges.append(charge)

    totals = {c: round(sum(ch["amount"] for ch in charges if ch["category"] == c), 2) for c in CATEGORY_LABELS}
    totals["total"] = round(sum(totals.values()), 2)
    return {
        "order_id": order_id,
        "lines": lines,
        "items": items,
        "charges": charges,
        "totals": totals,
        "sale_logged": bool(items) and all(db.get_item_sale(i["id"]) for i in items),
    }


def allocate_to_items(breakdown: dict) -> list:
    """
    pallet_costs rows for a breakdown's charges: an order-level charge is
    split across the order's lines by item price (a line that isn't a
    tracked item keeps its share off every pallet - it's still a real
    QuickBooks expense, just not this business's pallet); a listing-level
    charge goes wholly to that listing's item. Rows are per item and
    category; amounts are rounded with any rounding remainder on the
    largest share, so they sum exactly to what's booked.
    """
    lines = breakdown["lines"]
    prices = [line.get("line_item_cost") or 0.0 for line in lines]
    total_price = sum(prices)
    shares = [p / total_price for p in prices] if total_price > 0 else [1 / len(lines)] * len(lines) if lines else []

    by_key = defaultdict(float)  # (item_id, pallet_id, category) -> amount
    for charge in breakdown["charges"]:
        if charge["listing_id"]:
            line = next((l for l in lines if l.get("legacy_item_id") == charge["listing_id"]), None)
            if line and line["item"]:
                by_key[(line["item"]["id"], line["item"]["pallet_id"], charge["category"])] += charge["amount"]
            continue
        portions = [round(charge["amount"] * s, 2) for s in shares]
        if portions:
            biggest = max(range(len(portions)), key=lambda i: shares[i])
            portions[biggest] = round(portions[biggest] + charge["amount"] - sum(portions), 2)
        for line, portion in zip(lines, portions):
            if line["item"] and portion:
                by_key[(line["item"]["id"], line["item"]["pallet_id"], charge["category"])] += portion

    return [
        {"item_id": item_id, "pallet_id": pallet_id, "cost_type": COST_TYPES[category], "amount": round(amount, 2),
         "description": f"{CATEGORY_LABELS[category]} - eBay order {breakdown['order_id']}"}
        for (item_id, pallet_id, category), amount in by_key.items() if round(amount, 2)
    ]


def account_for(category: str) -> str:
    if category == "shipping" and config.QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID:
        return config.QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID
    if category == "ads" and config.QUICKBOOKS_EBAY_ADS_ACCOUNT_ID:
        return config.QUICKBOOKS_EBAY_ADS_ACCOUNT_ID
    return config.QUICKBOOKS_EBAY_FEES_ACCOUNT_ID


def setup_problem() -> str:
    """Why fees can't be booked right now, or "" if they can."""
    if not config.QUICKBOOKS_ENABLED or not quickbooks.is_connected():
        return "QuickBooks isn't connected - run `/finance connect-quickbooks` first."
    if not config.QUICKBOOKS_EBAY_FEES_ACCOUNT_ID:
        return (
            "`QUICKBOOKS_EBAY_FEES_ACCOUNT_ID` isn't set in `.env`, so there's no QuickBooks expense account to "
            "book eBay fees to. Find your eBay fees expense account's ID with `/finance list-accounts "
            "account_type:All account types`, add it to `.env`, and restart the bot."
        )
    return ""


def is_card_due(breakdown: dict, now: datetime) -> bool:
    """A card is worth posting once the sale is logged, there's something
    new to book, and either every item has shipped (labels are bought by
    then) or EBAY_FEE_CARD_DELAY_DAYS have passed since the order."""
    if not breakdown["sale_logged"] or not breakdown["charges"]:
        return False
    if all(i["status"] == db.STATUS_SHIPPED for i in breakdown["items"]):
        return True
    order_dt = ebay_finances._parse_dt(breakdown["lines"][0].get("order_date")) if breakdown["lines"] else None
    return bool(order_dt and now - order_dt >= timedelta(days=config.EBAY_FEE_CARD_DELAY_DAYS))


def orders_needing_cards(since_date: str, now: datetime = None) -> list:
    """Breakdowns for orders placed on/after since_date whose card is due
    and that have at least one charge not already on a posted card."""
    now = now or datetime.now(timezone.utc)
    carded = db.get_carded_ebay_fee_keys()
    due = []
    for order_id in db.get_ebay_order_ids_since(since_date):
        breakdown = order_fee_breakdown(order_id)
        if not is_card_due(breakdown, now):
            continue
        if any((c["transaction_id"], c["transaction_type"]) not in carded for c in breakdown["charges"]):
            due.append(breakdown)
    return due


async def confirm_order_fees(order_id: str, actor_id: int) -> dict:
    """
    The "Confirm & log fees" click. Returns {'ok': True, 'breakdown',
    'je_doc', 'pallet_ids'} or {'ok': False, 'message'} - never raises for
    an expected problem, and never records anything unless the QuickBooks
    Journal Entry was created.
    """
    problem = setup_problem()
    if problem:
        return {"ok": False, "message": problem}

    async with _order_locks[order_id]:
        try:
            await ebay_finances.refresh_order_transactions(order_id)
        except ebay_api.EbayAPIError as e:
            log.warning("Couldn't refresh eBay charges for order %s before booking (%s) - using last sync", order_id, e)

        breakdown = order_fee_breakdown(order_id)
        if not breakdown["charges"]:
            return {"ok": False, "message": "Every eBay charge on this order is already logged - nothing new to book."}
        if not breakdown["sale_logged"]:
            return {"ok": False, "message": "Log this order's sale first (its **Confirm & log sale** card), then the fees."}

        items_text = ", ".join(
            f"{(db.get_pallet(i['pallet_id']) or {}).get('name', i['pallet_id'])}#{i['item_number']}"
            for i in breakdown["items"]
        )
        memo = f"eBay fees, order {order_id} ({items_text})"
        expense_lines = [
            {"account_id": account_for(cat), "amount": breakdown["totals"][cat],
             "description": f"{CATEGORY_LABELS[cat]} - eBay order {order_id}"}
            for cat in CATEGORY_LABELS if breakdown["totals"][cat]
        ]
        date = max((c["date"] for c in breakdown["charges"] if c["date"]), default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        try:
            je = await quickbooks.create_balanced_journal_entry(
                expense_lines, config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, date, memo
            )
        except quickbooks.QuickBooksError as e:
            return {"ok": False, "message": f"QuickBooks didn't accept the Journal Entry - nothing was recorded: {e}"}

        try:
            db.record_ebay_fee_posting(order_id, breakdown["charges"], je["id"], actor_id, allocate_to_items(breakdown))
        except Exception as e:
            # The Journal Entry exists in QuickBooks but the ledger didn't
            # save - say so plainly so nobody clicks again and books it twice.
            log.exception("eBay fee Journal Entry %s posted but the ledger write failed", je.get("id"))
            return {"ok": False, "message": (
                f"⚠️ QuickBooks Journal Entry `{je.get('doc_number') or je.get('id')}` **was created**, but saving "
                f"the record here failed ({e}). **Don't click again** - that would book these fees twice. "
                "Ask a Pallet Admin to check the bot's log."
            )}
        return {
            "ok": True,
            "breakdown": breakdown,
            "je_doc": je.get("doc_number") or je.get("id"),
            "pallet_ids": sorted({i["pallet_id"] for i in breakdown["items"]}),
        }
