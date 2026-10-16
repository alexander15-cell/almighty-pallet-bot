"use client";

// Empties the cart once the buyer lands on the success page.
// TODO(Jacob): swap in the real cart hook from
// src/components/storefront/cart-provider.tsx - e.g.
//   const { clear } = useCart();
//   useEffect(() => { clear(); }, [clear]);

import { useEffect } from "react";

export function ClearCart() {
  useEffect(() => {
    // Placeholder until it's wired to the cart provider:
    // if the cart is kept in localStorage, clearing that key works too.
  }, []);
  return null;
}
