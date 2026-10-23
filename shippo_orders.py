"""
Shipped sales -> Shippo (SHIPPO_ORDERS=true).

Two kinds of non-eBay sale get a Shippo order, where a person checks the
box and buys the label:
  - a paid website / payment-link order (send_for_checkout) - buyer's
    address and email from Stripe, plus the service the buyer was quoted;
  - an in-person / other sale that ships, as soon as its buyer's address is
    saved with /finance set-shipping-info (send_for_item).
Each order lists every item with its SKU, weight and box size.

Every few minutes the bot asks Shippo about open orders (sync_labels). Once
a label has been bought it:
  - marks each item Shipped (the same shared action as the Discord button),
  - keeps the tracking number / link,
  - records the real label cost against the item's pallet (pallet_costs,
    source 'shippo' - pallet profit only; nothing is posted to QuickBooks
    here: the charge reaches the books the same way other card charges do),
  - posts a notice in #ebay-orders.

Pickup orders, eBay orders (labels bought through eBay) and test-mode
payments never go to Shippo.
"""
import logging
from datetime import datetime, timezone

import config
import database as db
import ebay_listing_rules
import finance_utils
import shipping
import shippo_api
import unit_groups

log = logging.getLogger(__name__)
SYSTEM_ACTOR = 0
MAX_ATTEMPTS = 5


def enabled() -> bool:
    return config.SHIPPO_ORDERS and bool(config.SHIPPO_API_TOKEN)


def _money(dollars) -> str:
    return f"{float(dollars or 0):.2f}"


def _line(item: dict) -> tuple:
    listing = db.get_ebay_listing_data(item["id"]) or {}
    parcel, source = shipping.parcel_for(item, listing)
    title = (listing.get("ebay_title") or item.get("ai_title") or f"Item #{item['item_number']}")[:150]
    box = (f"box {parcel['length']:g}x{parcel['width']:g}x{parcel['height']:g} in, {parcel['weight']:g} lb"
           + (" - DEFAULT BOX, measure it" if source == "default" else ""))
    return ({"title": f"{title} [{box}]", "sku": unit_groups.public_sku(item), "quantity": 1,
             "total_price": _money(item.get("sale_price") or listing.get("price")), "currency": "USD",
             "weight": f"{parcel['weight']:.2f}", "weight_unit": "lb"}, parcel["weight"])


def build_order(order_number: str, items: list, address: dict, email: str = None, totals: dict = None,
                shipping_method: str = None) -> dict:
    """A Shippo order. address: {name, line1, line2, city, state, postal_code,
    country}; totals (dollars): {subtotal, shipping, tax, total} when known."""
    lines = [_line(item) for item in items]
    to_address = {"name": address["name"], "street1": address["line1"], "street2": address.get("line2") or "",
                  "city": address["city"], "state": address["state"], "zip": address["postal_code"],
                  "country": address.get("country") or "US"}
    if email:
        to_address["email"] = email
    subtotal = sum(float(line["total_price"]) for line, _ in lines)
    totals = totals or {"subtotal": subtotal, "shipping": 0, "tax": 0, "total": subtotal}
    return {
        "order_number": order_number,
        "order_status": "PAID",
        "placed_at": datetime.now(timezone.utc).isoformat(),
        "to_address": to_address,
        "from_address": shippo_api.ship_from(),
        "line_items": [line for line, _ in lines],
        "shipping_cost": _money(totals["shipping"]),
        "shipping_cost_currency": "USD",
        "shipping_method": shipping_method or config.SHIP_LABEL,
        "subtotal_price": _money(totals["subtotal"]),
        "total_tax": _money(totals["tax"]),
        "total_price": _money(totals["total"]),
        "currency": "USD",
        "weight": f"{sum(w for _, w in lines):.2f}",
        "weight_unit": "lb",
    }


async def _send(key: str, order: dict, item_ids: list) -> str:
    """Creates the Shippo order once per key. Returns a note."""
    existing = db.get_shippo_order(key)
    if existing and (existing["status"] != "failed" or existing["attempts"] >= MAX_ATTEMPTS):
        return ""
    try:
        created = await shippo_api.create_order(order)
    except shippo_api.ShippoError as e:
        db.save_shippo_order(key, None, "failed", item_ids, error=str(e)[:300])
        log.warning("Couldn't send order %s to Shippo: %s", key, e)
        return f"Couldn't send this order to Shippo ({str(e)[:150]}) - the bot will retry."
    db.save_shippo_order(key, created.get("object_id"), "open", item_ids)
    return "Sent to Shippo - buy the label there; the bot marks it shipped once the label exists."


def _checkout_order(checkout: dict, session: dict, items: list, address: dict) -> dict:
    totals = session.get("total_details") or {}
    quoted = db.get_checkout_shipping(checkout["session_id"]) or {}
    quote_row = db.get_shipping_quote(quoted["quote_id"]) if quoted.get("quote_id") else None
    services = sorted({f"{d.get('carrier', '')} {d.get('service', '')}".strip()
                       for d in (quote_row or {}).get("detail", [])} - {""})
    return build_order(
        f"4G1P-{checkout['session_id'][-12:]}", items, address,
        email=(session.get("customer_details") or {}).get("email"),
        totals={"subtotal": (session.get("amount_subtotal") or 0) / 100,
                "shipping": (totals.get("amount_shipping") or 0) / 100,
                "tax": (totals.get("amount_tax") or 0) / 100,
                "total": (session.get("amount_total") or 0) / 100},
        shipping_method=", ".join(services) or None)


async def send_for_checkout(checkout: dict, session: dict, items: list, address: dict) -> str:
    """Website / payment-link order -> Shippo (once). Returns a note for the
    order notice ('' when there's nothing to say)."""
    if not address:
        return "No shipping address from Stripe - make this label by hand in Shippo."
    return await _send(checkout["session_id"], _checkout_order(checkout, session, items, address),
                       [i["id"] for i in items])


def _item_address(item: dict):
    if not (item.get("recipient_name") and item.get("address_line1") and item.get("city")
            and item.get("postal_code")):
        return None
    return {"name": item["recipient_name"], "line1": item["address_line1"], "line2": item.get("address_line2"),
            "city": item["city"], "state": item.get("state") or "", "postal_code": item["postal_code"],
            "country": item.get("country") or "US"}


async def send_for_item(item: dict) -> str:
    """An in-person / other (non-eBay) sale that ships, once its buyer's
    address is saved -> Shippo (once per item)."""
    item = db.get_item(item["id"])
    if item["status"] != db.STATUS_SOLD or (item.get("sale_platform") or "").lower() in ("", "ebay", "website"):
        return ""
    address = _item_address(item)
    if not address:
        return "The address is incomplete (name, street, city and ZIP needed) - not sent to Shippo yet."
    order = build_order(f"4G1P-{ebay_listing_rules.custom_label(item)}", [item], address)
    return await _send(f"item-{item['id']}", order, [item["id"]])


async def retry_failed(bot) -> int:
    """Re-sends orders whose Shippo creation failed (up to MAX_ATTEMPTS)."""
    import stripe_api
    import stripe_shop
    sent = 0
    for row in db.shippo_orders_with_status("failed"):
        if row["attempts"] >= MAX_ATTEMPTS:
            continue
        key = row["session_id"]
        if key.startswith("item-"):
            item = db.get_item(int(key.split("-", 1)[1]))
            note = await send_for_item(item) if item else ""
        else:
            checkout = db.get_stripe_checkout(key)
            if not checkout:
                continue
            try:
                session = await stripe_api.retrieve_checkout_session(key)
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
