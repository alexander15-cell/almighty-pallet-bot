"""
Stripe on the web server:

  The website's SERVER only - every /shop/ route needs the shared key
  (X-Shop-Key = STRIPE_SHOP_SERVER_KEY), checked before anything else.
  Contracts: docs/website-checkout.md sections 7-8.

  GET  /shop/catalog            ebay-catalog-v1 snapshot of every item.
  POST /shop/catalog/refresh    fresh eBay check for 1-20 SKUs.
  POST /shop/checkout           website-prices-v1 - verified against eBay,
                                then a Stripe Checkout page.
  GET  /shop/session/{id}       where a checkout stands.
  POST /stripe/webhook  public - Stripe's notifications, signature-checked.
  POST /items/{id}/payment-link   signed in - a single-use payment page for
                        one item, to send a buyer (ship or local pickup).
  GET  /finance/website           signed in - website orders.
"""
import hmac
import json
import logging
import time
from collections import defaultdict, deque

from aiohttp import web

import config
import database as db
import ebay_catalog
import stripe_api
import stripe_shop
from webapp.layout import csrf_input, esc, money, page, qs
from webapp.server import bot_of, html_response, require, user_of

log = logging.getLogger(__name__)

_recent = defaultdict(deque)  # client IP -> checkout start times (a light rate limit)
RATE_LIMIT, RATE_WINDOW = 10, 60
WEBSITE_PRICES_CONTRACT = "website-prices-v1"


def _from_website_server(request) -> bool:
    """The website's server, proven by the shared key (constant-time compare).
    No key configured = nobody (fail closed)."""
    if not config.STRIPE_SHOP_SERVER_KEY:
        log.warning("STRIPE_SHOP_SERVER_KEY isn't set - refusing website shop requests")
        return False
    key = request.headers.get("X-Shop-Key", "")
    return bool(key) and hmac.compare_digest(key.encode(), config.STRIPE_SHOP_SERVER_KEY.encode())


def _denied():
    return web.json_response({"error": "Not allowed.", "code": "unauthorized"}, status=403)


def _error(status: int, code: str, message: str, **extra):
    return web.json_response({"error": message, "code": code, **extra}, status=status,
                             headers={"Cache-Control": "no-store"})


def _rate_limited(request) -> bool:
    # The website's server forwards for every shopper, so its own IP would
    # rate-limit everyone together - its X-Client-IP is trusted (the key was
    # already checked by every caller of this).
    ip = request.headers.get("X-Client-IP", "")[:64] or request.headers.get("CF-Connecting-IP") or request.remote or "?"
    now = time.monotonic()
    times = _recent[ip]
    while times and now - times[0] > RATE_WINDOW:
        times.popleft()
    if len(times) >= RATE_LIMIT:
        return True
    times.append(now)
    return False


async def _json_body(request):
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


async def catalog(request):
    """GET /shop/catalog - the full ebay-catalog-v1 snapshot."""
    if not _from_website_server(request):
        return _denied()
    scope = request.query.get("scope")
    if scope not in (None, "test"):
        return _error(400, "bad_request", "scope must be omitted or 'test'.")
    try:
        data = ebay_catalog.catalog(scope)
    except ebay_catalog.NotFound:
        return _error(404, "not_found", "No test catalog while the bot is on live Stripe keys.")
    except ebay_catalog.CatalogUnavailable as e:
        return _error(503, "ebay_unavailable", str(e))
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def catalog_refresh(request):
    """POST /shop/catalog/refresh {"items": [sku, ...]} - a fresh eBay check
    for 1-20 SKUs. Never creates a checkout, holds anything or ends a listing."""
    if not _from_website_server(request):
        return _denied()
    if _rate_limited(request):
        return _error(429, "rate_limited", "Too many tries - wait a minute.")
    body = await _json_body(request)
    try:
        skus = ebay_catalog.parse_skus((body or {}).get("items"))
    except ValueError as e:
        return _error(400, "bad_request", str(e))
    try:
        data = await ebay_catalog.refresh(skus)
    except ebay_catalog.CatalogUnavailable:
        return _error(503, "ebay_unavailable", "eBay couldn't be checked right now - try again shortly.")
    return web.json_response(data, headers={"Cache-Control": "no-store"})


def _website_prices(skus: list, raw_items: list, prices) -> dict:
    """{sku: cents} - exactly the requested items, positive whole cents."""
    if not isinstance(prices, dict):
        raise ValueError("website_prices must map each SKU to whole cents.")
    by_sku = {}
    for raw, sku in zip(raw_items, skus):
        cents = prices.get(raw, prices.get(sku))
        if isinstance(cents, bool) or not isinstance(cents, int) or not 0 < cents <= stripe_shop.MAX_UNIT_CENTS:
            raise ValueError(f"The price for {sku} must be a positive whole number of cents.")
        by_sku[sku] = cents
    if len(prices) != len(skus):
        raise ValueError("website_prices must list exactly the items in the cart.")
    return by_sku


async def checkout(request):
    """
    POST /shop/checkout (website-prices-v1) - the website's server only.
    Checks every SKU against a fresh eBay pass and the bot's own state
    before anything is reserved: 409 not_available / price_changed, 503 if
    eBay can't be checked. Then holds the items and creates Stripe Checkout
    at the verified eBay prices, and confirms exactly what Stripe will charge.
    """
    if not _from_website_server(request):
        log.warning("Refused a website checkout without a valid X-Shop-Key")
        return _denied()
    if _rate_limited(request):
        return _error(429, "rate_limited", "Too many tries - wait a minute.")
    body = await _json_body(request)
    if body is None:
        return _error(400, "bad_request", "Send a JSON object.")
    if body.get("checkout_contract") != WEBSITE_PRICES_CONTRACT:
        return _error(400, "bad_request", f"checkout_contract must be {WEBSITE_PRICES_CONTRACT!r}.")
    try:
        skus = ebay_catalog.parse_skus(body.get("items"))
        wanted = _website_prices(skus, body["items"], body.get("website_prices"))
    except ValueError as e:
        return _error(400, "bad_request", str(e))
    delivery = body.get("delivery", "ship")
    if delivery not in ("ship", "pickup") or (delivery == "pickup" and not config.SHIP_LOCAL_PICKUP):
        return _error(400, "bad_request", "delivery must be \"ship\"" +
                      (" or \"pickup\"." if config.SHIP_LOCAL_PICKUP else "."))
    try:
        check = await ebay_catalog.verify_for_checkout(skus, wanted)
    except ebay_catalog.CatalogUnavailable:
        return _error(503, "ebay_unavailable", "eBay couldn't be checked right now - try again shortly.")
    if check["unavailable"]:
        return _error(409, "not_available", "Some items are no longer available.",
                      unavailable_items=check["unavailable"])
    if check["price_changes"]:
        return _error(409, "price_changed", "Some prices have changed - refresh and try again.",
                      price_changes=check["price_changes"], current_prices=check["prices"])
    try:
        result = await stripe_shop.start_checkout(skus, source="website", delivery=delivery,
                                                  website_prices=check["prices"])
    except stripe_shop.CheckoutError as e:
        if e.skus:
            return _error(409, "not_available", str(e), unavailable_items=[s for s in e.skus if s in skus])
        return _error(503, "checkout_unavailable", str(e))
    return web.json_response({
        "url": result["url"], "checkout_contract": WEBSITE_PRICES_CONTRACT, "currency": "usd",
        "subtotal_cents": result["subtotal_cents"], "website_prices": result["unit_amounts"],
        "session_id": result["session_id"], "expires_at": result["expires_at"], "livemode": result["livemode"],
        "delivery": result["delivery"], "shipping_cents": result["shipping_cents"]},
        headers={"Cache-Control": "no-store"})


async def session_status(request):
    """GET /shop/session/{id} - the website's server asks whether a checkout
    was paid (shared key required)."""
    if not _from_website_server(request):
        return web.json_response({"error": "Not allowed."}, status=403)
    state = stripe_shop.session_state(request.match_info["session_id"])
    if state is None:
        return web.json_response({"error": "No such checkout."}, status=404)
    return web.json_response(state)


async def webhook(request):
    payload = await request.read()
    event, error = None, None
    # Live endpoint first; then the test-mode endpoint (checkout test item).
    for secret in (config.STRIPE_WEBHOOK_SECRET, config.STRIPE_TEST_WEBHOOK_SECRET):
        if not secret:
            continue
        try:
            event = stripe_api.verify_webhook(payload, request.headers.get("Stripe-Signature", ""), secret)
            break
        except stripe_api.StripeError as e:
            error = e
    if event is None:
        log.warning("Rejected a Stripe webhook: %s", error or "no webhook secret configured")
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
        # Staff only - show what actually went wrong (Stripe's own message).
        message = f"{e} ({e.detail})" if e.detail else str(e)
        raise web.HTTPSeeOther(f"/items/{item['id']}?{qs(msg=message[:400])}")
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
    app.router.add_get("/shop/catalog", catalog)
    app.router.add_post("/shop/catalog/refresh", catalog_refresh)
    app.router.add_post("/shop/checkout", checkout)
    app.router.add_get(r"/shop/session/{session_id:cs_[A-Za-z0-9_]+}", session_status)
    app.router.add_post("/stripe/webhook", webhook)
    app.router.add_post(r"/items/{item_id:\d+}/payment-link", payment_link)
    app.router.add_get("/finance/website", website_orders)
