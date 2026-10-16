# Hooking the website cart up to Stripe checkout

For whoever maintains the 4guys1palletoverstock.com storefront (Next.js).
The pallet bot does all the Stripe work: it creates the checkout page, holds
the items, collects payment and sales tax, and when the buyer pays it marks
the items sold, ends their eBay listings and books everything to QuickBooks.
The website only has to send the cart to the bot and redirect.

> **Ready-made files:** `website-kit/` in this repo (server route, button,
> success page, README). The site's Content-Security-Policy only allows the
> browser to talk to the site itself, so use the kit's server route
> (`/api/checkout`) rather than calling the bot from the browser.

## 1. The Checkout button

Replace the demo checkout (and the "Purchases are completed on eBay" copy)
with a call to the bot. Send each cart item's product slug as-is - e.g.
`ebay-318921853363-pallet-1-item-100` - or just its `pallet-N-item-M` part.
Don't send prices: the bot charges its own price.

```tsx
// e.g. in src/components/storefront/cart (client component)
async function checkout(cartItems: { slug: string }[]) {
  const res = await fetch("https://dash.4guys1palletoverstock.com/shop/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: cartItems.map((i) => i.slug) }),
  });
  const data = await res.json();
  if (res.ok && data.url) {
    window.location.href = data.url; // Stripe's hosted checkout page
  } else {
    alert(data.error ?? "Checkout isn't available right now - please try again.");
  }
}
```

- The call is allowed from `https://4guys1palletoverstock.com` (the bot's
  `STRIPE_SHOP_ORIGINS`). Calling it from the Next.js server instead (an API
  route) also works and needs no CORS.
- Errors come back as `{"error": "..."}` with a friendly message to show,
  e.g. "Sorry - "Ceiling Light Fixture" just sold." (HTTP 409) or "Someone
  is checking out ... right now" (the item is held by another buyer for up
  to 30 minutes). Remove sold items from the cart when that happens.
- Up to 20 items per checkout; each item is one of a kind (quantity 1).

## 2. Pages Stripe sends the buyer back to

- **`/checkout/success`** - Stripe adds `?session_id=cs_...`. Show "Thanks -
  your order is paid. We'll email you when it ships." and clear the cart.
  (Don't treat this page as proof of payment; the bot is told separately and
  securely by Stripe.)
- **`/cart`** - where "Back" on Stripe's page returns to. Leave the cart as is.

Both addresses can be changed in the bot (`STRIPE_SUCCESS_URL`,
`STRIPE_CANCEL_URL`).

## 3. Prices and stock

- The bot charges the item's listing price as saved in the bot. If the site
  shows a different price (e.g. the eBay price was changed by hand in Seller
  Hub), the buyer pays the bot's price on Stripe's page - keep them in sync.
- Only items that are **Listed** in the bot can be bought. Items that sold,
  or are on hold, are refused.

## 4. Testing

The bot's Stripe keys are in test mode until the owners switch them. Use
card `4242 4242 4242 4242`, any future expiry, any CVC, and a US address.
Nothing is charged in test mode.

## 5. `website-prices-v1` - the website's own prices (server-to-server only)

When the website's **server** wants its own prices charged (read from its
own database, never from the shopper), it calls `/shop/checkout` with the
shared key:

```
POST /shop/checkout
X-Shop-Key: <shared key = bot's STRIPE_SHOP_SERVER_KEY>
X-Client-IP: <shopper's IP, for per-shopper rate limits>

{"items": ["pallet-1-item-96"],
 "checkout_contract": "website-prices-v1",
 "website_prices": {"pallet-1-item-96": 2200}}
```

- Without a valid `X-Shop-Key` the request is refused (403) before
  anything reaches Stripe. `website_prices` without the contract name, or
  an unknown contract, is 400.
- `website_prices` must name exactly the requested items, each a positive
  whole number of USD cents (max 2,000,000). Listed / held / sold /
  one-of-each checks still apply (409 with a message otherwise).
- The Stripe line items use those amounts. The answer is built from what
  was sent to Stripe and checked against Stripe's own `amount_subtotal` -
  if they ever differ the session is expired and 409 returned:

```json
{"url": "https://checkout.stripe.com/g/pay/cs_live_...",
 "checkout_contract": "website-prices-v1", "currency": "usd",
 "subtotal_cents": 2200, "website_prices": {"pallet-1-item-96": 2200},
 "session_id": "cs_live_...", "expires_at": "2026-..."}
```

Shipping and tax are added by Stripe on its page (free shipping, Stripe Tax).

## 6. Order status (optional)

```
GET /shop/session/{session_id}
X-Shop-Key: <shared key>
```

→ `{"session_id", "state": "open|processing|paid|expired|failed|refund_pending",
"paid": true|false, "items": ["pallet-1-item-96"], "amount_total_cents": 2354}`.
`paid` turns true once the bot has received Stripe's signed payment
notification - safe to use for clearing the cart / showing "paid". 404 for
an unknown session, 403 without the key.

## 7. Testing without touching real stock

Use the bot's **test** keys (`sk_test_...` and a test-mode webhook secret)
while validating. Make a throwaway item in a test pallet, mark it **Listed
(Other)** so it has no eBay listing, give it a price, and buy it with card
4242 4242 4242 4242. Afterwards `/item delete` it in Discord.
