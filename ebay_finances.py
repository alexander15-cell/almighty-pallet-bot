"""
eBay's real fee/shipping/payout numbers, side-by-side with what this bot
has recorded - /ebay sync-finances and /ebay net-report (cogs/ebay.py).

Why this exists: a pallet's revenue (items.sale_price) is whatever was
logged as the sale price - and a sale logged from an #ebay-orders card logs
eBay's pricingSummary.total, which includes the shipping the buyer paid.
Neither path ever subtracts eBay's own fees, eBay-bought shipping labels,
refunds, or promoted-listing ad fees. eBay already knows all of those, so
this pulls them and shows the difference - WITHOUT changing a single
existing number:

  - sync() reads two eBay APIs and writes ONLY to this feature's own mirror
    tables (ebay_fin_transactions, ebay_order_breakdowns, ebay_order_lines,
    ebay_finance_sync_log - see their CREATE TABLE comments in database.py).
    It never touches items, sales, sale_items, pallet_costs,
    finance_transactions, QuickBooks, or Discord cards.
  - build_pallet_report()/build_overview() are pure reads.

The two eBay sources:

  - Finances API getTransactions (sell.finances scope): every monetary
    transaction - SALE (with per-line fees), REFUND, SHIPPING_LABEL,
    NON_SALE_CHARGE (ad/insertion/store fees), CREDIT, DISPUTE, ... - for up
    to the last five years, in date windows of at most 36 months. Its
    `amount` is what actually hit the seller's eBay balance (bookingEntry
    CREDIT/DEBIT), so an order's net payout is simply the signed sum of
    every transaction tied to that order - no need to re-derive eBay's fee
    math (or guess at how refund fee-credits net out).
  - Fulfillment API getOrders (sell.fulfillment scope, already used by the
    order auto-sync): the per-line SKU, item price and buyer-paid shipping,
    which the Finances API doesn't itemize - up to two years back. The SKU
    (this bot's pallet-N-item-M custom label) is what links eBay's money
    back to a tracked item.

Matching (match_line_to_item) happens at report time, never stored: SKU
first, then the confirmed eBay listing ID as a fallback - and only to an
item created BEFORE the order, so a SKU like pallet-1-item-1 can never
attach to a different pallet re-created under a reused ID after a db-wipe.
"""
import json
import logging
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp

import config
import database as db
import ebay_api
import ebay_listing_rules

log = logging.getLogger(__name__)

# eBay's documented history limits (from the live API spec): getTransactions
# returns at most the last five years, with any one transactionDate filter
# spanning no more than 36 months; getOrders returns at most two years.
# A day of margin on each so a request never lands just past the edge.
_TRANSACTIONS_MAX_LOOKBACK = timedelta(days=5 * 365 - 1)
_TRANSACTIONS_WINDOW = timedelta(days=365)
_ORDERS_MAX_LOOKBACK = timedelta(days=2 * 365 - 1)

_TRANSACTIONS_PAGE_LIMIT = 1000  # getTransactions maximum
_ORDERS_PAGE_LIMIT = 200         # getOrders maximum

# Transaction types that are the sale itself, shipping labels, or
# non-sale charges are broken out separately in the report; every other
# order-linked type (REFUND, DISPUTE, CREDIT, ADJUSTMENT, ...) is grouped
# as "refunds & adjustments".
_TYPE_SALE = "SALE"
_TYPE_SHIPPING_LABEL = "SHIPPING_LABEL"
_TYPE_NON_SALE_CHARGE = "NON_SALE_CHARGE"
# Account money movement, not profit or loss on any order - excluded from
# account-level fee totals.
_TYPES_NOT_A_COST = {"TRANSFER", "WITHDRAWAL", "PURCHASE", "LOAN_REPAYMENT"}

_SCOPE_HINT = (
    " - if this is a permissions error, EBAY_REFRESH_TOKEN needs re-consenting with the "
    "sell.finances scope added (see README's 'eBay fee & payout sync' section)."
)


def _finances_base_url() -> str:
    # The Finances API lives on the apiz. host, not api. - per eBay's own spec.
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://apiz.sandbox.ebay.com/sell/finances/v1"
    return "https://apiz.ebay.com/sell/finances/v1"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _money(amount_obj) -> float:
    """eBay's Amount container ({"value": "12.34", "currency": "USD"}) as a
    float, or None if absent/unparseable."""
    if not isinstance(amount_obj, dict) or amount_obj.get("value") in (None, ""):
        return None
    try:
        return float(amount_obj["value"])
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _get(url: str, context: str) -> dict:
    access_token = await ebay_api.get_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE_ID}
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                hint = _SCOPE_HINT if resp.status in (401, 403) else ""
                raise ebay_api.EbayAPIError(f"eBay {context} failed ({resp.status}): {(body or {}).get('errors') or body}{hint}")
            return body or {}


# ----------------------------------------------------------------- parsing --

def parse_transaction(raw: dict):
    """One Finances API Transaction -> the flat shape
    database.upsert_ebay_fin_transaction stores, or None if it has no
    usable ID/type. Buyer username is deliberately not kept."""
    transaction_id = raw.get("transactionId")
    transaction_type = raw.get("transactionType")
    if not transaction_id or not transaction_type:
        return None

    line_fees = {}
    for line in raw.get("orderLineItems") or []:
        line_id = line.get("lineItemId")
        if not line_id:
            continue
        line_fees[line_id] = round(sum(_money(f.get("amount")) or 0.0 for f in line.get("marketplaceFees") or []), 2)

    references = [
        {"type": r.get("referenceType"), "id": str(r.get("referenceId"))}
        for r in raw.get("references") or []
        if r.get("referenceType") and r.get("referenceId") is not None
    ]

    tax = _money(raw.get("ebayCollectedTaxAmount"))
    if tax is None:
        tax = _money(raw.get("eBayCollectedTaxAmount"))

    return {
        "transaction_id": str(transaction_id),
        "transaction_type": transaction_type,
        "transaction_date": raw.get("transactionDate"),
        "transaction_status": raw.get("transactionStatus"),
        "booking_entry": raw.get("bookingEntry"),
        "amount": _money(raw.get("amount")),
        "total_fee_amount": _money(raw.get("totalFeeAmount")),
        "total_fee_basis_amount": _money(raw.get("totalFeeBasisAmount")),
        "ebay_collected_tax": tax,
        "order_id": raw.get("orderId"),
        "fee_type": raw.get("feeType"),
        "transaction_memo": raw.get("transactionMemo"),
        "references_json": json.dumps(references) if references else None,
        "line_fees_json": json.dumps(line_fees) if line_fees else None,
        "payout_id": raw.get("payoutId"),
    }


def parse_order_breakdown(raw: dict):
    """One Fulfillment API Order -> the shape database.
    upsert_ebay_order_breakdown stores, or None if it has no order ID.
    Money fields only - no buyer/shipping-address data is kept."""
    order_id = raw.get("orderId")
    if not order_id:
        return None
    pricing = raw.get("pricingSummary") or {}

    def positive(amount_obj):
        value = _money(amount_obj)
        return abs(value) if value is not None else None

    lines = []
    for li in raw.get("lineItems") or []:
        line_id = li.get("lineItemId")
        if not line_id:
            continue
        lines.append({
            "line_item_id": str(line_id),
            "sku": li.get("sku"),
            "legacy_item_id": str(li["legacyItemId"]) if li.get("legacyItemId") else None,
            "title": li.get("title"),
            "quantity": li.get("quantity") or 1,
            "line_item_cost": _money(li.get("lineItemCost")),
            "shipping_cost": _money((li.get("deliveryCost") or {}).get("shippingCost")),
        })

    return {
        "order_id": order_id,
        "order_date": raw.get("creationDate"),
        "price_subtotal": _money(pricing.get("priceSubtotal")),
        "price_discount": positive(pricing.get("priceDiscount")),
        "delivery_cost": _money(pricing.get("deliveryCost")),
        "delivery_discount": positive(pricing.get("deliveryDiscount")),
        "adjustment": _money(pricing.get("adjustment")),
        "order_total": _money(pricing.get("total")),
        "total_marketplace_fee": _money(raw.get("totalMarketplaceFee")),
        "total_fee_basis_amount": _money(raw.get("totalFeeBasisAmount")),
        "cancel_state": (raw.get("cancelStatus") or {}).get("cancelState"),
        "lines": lines,
    }


# -------------------------------------------------------------------- sync --

async def fetch_transactions(since: datetime, until: datetime) -> list:
    """Every Finances API transaction between since and until (raw dicts),
    split into windows no longer than eBay's 36-month filter limit and
    paginated within each."""
    results = []
    window_start = since
    while window_start < until:
        window_end = min(window_start + _TRANSACTIONS_WINDOW, until)
        filter_value = f"transactionDate:[{_iso(window_start)}..{_iso(window_end)}]"
        offset = 0
        while True:
            url = (
                f"{_finances_base_url()}/transaction?filter={urllib.parse.quote(filter_value)}"
                f"&sort=transactionDate&limit={_TRANSACTIONS_PAGE_LIMIT}&offset={offset}"
            )
            body = await _get(url, "getTransactions")
            page = body.get("transactions") or []
            results.extend(page)
            offset += len(page)
            if not page or offset >= (body.get("total") or 0):
                break
        window_start = window_end
    return results


async def refresh_order_transactions(order_id: str) -> int:
    """Re-pulls just this order's Finances transactions (orderId filter)
    into the mirror table - used right before a "Confirm & log fees" click
    books anything, so it acts on eBay's latest numbers rather than the
    last background sync. Returns how many were refreshed; raises
    ebay_api.EbayAPIError on failure."""
    filter_value = urllib.parse.quote(f"orderId:{{{order_id}}}")
    count, offset = 0, 0
    while True:
        url = (f"{_finances_base_url()}/transaction?filter={filter_value}"
               f"&limit={_TRANSACTIONS_PAGE_LIMIT}&offset={offset}")
        body = await _get(url, "getTransactions (order)")
        page = body.get("transactions") or []
        for raw in page:
            txn = parse_transaction(raw)
            if txn:
                db.upsert_ebay_fin_transaction(txn)
                count += 1
        offset += len(page)
        if not page or offset >= (body.get("total") or 0):
            return count


def parse_payout(raw: dict):
    """One Finances API Payout -> the flat shape database.upsert_ebay_payout
    stores, or None without an ID. Only the bank's nickname and last four
    digits are kept."""
    payout_id = raw.get("payoutId")
    if not payout_id:
        return None
    instrument = raw.get("payoutInstrument") or {}
    return {
        "payout_id": str(payout_id),
        "payout_status": raw.get("payoutStatus"),
        "amount": _money(raw.get("amount")),
        "payout_date": raw.get("payoutDate") or raw.get("lastAttemptedPayoutDate"),
        "transaction_count": raw.get("transactionCount"),
        "instrument_nickname": instrument.get("nickname"),
        "instrument_last4": instrument.get("accountLastFourDigits"),
    }


async def fetch_payouts(since: datetime, until: datetime) -> list:
    """Every Finances API payout between since and until (raw dicts), in
    the same <=36-month windows as fetch_transactions, paginated."""
    results = []
    window_start = since
    while window_start < until:
        window_end = min(window_start + _TRANSACTIONS_WINDOW, until)
        filter_value = f"payoutDate:[{_iso(window_start)}..{_iso(window_end)}]"
        offset = 0
        while True:
            url = (f"{_finances_base_url()}/payout?filter={urllib.parse.quote(filter_value)}"
                   f"&sort=payoutDate&limit=200&offset={offset}")
            body = await _get(url, "getPayouts")
            page = body.get("payouts") or []
            results.extend(page)
            offset += len(page)
            if not page or offset >= (body.get("total") or 0):
                break
        window_start = window_end
    return results


async def fetch_orders(since: datetime, until: datetime) -> list:
    """Every Fulfillment API order created between since and until (raw
    dicts), paginated."""
    results = []
    filter_value = f"creationdate:[{_iso(since)}..{_iso(until)}]"
    offset = 0
    while True:
        url = (
            f"{ebay_api._fulfillment_base_url()}/order?filter={urllib.parse.quote(filter_value)}"
            f"&limit={_ORDERS_PAGE_LIMIT}&offset={offset}"
        )
        body = await _get(url, "getOrders")
        page = body.get("orders") or []
        results.extend(page)
        offset += len(page)
        if not page or offset >= (body.get("total") or 0):
            break
    return results


async def sync(days_back: int, actor_id: int = None, now: datetime = None) -> dict:
    """
    Pulls eBay's transactions (up to 5 years) and order breakdowns (up to 2
    years) for the last `days_back` days into the mirror tables. Safe to
    re-run any time - every row is keyed on eBay's own IDs, so a re-sync
    refreshes rows in place instead of duplicating them. Writes nothing
    outside the mirror tables. Raises ebay_api.EbayAPIError on failure
    (after logging the failed attempt in ebay_finance_sync_log); rows
    already stored before the failure are kept - they're still accurate.
    """
    now = now or datetime.now(timezone.utc)
    requested = timedelta(days=max(1, days_back))
    transactions_from = now - min(requested, _TRANSACTIONS_MAX_LOOKBACK)
    orders_from = now - min(requested, _ORDERS_MAX_LOOKBACK)
    sync_id = db.start_ebay_finance_sync(_iso(transactions_from), _iso(orders_from), actor_id)

    # Sets, not counters: adjacent date windows share a boundary instant, so
    # the same transaction can come back twice - the upsert already
    # de-duplicates storage, this keeps the reported count honest too.
    seen_orders, seen_txns = set(), set()
    try:
        for raw in await fetch_orders(orders_from, now):
            order = parse_order_breakdown(raw)
            if order:
                db.upsert_ebay_order_breakdown(order)
                seen_orders.add(order["order_id"])
        for raw in await fetch_transactions(transactions_from, now):
            txn = parse_transaction(raw)
            if txn:
                db.upsert_ebay_fin_transaction(txn)
                seen_txns.add((txn["transaction_id"], txn["transaction_type"]))
        for raw in await fetch_payouts(transactions_from, now):
            payout = parse_payout(raw)
            if payout:
                db.upsert_ebay_payout(payout)
    except Exception as e:
        db.finish_ebay_finance_sync(sync_id, len(seen_txns), len(seen_orders), error=str(e)[:500])
        raise
    db.finish_ebay_finance_sync(sync_id, len(seen_txns), len(seen_orders))
    return {
        "transactions": len(seen_txns),
        "orders": len(seen_orders),
        "transactions_from": transactions_from,
        "orders_from": orders_from,
    }


# ------------------------------------------------------------------ report --

def match_line_to_item(line: dict):
    """The tracked item an eBay order line belongs to, or None: SKU first
    (this bot's pallet-N-item-M custom label), then the confirmed eBay
    listing ID. Only an item created before the order counts - see this
    module's docstring for why."""
    order_dt = _parse_dt(line.get("order_date"))
    candidates = []
    parsed = ebay_listing_rules.parse_custom_label(line.get("sku") or "")
    if parsed:
        candidates.append(db.get_item_by_pallet_and_number(*parsed))
    candidates.append(db.get_item_by_ebay_listing_id(line.get("legacy_item_id")))
    for item in candidates:
        if not item or item.get("status") == db.STATUS_DELETED:
            continue
        created = _parse_dt(item.get("created_at"))
        if order_dt and created and created > order_dt:
            continue
        return item
    return None


def _signed(txn: dict) -> float:
    """A transaction's effect on the seller's eBay balance: + for CREDIT,
    - for DEBIT."""
    amount = txn.get("amount") or 0.0
    return -amount if txn.get("booking_entry") == "DEBIT" else amount


def _shares(values: list) -> list:
    """Each value's proportion of the total (equal split if the total is 0)."""
    total = sum(v or 0.0 for v in values)
    if total <= 0:
        return [1.0 / len(values)] * len(values) if values else []
    return [(v or 0.0) / total for v in values]


def build_line_rows() -> dict:
    """
    Every synced eBay order line with eBay's money allocated to it, plus
    whatever couldn't be tied to any order line. Returns:
      {"lines": [...], "account_level": {fee_type: amount}, ...}
    Each line row: item (or None), order_id, order_date, item_price (after
    discounts/adjustments), buyer_shipping, ebay_fees, refunds,
    shipping_labels, ad_fees, net_payout, net_is_estimate.

    Allocation inside a multi-line order is proportional to each line's
    item price. net_payout is eBay's own signed sum of every transaction
    tied to the order (so it's exact at the order level); when no
    Finances data covers the order yet, it falls back to an estimate of
    order total minus eBay's reported marketplace fee, flagged
    net_is_estimate.
    """
    lines = db.get_all_ebay_order_lines()
    transactions = db.get_all_ebay_fin_transactions()

    txns_by_order = defaultdict(list)
    ad_fees_by_listing = defaultdict(float)
    account_level = defaultdict(float)
    for txn in transactions:
        if txn.get("order_id"):
            txns_by_order[txn["order_id"]].append(txn)
            continue
        refs = txn.get("references") or []
        order_ref = next((r["id"] for r in refs if r["type"] == "ORDER_ID"), None)
        if order_ref:
            txns_by_order[order_ref].append(txn)
            continue
        if txn["transaction_type"] in _TYPES_NOT_A_COST:
            continue
        listing_ref = next((r["id"] for r in refs if r["type"] == "ITEM_ID"), None)
        if listing_ref and txn["transaction_type"] == _TYPE_NON_SALE_CHARGE:
            ad_fees_by_listing[listing_ref] += -_signed(txn)
            continue
        label = txn.get("fee_type") or txn["transaction_type"]
        account_level[label] += -_signed(txn)

    lines_by_order = defaultdict(list)
    for line in lines:
        lines_by_order[line["ebay_order_id"]].append(line)

    # Ad/listing fees referenced only by listing ID, split across that
    # listing's synced order lines (normally exactly one, since this bot
    # always lists quantity 1).
    lines_per_listing = defaultdict(int)
    for line in lines:
        if line.get("legacy_item_id"):
            lines_per_listing[line["legacy_item_id"]] += 1
    synced_listings = set(lines_per_listing)
    for listing_id, amount in ad_fees_by_listing.items():
        if listing_id not in synced_listings:
            account_level["Listing fees (no synced order)"] += amount

    rows = []
    covered_orders = set()
    for order_id, order_lines in lines_by_order.items():
        covered_orders.add(order_id)
        first = order_lines[0]
        shares = _shares([l.get("line_item_cost") for l in order_lines])
        order_txns = txns_by_order.get(order_id, [])
        sale_txns = [t for t in order_txns if t["transaction_type"] == _TYPE_SALE]

        line_fee_lookup = {}
        sale_fee_total = 0.0
        for t in sale_txns:
            line_fee_lookup.update(t.get("line_fees") or {})
            sale_fee_total += t.get("total_fee_amount") or 0.0
        if not sale_txns:
            sale_fee_total = first.get("total_marketplace_fee") or 0.0
        unassigned_fees = sale_fee_total - sum(line_fee_lookup.get(l["line_item_id"], 0.0) for l in order_lines)

        labels = sum(-_signed(t) for t in order_txns if t["transaction_type"] == _TYPE_SHIPPING_LABEL)
        order_ad_fees = sum(-_signed(t) for t in order_txns if t["transaction_type"] == _TYPE_NON_SALE_CHARGE)
        refunds = sum(
            -_signed(t) for t in order_txns
            if t["transaction_type"] not in (_TYPE_SALE, _TYPE_SHIPPING_LABEL, _TYPE_NON_SALE_CHARGE)
        )
        order_net = sum(_signed(t) for t in order_txns) if sale_txns else None
        has_line_shipping = any(l.get("shipping_cost") is not None for l in order_lines)

        for line, share in zip(order_lines, shares):
            item_price = (line.get("line_item_cost") or 0.0) \
                - (first.get("price_discount") or 0.0) * share \
                + (first.get("adjustment") or 0.0) * share
            if has_line_shipping:
                shipping = line.get("shipping_cost") or 0.0
            else:
                shipping = (first.get("delivery_cost") or 0.0) * share
            shipping -= (first.get("delivery_discount") or 0.0) * share

            listing_ad_fees = 0.0
            if line.get("legacy_item_id") in ad_fees_by_listing:
                listing_ad_fees = ad_fees_by_listing[line["legacy_item_id"]] / lines_per_listing[line["legacy_item_id"]]
            ad_fees = order_ad_fees * share + listing_ad_fees
            fees = line_fee_lookup.get(line["line_item_id"], 0.0) + unassigned_fees * share

            if order_net is not None:
                net = order_net * share - listing_ad_fees
                estimate = False
            else:
                gross = first["order_total"] * share if first.get("order_total") is not None else item_price + shipping
                net = gross - fees - (refunds + labels) * share - ad_fees
                estimate = True

            rows.append({
                "item": match_line_to_item(line),
                "order_id": order_id,
                "order_date": line.get("order_date"),
                "sku": line.get("sku"),
                "title": line.get("title"),
                "cancelled": (first.get("cancel_state") or "") == "CANCELED",
                "item_price": round(item_price, 2),
                "buyer_shipping": round(shipping, 2),
                "ebay_fees": round(fees, 2),
                "refunds": round(refunds * share, 2),
                "shipping_labels": round(labels * share, 2),
                "ad_fees": round(ad_fees, 2),
                "net_payout": round(net, 2),
                "net_is_estimate": estimate,
            })

    # Order-linked transactions for an order with no synced breakdown (older
    # than getOrders' two-year window, or not synced yet) - can't be tied to
    # an item without the order's SKUs, so they're reported, never guessed.
    orphan_order_net = sum(
        _signed(t) for order_id, txns in txns_by_order.items() if order_id not in covered_orders for t in txns
    )
    orphan_orders = sum(1 for order_id in txns_by_order if order_id not in covered_orders)

    return {
        "lines": rows,
        "account_level": {k: round(v, 2) for k, v in account_level.items() if round(v, 2)},
        "orders_without_breakdown": orphan_orders,
        "orders_without_breakdown_net": round(orphan_order_net, 2),
    }


_SUM_FIELDS = ("item_price", "buyer_shipping", "ebay_fees", "refunds", "shipping_labels", "ad_fees", "net_payout")


def _empty_totals() -> dict:
    return {f: 0.0 for f in _SUM_FIELDS}


def summarize_items(line_rows: list) -> dict:
    """Groups line rows by matched item -> {item_id: {"item", totals...,
    "recorded", "orders", "estimate"}}. Unmatched lines are skipped here
    (the report counts them separately)."""
    by_item = {}
    for row in line_rows:
        item = row["item"]
        if not item:
            continue
        entry = by_item.setdefault(item["id"], {"item": item, "orders": [], "estimate": False, **_empty_totals()})
        for f in _SUM_FIELDS:
            entry[f] += row[f]
        entry["orders"].append(row["order_id"])
        entry["estimate"] = entry["estimate"] or row["net_is_estimate"]
    for entry in by_item.values():
        entry["recorded"] = entry["item"].get("sale_price")
        for f in _SUM_FIELDS:
            entry[f] = round(entry[f], 2)
    return by_item


def build_pallet_report(pallet_id: int, line_data: dict = None) -> dict:
    """
    One pallet's side-by-side view - recorded numbers exactly as the
    pinned card shows them, next to what eBay says. Pure read. Keys:
      matched: per-item rows (see summarize_items)
      totals: eBay sums across matched items, plus recorded_for_matched
      missing: items recorded as eBay sales with no matching eBay order
      shipping_in_revenue: matched items whose recorded price equals item
        price + buyer shipping (i.e. shipping was counted as revenue)
      financials: database.get_pallet_financials (unchanged)
      profit_with_ebay_net: the card's profit, with each matched item's
        recorded price swapped for eBay's net payout - everything else
        (cost, non-eBay sales, manual refunds/expenses) left as-is
      manual_entries_on_matched: count of /finance refund/expense rows
        tied to a matched item - those may already be counted in eBay's
        numbers, so they're flagged rather than silently netted twice
    """
    line_data = line_data or build_line_rows()
    by_item = {k: v for k, v in summarize_items(line_data["lines"]).items() if v["item"]["pallet_id"] == pallet_id}

    totals = _empty_totals()
    recorded_for_matched = 0.0
    shipping_in_revenue = []
    for entry in by_item.values():
        for f in _SUM_FIELDS:
            totals[f] += entry[f]
        recorded = entry["recorded"]
        if recorded is not None:
            recorded_for_matched += recorded
            if entry["buyer_shipping"] > 0 and abs(recorded - (entry["item_price"] + entry["buyer_shipping"])) < 0.02:
                shipping_in_revenue.append(entry)
    totals = {f: round(v, 2) for f, v in totals.items()}

    matched_ids = set(by_item)
    missing = [i for i in db.get_ebay_platform_items_with_sale_price(pallet_id) if i["id"] not in matched_ids]

    financials = db.get_pallet_financials(pallet_id)
    # eBay fees already booked onto this pallet's cost by "Confirm & log
    # fees" (ebay_fee_expenses.py, pallet_costs source 'ebay') are already
    # inside eBay's net payout - added back so they aren't subtracted twice.
    booked_ebay_costs = db.sum_pallet_costs_from_source(pallet_id, "ebay")
    profit_with_ebay_net = None
    if financials["profit_so_far"] is not None:
        profit_with_ebay_net = round(
            financials["profit_so_far"] + booked_ebay_costs - recorded_for_matched + totals["net_payout"], 2
        )

    manual_entries = db.count_item_refunds_and_expenses(list(matched_ids))

    return {
        "pallet": financials["pallet"],
        "matched": sorted(by_item.values(), key=lambda e: e["item"]["item_number"]),
        "totals": totals,
        "recorded_for_matched": round(recorded_for_matched, 2),
        "missing": missing,
        "shipping_in_revenue": shipping_in_revenue,
        "financials": financials,
        "profit_with_ebay_net": profit_with_ebay_net,
        "manual_entries_on_matched": manual_entries,
        "any_estimates": any(e["estimate"] for e in by_item.values()),
    }


def build_overview() -> dict:
    """Every pallet with matched eBay data, plus the business-wide figures
    that don't belong to any one item: unmatched eBay order lines and
    account-level charges (store subscription, insertion fees, ad fees not
    tied to a synced order, ...). Pure read."""
    line_data = build_line_rows()
    pallet_ids = sorted({row["item"]["pallet_id"] for row in line_data["lines"] if row["item"]})
    unmatched = [row for row in line_data["lines"] if not row["item"]]
    return {
        "pallets": [build_pallet_report(pid, line_data) for pid in pallet_ids],
        "unmatched_lines": unmatched,
        "unmatched_net": round(sum(r["net_payout"] for r in unmatched), 2),
        "account_level": line_data["account_level"],
        "orders_without_breakdown": line_data["orders_without_breakdown"],
        "orders_without_breakdown_net": line_data["orders_without_breakdown_net"],
        "last_sync": db.get_last_successful_ebay_finance_sync(),
    }
