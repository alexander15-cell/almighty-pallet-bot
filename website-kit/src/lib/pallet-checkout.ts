// Client helper: send the cart to /api/checkout and go to Stripe.
//
// `items` = each cart item's product slug, e.g.
// "ebay-318921853363-pallet-1-item-100" (or just "pallet-1-item-100").
// Never send prices - the bot charges its own.

export type CheckoutResult = { ok: true } | { ok: false; error: string; soldOut: boolean };

export async function startCheckout(items: string[]): Promise<CheckoutResult> {
  try {
    const res = await fetch("/api/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.url) {
      window.location.assign(data.url); // Stripe's hosted checkout page
      return { ok: true };
    }
    return {
      ok: false,
      error: data.error ?? "Checkout isn't available right now - please try again.",
      soldOut: res.status === 409, // an item sold or is being bought by someone else
    };
  } catch {
    return { ok: false, error: "Couldn't reach checkout - check your connection and try again.", soldOut: false };
  }
}
