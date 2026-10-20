# Website checkout kit (Stripe, via the pallet bot)

> **Superseded (Oct 2026):** the bot's routes now require the shared key on
> every call and use the catalog + `website-prices-v1` contracts in
> `docs/website-checkout.md`. This kit's route predates that - use the doc.

Hey Jacob - this hooks the storefront's cart up to real payments. The pallet
bot does all the Stripe work (checkout page, card/Apple Pay/Google Pay,
Indiana sales tax, holding one-of-a-kind items, marking them sold, ending the
eBay listing, QuickBooks). The site just sends the cart and redirects.

```
Cart "Checkout"  ->  POST /api/checkout (this site's server)
                 ->  POST https://dash.4guys1palletoverstock.com/shop/checkout (the bot)
                 <-  { url: "https://checkout.stripe.com/..." }
browser -> Stripe's page -> paid -> back to /checkout/success
```

It goes through this site's own server (not the browser straight to the
bot) because the site's Content-Security-Policy has `connect-src 'self'`,
and so the shared key stays server-side.

## Files (paths match the repo's `src/` layout)

| File | What it is |
|---|---|
| `src/app/api/checkout/route.ts` | Server route that forwards the cart to the bot. |
| `src/lib/pallet-checkout.ts` | `startCheckout(slugs)` - call it from the cart. |
| `src/components/storefront/checkout-button.tsx` | Ready-made `<CheckoutButton items={...} />`. |
| `src/app/checkout/success/page.tsx` + `clear-cart.tsx` | Thank-you page Stripe returns to. |

## Steps

1. Copy the `src/` files in. If the site already has a `/checkout/success`
   route or a `pallet-checkout` lib, merge rather than overwrite.
2. Add two **server-only** env vars (wherever the site's env lives - not
   `NEXT_PUBLIC_`):
   ```
   PALLET_BOT_URL=https://dash.4guys1palletoverstock.com
   PALLET_BOT_SHOP_KEY=<a long random string>
   ```
   Make the key with e.g. `openssl rand -hex 32`. The SAME value goes in the
   bot's `.env` as `STRIPE_SHOP_SERVER_KEY` (Alex restarts the bot after).
   Don't commit it or post it in chat.
3. In the cart, replace the demo checkout with
   `<CheckoutButton items={cartItems.map((i) => i.slug)} onSoldOut={...} />`,
   where `slug` is what the product URL uses after `/product/` - e.g.
   `ebay-318921853363-pallet-1-item-100`. (Just the `pallet-1-item-100` part
   works too.) Never send prices - the bot charges its own.
   Use `onSoldOut` to drop sold items from the cart / refresh it.
4. In `clear-cart.tsx`, wire the TODO to the cart provider's clear function
   (`src/components/storefront/cart-provider.tsx`).
5. Remove the "Purchases are completed on eBay" copy and the demo-checkout
   banner (`demoCheckoutEnabled` / `prototypeBannerEnabled` in the site
   settings), once a test order works.

## Rules the bot enforces (worth reflecting in the UI)

- Only items **Listed** in the bot can be bought; each is quantity 1.
- When a buyer starts checkout, their items are **held for 30 minutes**.
  Someone else trying gets `409` "Someone is checking out ... right now".
- A sold item gets `409` "Sorry - "..." just sold."
- Max 20 items per checkout. Free shipping, US addresses. Tax is added on
  Stripe's page from the shipping address (7% for Indiana).
- The bot's price wins. If a listing's price was changed by hand on eBay,
  the site may show a different number than Stripe charges - keep the site
  reading prices from the bot's feed.

## Testing (sandbox - nothing is charged)

Card `4242 4242 4242 4242`, any future expiry, any CVC, a US address. Check:
the cart redirects to `checkout.stripe.com`, paying lands on
`/checkout/success`, and in Discord a "Website order paid" message appears
in #ebay-orders and the item moves to Sold. Card `4000 0000 0000 0002`
tests a decline.

Questions about the bot side: `docs/website-checkout.md` in the
almighty-pallet-bot repo.
