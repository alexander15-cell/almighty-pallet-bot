"""
Website orders -> Shippo (SHIPPO_ORDERS=true).

When a shipped website / payment-link order is paid, the bot creates it as
an order in the Shippo dashboard: buyer's address, each item with its SKU,
weight and box size, and the service the buyer was quoted. A person opens
Shippo, checks the box, and buys the label there. Those items are left out
of the Pirate Ship export so nobody labels them twice.

Every few minutes the bot asks Shippo about open orders (sync_labels). Once
a label has been bought it:
  - marks each item Shipped (the same shared action as the Discord button),
  - keeps the tracking number / link,
  - records the real label cost against the item's pallet (pallet_costs,
    source 'shippo' - pallet profit only; nothing is posted to QuickBooks
    here: the charge reaches the books the same way other label/card
    charges do),
  - posts a notice in #ebay-orders.

Pickup orders, eBay orders and test-mode payments never go to Shippo.
"""
import logging
from datetime import datetime, timezone

import config
import database as db
import ebay_listing_rules
import finance_utils
import shipping
import shippo_api

log = logging.getLogger(__name__)
SYSTEM_ACTOR = 0
MAX_ATTEMPTS = 5


def enabled() -> bool:
    return config.SHIPPO_ORDERS and bool(config.SHIPPO_API_TOKEN)


def _money(dollars) -> str:
    return f"{float(dollars or 0):.2f}"


def build_order(checkout: dict, session: dict, items: list, address: dict) -> dict:
    lines, total_weight = [], 0.0
    for item in items:
        listing = db.get_ebay_listing_data(item["id"]) or {}
        parcel, source = shipping.parcel_for(item, listing)
        total_weight += parcel["weight"]
        title = (listing.get("ebay_title") or item.get("ai_title") or f"Item #{item['item_number']}")[:150]
        box = (f"box {parcel['length']:g}x{parcel['width']:g}x{parcel['height']:g} in, {parcel['weight']:g} lb"
               + (" - DEFAULT BOX, measure it" if source == "default" else ""))
        lines.append({"title": f"{title} [{box}]", "sku": ebay_listing_rules.custom_label(item), "quantity": 1,
                      "total_price": _money(item.get("sale_price") or listing.get("price")), "currency": "USD",
                      "weight": f"{parcel['weight']:.2f}", "weight_unit": "lb"})
    totals = session.get("total_details") or {}
    quoted = db.get_checkout_shipping(checkout["session_id"]) or {}
    quote_row = db.get_shipping_quote(quoted["quote_id"]) if quoted.get("quote_id") else None
    services = sorted({f"{d.get('carrier', '')} {d.get('service', '')}".strip()
                       for d in (quote_row or {}).get("detail", [])} - {""})
    email = (session.get("customer_details") or {}).get("email")
    to_address = {"name": address["name"], "street1": address["line1"], "street2": address.get("line2") or "",
                  "city": address["city"], "state": address["state"], "zip": address["postal_code"],
                  "country": address.get("country") or "US"}
    if email:
        to_address["email"] = email
    return {
        "order_number": f"4G1P-{checkout['session_id'][-12:]}",
        "order_status": "PAID",
        "placed_at": datetime.now(timezone.utc).isoformat(),
        "to_address": to_address,
        "from_address": shippo_api.ship_from(),
        "line_items": lines,
        "shipping_cost": _money((totals.get("amount_shipping") or 0) / 100),
        "shipping_cost_currency": "USD",
        "shipping_method": ", ".join(services) or config.SHIP_LABEL,
        "subtotal_price": _money((session.get("amount_subtotal") or 0) / 100),
        "total_tax": _money((totals.get("amount_tax") or 0) / 100),
        "total_price": _money((session.get("amount_total") or 0) / 100),
        "currency": "USD",
        "weight": f"{total_weight:.2f}",
        "weight_unit": "lb",
    }


async def send_for_checkout(checkout: dict, session: dict, items: list, address: dict) -> str:
    """Creates the Shippo order for a paid shipped checkout (once). Returns a
    note for the order notice ('' when there's nothing to say)."""
    session_id = checkout["session_id"]
    existing = db.get_shippo_order(session_id)
    if existing and existing["status"] != "failed":
        return ""
    if existing and existing["attempts"] >= MAX_ATTEMPTS:
        return ""
    if not address:
        return "No shipping address from Stripe - make this label by hand."
    try:
        order = await shippo_api.create_order(build_order(checkout, session, items, address))
    except shippo_api.ShippoError as e:
        db.save_shippo_order(session_id, None, "failed", [i["id"] for i in items], error=str(e)[:300])
        log.warning("Couldn't send order %s to Shippo: %s", session_id, e)
        return (f"Couldn't send this order to Shippo ({str(e)[:150]}) - the bot will retry; "
                "or make the label by hand.")
    db.save_shippo_order(session_id, order.get("object_id"), "open", [i["id"] for i in items])
    return "Sent to Shippo - buy the label there; the bot marks it shipped once the label exists."


async def retry_failed(bot) -> int:
    """Re-sends orders whose Shippo creation failed (up to MAX_ATTEMPTS)."""
    import stripe_api
    import stripe_shop
    sent = 0
    for row in db.shippo_orders_with_status("failed"):
        if row["attempts"] >= MAX_ATTEMPTS:
            continue
        checkout = db.get_stripe_checkout(row["session_id"])
        if not checkout:
            continue
        try:
            session = await stripe_api.retrieve_checkout_session(row["session_id"])
        except stripe_api.StripeError:
            continue
        items = [db.get_item(i) for i in checkout["item_ids"]]
        note = await send_for_checkout(checkout, session, items, stripe_shop._addr(session))
        sent += note.startswith("Sent to Shippo")
    return sent


def _transaction_ids(order: dict) -> list:
    out = []
    for t in order.get("transactions") or []:
        out.append(t if isinstance(t, str) else (t or {}).get("object_id"))
    return [t for t in out if t]


async def sync_labels(bot) -> int:
    """Finds labels bought in Shippo for open orders: marks the items
    Shipped, saves tracking, records the label cost. Returns how many
    orders were completed."""
    done = 0
    for row in db.shippo_orders_with_status("open"):
        try:
            order = await shippo_api.get_order(row["order_id"])
            labels = []
            for tid in _transaction_ids(order):
                txn = await shippo_api.get_transaction(tid)
                if (txn.get("status") or "").upper() != "SUCCESS":
                    continue
                rate_id = txn.get("rate") if isinstance(txn.get("rate"), str) else (txn.get("rate") or {}).get("object_id")
                rate = await shippo_api.get_rate(rate_id) if rate_id else {}
                labels.append({"tracking": txn.get("tracking_number") or "",
                               "url": txn.get("tracking_url_provider") or "",
                               "cost": float(rate.get("amount") or 0),
                               "service": f"{rate.get('provider', '')} {(rate.get('servicelevel') or {}).get('name', '')}".strip()})
        except shippo_api.ShippoError as e:
            log.warning("Couldn't check Shippo order %s: %s", row["order_id"], e)
            continue
        if not labels:
            continue
        await _complete(bot, row, labels)
        done += 1
    return done


async def _complete(bot, row: dict, labels: list):
    items = [db.get_item(i) for i in row["item_ids"]]
    items = [i for i in items if i]
    cost = round(sum(label["cost"] for label in labels), 2)
    tracking = ", ".join(label["tracking"] for label in labels if label["tracking"])
    url = next((label["url"] for label in labels if label["url"]), "")
    # The label cost lands on each item's pallet, split by item price.
    prices = [(i.get("sale_price") or 0) for i in items]
    shares = _split(cost, prices)
    for item, share in zip(items, shares):
        if share:
            db.add_pallet_cost(item["pallet_id"], "shipping", share, "shippo",
                               description=f"Shippo label {tracking}"[:200], item_id=item["id"])
    flow = bot.get_cog("ItemFlow") if bot else None
    for item in items:
        if item["status"] == db.STATUS_SOLD and flow is not None:
            try:
                await flow.do_mark_shipped(item["id"], SYSTEM_ACTOR)
            except Exception:
                log.exception("Couldn't mark item %s shipped after its Shippo label", item["id"])
    db.update_shippo_order(row["session_id"], status="labelled", tracking=tracking, tracking_url=url,
                           label_cost=cost)
    names = ", ".join(f"#{i['item_number']}" for i in items)
    text = (f"📦 **Website order labelled in Shippo** - {names} - label ${cost:.2f}"
            + (f" - tracking {tracking}" if tracking else "") + (f" ({url})" if url else "")
            + " - marked Shipped.")
    channel_id = db.get_shared_channel_id("ebay-orders")
    channel = bot.get_channel(channel_id) if (bot and channel_id) else None
    if channel is not None:
        try:
            await channel.send(text[:1900])
        except Exception:
            log.exception("Couldn't post the Shippo label notice")
    for pallet_id in {i["pallet_id"] for i in items}:
        await finance_utils.refresh_finance_message(bot, pallet_id)


def _split(total: float, weights: list) -> list:
    weights = [max(w or 0, 0) for w in weights]
    if not weights:
        return []
    if not sum(weights):
        weights = [1] * len(weights)
    shares = [round(total * w / sum(weights), 2) for w in weights]
    shares[-1] = round(total - sum(shares[:-1]), 2)
    return shares
