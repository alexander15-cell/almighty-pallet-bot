"""
Stripe on the web server:

  POST /shop/checkout   public - the website's Checkout button. Body
                        {"items": [item id or "pallet-N-item-M", ...]}.
                        Answers {"url": <Stripe Checkout page>} or
                        {"error": "..."}. Only the website origins in
                        STRIPE_SHOP_ORIGINS may call it from a browser.
  POST /stripe/webhook  public - Stripe's notifications, signature-checked.
  POST /items/{id}/payment-link   signed in - a single-use payment page for
                        one item, to send a buyer (ship or local pickup).
  GET  /finance/website           signed in - website orders.
"""
import json
import logging
import time
from collections import defaultdict, deque

from aiohttp import web

import config
import database as db
import stripe_api
import stripe_shop
from webapp.layout import csrf_input, esc, money, page, qs
from webapp.server import bot_of, html_response, require, user_of

log = logging.getLogger(__name__)

_recent = defaultdict(deque)  # client IP -> checkout start times (a light rate limit)
RATE_LIMIT, RATE_WINDOW = 10, 60


def _cors(request, resp):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin and origin in config.STRIPE_SHOP_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


async def checkout_preflight(request):
    return _cors(request, web.Response(status=204))


def _client_ip(request) -> str:
    return request.headers.get("CF-Connecting-IP") or request.remote or "?"


async def checkout(request):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin and origin not in config.STRIPE_SHOP_ORIGINS:
        return web.json_response({"error": "Not allowed."}, status=403)
    ip, now = _client_ip(request), time.monotonic()
    times = _recent[ip]
    while times and now - times[0] > RATE_WINDOW:
        times.popleft()
    if len(times) >= RATE_LIMIT:
        return _cors(request, web.json_response({"error": "Too many tries - wait a minute."}, status=429))
    times.append(now)
    try:
        body = await request.json()
        refs = body.get("items")
        if not isinstance(refs, list):
            raise ValueError
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _cors(request, web.json_response({"error": "Send {\"items\": [...]}."}, status=400))
    try:
        result = await stripe_shop.start_checkout(refs, source="website", delivery="ship")
    except stripe_shop.CheckoutError as e:
        return _cors(request, web.json_response({"error": str(e)}, status=409))
    return _cors(request, web.json_response({"url": result["url"], "expires_at": result["expires_at"]}))


async def webhook(request):
    payload = await request.read()
    try:
        event = stripe_api.verify_webhook(payload, request.headers.get("Stripe-Signature", ""),
                                          config.STRIPE_WEBHOOK_SECRET)
    except stripe_api.StripeError as e:
        log.warning("Rejected a Stripe webhook: %s", e)
        return web.Response(status=400, text="bad signature")
    try:
        outcome = await stripe_shop.handle_event(bot_of(request), event)
    except Exception:
        log.exception("Stripe webhook %s failed", event.get("id"))
        return web.Response(status=500, text="error")  # Stripe retries
    log.info("Stripe %s %s: %s", event.get("type"), event.get("id"), outcome)
    return web.json_response({"received": True})


async def payment_link(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT, config.ROLE_FINANCE_MGMT)
    item = db.get_item(int(request.match_info["item_id"]))
    if not item:
        raise web.HTTPNotFound()
    form = await request.post()
    delivery = "pickup" if form.get("delivery") == "pickup" else "ship"
    try:
        result = await stripe_shop.start_checkout([item["id"]], source="link", delivery=delivery,
                                                  actor_id=user.discord_id)
    except stripe_shop.CheckoutError as e:
        raise web.HTTPSeeOther(f"/items/{item['id']}?{qs(msg=str(e))}")
    until = result["expires_at"][:16].replace("T", " ")
    body = (f"<p><a href='/items/{item['id']}'>‹ Back to the item</a></p><h1>Payment link ready</h1>"
            f"<div class='card'><p>Send this to the buyer. It's for this one item only, works once, and holds the item "
            f"until {esc(until)} UTC ({'local pickup' if delivery == 'pickup' else 'shipped'}; tax added at checkout).</p>"
            f"<input type='text' readonly value='{esc(result['url'])}' id='pay-link'>"
            f"<div class='actions'><button type='button' class='btn primary' data-copy='pay-link'>Copy link</button>"
            f"<a class='btn' href='{esc(result['url'])}' target='_blank' rel='noopener'>Open it</a></div></div>"
            "<p class='muted'>When they pay, the bot marks it sold, ends the eBay listing and logs the sale.</p>")
    return html_response(page(user, "Payment link", body, active="listing"))


def payment_link_form(user, item: dict) -> str:
    """The item page's 'Send payment link' panel (Listed items)."""
    if not config.STRIPE_ENABLED or item["status"] != db.STATUS_LISTED:
        return ""
    if not user.has(config.ROLE_LISTING_MGMT, config.ROLE_FINANCE_MGMT):
        return ""
    return (f'<form method="post" action="/items/{item["id"]}/payment-link" class="card">{csrf_input(user)}'
            '<h2 style="margin-top:0">Send a payment link</h2><p class="muted small">For a buyer you\'re talking to '
            '(e.g. on Facebook): a Stripe page for just this item. Card, Apple Pay or Google Pay; tax is added.</p>'
            '<select name="delivery"><option value="ship">Ship it to them</option>'
            '<option value="pickup">Local pickup</option></select>'
            '<button class="btn primary">Create payment link</button></form>')


async def website_orders(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    rows = []
    for c in db.list_stripe_checkouts(limit=50):
        items = ", ".join(f"<a href='/items/{i}'>#{(db.get_item(i) or {}).get('item_number', '?')}</a>"
                          for i in c["item_ids"])
        state = {"fulfilled": "✅ done", "attention": "⚠️ needs attention", "paid": "⏳ finishing",
                 "processing": "⏳ payment clearing", "open": "🛒 at checkout", "expired": "expired",
                 "failed": "payment failed"}.get(c["status"], c["status"])
        rows.append(f"<tr><td>{esc(c['created_at'][:16].replace('T', ' '))}</td><td>{esc(c['source'])}</td>"
                    f"<td>{items}</td><td class='num'>{money(c['amount_total'])}</td>"
                    f"<td class='num'>{money(c['amount_tax'])}</td><td class='num'>{money(c['stripe_fee'])}</td>"
                    f"<td>{esc(state)}{'<br><span class=muted small>' + esc(c['note']) + '</span>' if c['note'] else ''}</td></tr>")
    body = ("<h1>Website orders</h1><div class='tabs'><a href='/finance'>To confirm</a><a href='/finance/pallets'>"
            "Pallet profit</a><a href='/finance/sales'>Sales</a><a href='/finance/payouts'>Payouts</a>"
            "<a class='on' href='/finance/website'>Website</a></div>"
            "<div class='card scroll'><table><tr><th>When</th><th>From</th><th>Items</th><th class='num'>Total</th>"
            "<th class='num'>Tax</th><th class='num'>Stripe fee</th><th>State</th></tr>"
            f"{''.join(rows) or '<tr><td colspan=7 class=muted>No website orders yet.</td></tr>'}</table></div>")
    return html_response(page(user, "Website orders", body, active="finance"))


def add_routes(app: web.Application):
    app.router.add_route("OPTIONS", "/shop/checkout", checkout_preflight)
    app.router.add_post("/shop/checkout", checkout)
    app.router.add_post("/stripe/webhook", webhook)
    app.router.add_post(r"/items/{item_id:\d+}/payment-link", payment_link)
    app.router.add_get("/finance/website", website_orders)
