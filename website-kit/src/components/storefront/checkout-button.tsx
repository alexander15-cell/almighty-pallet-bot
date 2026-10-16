"use client";

// Drop-in replacement for the cart's demo checkout button.
//
//   <CheckoutButton items={cart.items.map((i) => i.slug)} />
//
// `items` = the product slugs in the cart (whatever the product page URL
// uses after /product/, e.g. "ebay-318921853363-pallet-1-item-100").

import { useState } from "react";
import { startCheckout } from "@/lib/pallet-checkout";

type Props = {
  items: string[];
  className?: string;
  /** Called when an item in the cart has sold / is being bought - e.g. refresh the cart. */
  onSoldOut?: (message: string) => void;
};

export function CheckoutButton({ items, className, onSoldOut }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleClick() {
    setBusy(true);
    setError(null);
    const result = await startCheckout(items);
    if (!result.ok) {
      setError(result.error);
      if (result.soldOut) onSoldOut?.(result.error);
      setBusy(false);
    }
    // on success the browser is already on its way to Stripe
  }

  return (
    <div>
      <button
        type="button"
        className={className ?? "checkout-button"}
        onClick={handleClick}
        disabled={busy || items.length === 0}
      >
        {busy ? "Starting checkout…" : "Checkout"}
      </button>
      {error && (
        <p role="alert" className="checkout-error">
          {error}
        </p>
      )}
      <p className="checkout-note">Secure payment by Stripe. Sales tax is added at checkout. Free shipping.</p>
    </div>
  );
}
