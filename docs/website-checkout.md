# Website ↔ pallet bot API

For whoever maintains the 4guys1palletoverstock.com storefront (Next.js).
The pallet bot does all the Stripe work: it creates the checkout page, holds
the items, collects payment and sales tax, and when the buyer pays it marks
the items sold, ends their eBay listings and books everything to QuickBooks.

**Active eBay listings are the source of truth** for each item's listing ID
and price, matched by Custom label (`pallet-N-item-M`). The website keeps its
products in step with the catalog below, refreshes right before checkout,
and the bot re-checks everything again when checkout starts.

Base URL: `https://dash.4guys1palletoverstock.com`

## 1. Authentication (every `/shop/` route)

Server-to-server only. Every request carries the shared key:

```
X-Shop-Key: <shared key - the bot's STRIPE_SHOP_SERVER_KEY = the website's PALLET_BOT_SHOP_KEY>
X-Client-IP: <the shopper's IP>   (optional - gives each shopper their own rate limit)
```

- The key is compared in constant time and checked **before** anything
  else - inventory isn't read and eBay isn't called without it.
- Wrong or missing key → `403 {"error": "Not allowed.", "code": "unauthorized"}`.
  If the bot has no key configured, every request is refused (fails closed).
- Never put the key in a URL, a log, or a browser. Calling from the browser
  isn't supported any more.
- Errors are JSON: `{"error": "<message safe to show>", "code": "<code>", ...}`.
- Rate limit: 10 refresh/checkout calls per minute per shopper (`429`, `rate_limited`).

## 2. `GET /shop/catalog` - full snapshot (`ebay-catalog-v1`)

```json
{
  "catalog_contract": "ebay-catalog-v1",
  "complete": true,
  "generated_at": "2026-10-19T18:02:11.418Z",
  "items": [
    {"sku": "pallet-1-item-96", "ebay_listing_id": "860012952535", "ebay_active": true,
     "price_cents": 2200, "currency": "usd", "available_quantity": 1,
     "bot_status": "listed", "ebay_checked_at": "2026-10-19T17:55:03.120Z"},
    {"sku": "pallet-1-item-111", "ebay_listing_id": "318921888878", "ebay_active": true,
     "price_cents": 5337, "currency": "usd", "available_quantity": 0,
     "bot_status": "sold", "ebay_checked_at": "2026-10-19T17:55:03.120Z"},
    {"sku": "pallet-1-item-113", "ebay_listing_id": null, "ebay_active": false,
     "price_cents": null, "currency": "usd", "available_quantity": 0,
     "bot_status": "listed", "ebay_checked_at": "2026-10-19T17:55:03.120Z"}
  ]
}
```

(Example values are illustrative.)

- Every item the bot tracks, including sold / shipped / on-hold / deleted
  ones, so the website can hide them. The feed never asks the website to
  add products - update only products you already have.
- **`ebay_listing_id` / `price_cents`** come from the seller's *active*
  listings (GetMyeBaySelling, every page), matched by Custom label. A
  relisted SKU keeps its identity and gets the new listing ID and price.
- **Ended / missing listing:** `ebay_active=false`, `ebay_listing_id=null`,
  `price_cents=null`, `available_quantity=0`. An ended listing's price is
  never reported.
- **`bot_status`:** `listed`, `sold`, `shipped`, `on_hold`, or
  `unavailable` (anything else - still being processed, deleted, unknown).
- **`available_quantity`** is eBay's available quantity, but **0** unless
  the item is `listed` in the bot, live on eBay as a fixed-price listing,
  and not reserved by another checkout. Sold / shipped / held items are 0
  even while eBay still shows them. Auctions report no price and 0.
- **`ebay_checked_at`** is when the bot last *successfully* read the full
  active-listing list from eBay (refreshed every 20 minutes).
- If that check is more than 60 minutes old, or has never succeeded:
  `503 {"code": "ebay_unavailable"}` - never a stale snapshot labeled fresh.
  A 503 means "don't change anything", not "items sold".
- One response, no pagination.

## 3. `POST /shop/catalog/refresh` - fresh check for specific items

```
POST /shop/catalog/refresh
{"items": ["pallet-1-item-113", "pallet-1-item-96"]}
```

- 1-20 unique SKUs (`pallet-N-item-M`), otherwise `400 bad_request`.
- Runs a fresh, complete eBay check (all pages), reads current bot status
  and checkout holds, and answers one record per requested SKU, in order,
  with the same fields as the catalog:

```json
{"catalog_contract": "ebay-catalog-v1", "scope": "requested_items",
 "generated_at": "2026-10-19T18:04:40.002Z",
 "items": [ {"sku": "pallet-1-item-113", ...}, {"sku": "pallet-1-item-96", ...} ]}
```

- An unknown SKU comes back as `bot_status="unavailable"`, nulls, quantity 0
  and `ebay_checked_at=null` - never silently dropped.
- Calls within 30 seconds of a successful check share it (its real time is
  reported). If eBay can't be checked within ~25 seconds:
  `503 ebay_unavailable`.
- Never creates a Stripe session, reserves anything or ends a listing. It
  does copy eBay's current price into the bot for those items.

## 4. `POST /shop/checkout` (`website-prices-v1`)

```
POST /shop/checkout
{"items": ["pallet-1-item-96"],
 "checkout_contract": "website-prices-v1",
 "website_prices": {"pallet-1-item-96": 2200}}
```

1. Key check, then the body: `checkout_contract` must be
   `website-prices-v1`; `items` 1-20 unique SKUs; `website_prices` exactly
   those SKUs, each a positive whole number of cents (max 2,000,000).
   Otherwise `400 bad_request`.
2. Fresh eBay + bot check (reusing one less than 2 minutes old). eBay can't
   be checked → `503 ebay_unavailable`.
3. Any item not sellable - not `listed`, held, ended on eBay, an auction,
   or a multi-unit listing (website checkout is one unit of a one-unit
   listing; multi-unit listings stay on eBay) →

   ```json
   {"error": "Some items are no longer available.", "code": "not_available",
    "unavailable_items": ["pallet-1-item-111"]}
   ```
4. Any `website_prices` that differs from the current eBay price →

   ```json
   {"error": "Some prices have changed - refresh and try again.", "code": "price_changed",
    "price_changes": [{"sku": "pallet-1-item-113", "website_cents": 8652, "current_cents": 5000}],
    "current_prices": {"pallet-1-item-113": 5000}}
   ```
   Nothing is reserved and nothing is marked sold.
5. Otherwise the items are reserved (re-checked inside the reservation, so
   two buyers can't get the same item - the loser gets `not_available`) and
   Stripe Checkout is created at the verified prices. The answer is built
   from what was sent to Stripe and checked against Stripe's own
   `amount_subtotal`:

```json
{"url": "https://checkout.stripe.com/g/pay/cs_test_...",
 "checkout_contract": "website-prices-v1", "currency": "usd",
 "subtotal_cents": 2200, "website_prices": {"pallet-1-item-96": 2200},
 "session_id": "cs_test_...", "expires_at": "2026-10-19T18:35:10+00:00"}
```

- Shipping (free) and Indiana sales tax are added by Stripe on its page.
- `503 checkout_unavailable` if Stripe itself fails - try again later.
- Reservations last 30 minutes and are released when Stripe reports the
  session expired. Fulfillment happens only on Stripe's signed webhook.

## 5. Pages Stripe sends the buyer back to

- Success: `https://4guys1palletoverstock.com/checkout/success?session_id={CHECKOUT_SESSION_ID}`
  - not proof of payment on its own; use section 6.
- Cancel: `https://4guys1palletoverstock.com/cart`

## 6. `GET /shop/session/{session_id}` - order status

→ `{"session_id", "state": "open|processing|paid|expired|failed|refund_pending",
"paid": true|false, "items": ["pallet-1-item-96"], "amount_total_cents": 2354}`.
`paid` turns true once the bot has Stripe's signed payment notification.
404 for an unknown session.

## 7. Testing without touching real stock

1. Bot owner: switch the bot to Stripe **test** keys (`sk_test_...` and the
   test-mode webhook secret) and restart.
2. Make a throwaway item in a test pallet in Discord, give it a small price
   and mark it **Listed (Other)** - it has no eBay listing.
3. Put its SKU in the bot's `.env`: `SHOP_TEST_SKUS=pallet-N-item-M`, restart.
   That SKU then shows as available (bot price, quantity 1) - **only while
   the bot is on test keys**; on live keys it's refused like any item
   without an eBay listing.
4. From the website: refresh → checkout → pay with card
   `4242 4242 4242 4242`, any future date / CVC, a US address.
5. Check: the item goes Sold in Discord, `/shop/session/...` says paid.
   Also try a wrong price (409 price_changed), the same item twice (409
   not_available), and letting a session expire (the reservation is released).
6. Afterwards: `/item delete` the throwaway item, remove `SHOP_TEST_SKUS`,
   switch back to live keys, restart.
