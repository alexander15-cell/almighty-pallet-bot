// /checkout/success - where Stripe sends the buyer after paying
// (Stripe adds ?session_id=cs_...). This page is just a thank-you: the bot
// learns about the payment directly and securely from Stripe, so nothing
// here should be treated as proof of payment.

import Link from "next/link";
import { ClearCart } from "./clear-cart";

export const metadata = { title: "Thank you - 4 Guys 1 Pallet Overstock" };

export default function CheckoutSuccessPage() {
  return (
    <main className="checkout-success">
      <ClearCart />
      <h1>Thank you - your order is paid!</h1>
      <p>
        Stripe has emailed you a receipt. We&apos;ll pack your order and ship it out (free shipping) - you&apos;ll get
        tracking once it&apos;s on its way.
      </p>
      <p>
        Questions? Reply to your receipt email or contact us through the site.
      </p>
      <p>
        <Link href="/shop">Keep shopping</Link>
      </p>
    </main>
  );
}
