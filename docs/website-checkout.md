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
- **`shipping_cents`** - what shipping this item costs on its own when the
  bot runs custom shipping (priced from the item's size and weight; same
  price anywhere in the US). `null` in flat-rate mode or when the listing
  isn't live. An order ships for the **sum** of its items; local pickup is $0.
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
 "session_id": "cs_test_...", "expires_at": "2026-10-19T18:35:10+00:00",
 "livemode": false, "delivery": "ship", "shipping_cents": 900}
```

- Optional `"delivery": "ship"` (default) or `"pickup"` (local pickup, $0
  shipping, billing address instead of a shipping address).
- `shipping_cents` in the answer is the shipping Stripe shows as its own line
  (the sum of the items' `shipping_cents`; 0 for pickup). `subtotal_cents`
  stays items only. Indiana sales tax is added by Stripe Tax on its page
  (on shipping too).
- `503 checkout_unavailable` if Stripe itself fails - try again later.
- Reservations last 30 minutes and are released when Stripe reports the
  session expired. Fulfillment happens only on Stripe's signed webhook.

## 4b. Live shipping (when the bot runs `SHIPPING_MODE=shippo`)

Shipping depends on where the box goes, so the cart asks for the buyer's
ZIP code **before** checkout (Stripe's page can't re-price shipping after the
address is typed).

```
POST /shop/shipping/quote
{"items": ["pallet-1-item-96", "pallet-1-item-113"], "zip": "46201"}
```
→
```json
{"quote_id": "shq_...", "zip": "46201", "shipping_cents": 2160,
 "expires_at": "2026-10-21T19:40:00+00:00",
 "items": [{"sku": "pallet-1-item-96", "shipping_cents": 940, "carrier": "USPS",
            "service": "Ground Advantage", "days": 3, "parcel": "listing"},
           {"sku": "pallet-1-item-113", "shipping_cents": 1220, "carrier": "USPS",
            "service": "Ground Advantage", "days": 4, "parcel": "default"}]}
```

- Each item ships as its own box (its saved size and weight; `parcel:
  "default"` = no measurements saved, a standard box was used); the price is
  the cheapest live carrier rate plus the business's handling/markup.
- `400 bad_request` for a bad ZIP; `503 shipping_unavailable` if rates can't
  be fetched - don't show a made-up price. `404` when live shipping is off.
- Quotes last 60 minutes and are for exactly that set of items. Then send
  `"shipping_quote_id": "shq_..."` with the checkout (delivery `ship`). A
  missing, expired or different-cart quote → `409 shipping_quote_required`
  (re-quote). Stripe charges exactly `shipping_cents` as its own line.
- If the buyer then types a different ZIP on Stripe's page the order still
  goes through and the bot flags it for the owner.
- `"delivery": "pickup"` needs no quote ($0 shipping).
- In this mode the catalog's per-item `shipping_cents` is `null` (it
  depends on the ZIP).

## 4c. Multi-unit listings

A listing with several units (e.g. `pallet-1-item-43`, 17 on eBay) is one
product to the website, under the listing's SKU. Inside the bot each unit
is its own item (set up with `/admin sync-units`), so one unit can be sold,
held or shipped without touching the others.

- **Catalog / refresh:** `available_quantity` is the NET number a new buyer
  can reserve now - Listed units not reserved by another checkout, never
  more than eBay itself shows. `bot_status` stays `listed` while any unit
  is. The added units' own SKUs never appear and read as unknown.
- **Checkout:** unchanged contract. Each SKU in `items` still means ONE
  unit (no duplicates; a quantity selector would be a new contract). The
  bot reserves one specific unit; `website_prices` / `subtotal_cents` are
  per unit. `not_available` only when no unit can be reserved.
- **Quote:** one sellable unit's box per SKU, same binding to the cart.
- **eBay stays in step:** eBay's available quantity is set to the units
  still for sale here - a unit reserved by a website checkout comes off eBay
  before the buyer can pay; an expired/cancelled checkout puts it back; a
  paid one keeps it off. Before every eBay write the bot re-reads the
  listing: if eBay shows a sale the bot hasn't recorded yet, it doesn't
  write (that would resurrect a sold unit) and the checkout gets
  `409 not_available` until the order is recorded (every ~20 min). At 0
  units the listing is ended, and relisted (new listing ID in the catalog)
  if a reserved unit comes back. If eBay can't be updated, the checkout is
  cancelled before payment and the owner is told. A 10-minute sync repairs
  anything a missed event or restart left out of step.
- **Only listings made in Seller Hub** are supported for multi-unit; a
  bot-made (Inventory API) multi-unit listing is left eBay-only.
- **Test:** `/admin checkout-test action:create-multi` makes a disposable
  3-unit item (test keys only, `?scope=test`, no eBay listing).

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

Production stays on the live keys. The disposable test item uses its own
Stripe **test** keys (`STRIPE_TEST_SECRET_KEY` + `STRIPE_TEST_WEBHOOK_SECRET`,
the signing secret of a test-mode endpoint at the same `/stripe/webhook`
URL). Real items always use the live keys; the test item always uses the
test keys and is refused if they aren't set; the two can't share a cart.

**The test item** - `/admin checkout-test action:create` makes "TEST
CHECKOUT ONLY", $1.00, in its own archived pallet with no channels, and
replies with its SKU (`pallet-N-item-1`).

- `GET /shop/catalog?scope=test` - only the test item, with a simulated
  listing (`ebay_listing_id: "TEST-<sku>"`, price 100, quantity 1,
  `ebay_checked_at` = response time). Never in the normal `GET /shop/catalog`.
  404 when no test key is set.
- refresh / checkout naming the test SKU use the simulated listing (no eBay
  call) and create the session with the test keys: `"livemode": false`,
  `cs_test_` id. Stale price → `409 price_changed`; held → `409 not_available`.
- The dashboard's **Send a payment link** on the test item works the same way.
- A paid test session only flips the test item to Sold: no Discord cards,
  no eBay changes, no sale log, no QuickBooks, no buyer messages.
- Shipping: the bot's free-shipping rate (`STRIPE_SHIPPING_PRICE`, $0) as its
  own line; tax: Stripe Tax on the test account (shown as its own line once
  the test account's tax settings have a head office and an Indiana registration).

**Reset between tests:** `/admin checkout-test action:reset` - back to
Listed, any open test checkout cancelled. `action:status` shows it;
`action:retire` soft-deletes it when testing is finished.

Card `4242 4242 4242 4242`, any future date / CVC, a US address.
