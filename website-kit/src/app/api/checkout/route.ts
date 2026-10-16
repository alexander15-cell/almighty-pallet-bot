// POST /api/checkout  { items: string[] }  ->  { url } | { error }
//
// Runs on the website's server. Forwards the cart to the pallet bot, which
// checks each item is still for sale, holds it, and creates the Stripe
// Checkout page. Done server-side because the site's Content-Security-Policy
// (connect-src 'self') stops the browser calling another domain, and so the
// shared key never reaches the browser.
//
// Needs two server-only env vars (NOT prefixed NEXT_PUBLIC_):
//   PALLET_BOT_URL=https://dash.4guys1palletoverstock.com
//   PALLET_BOT_SHOP_KEY=<same value as STRIPE_SHOP_SERVER_KEY in the bot's .env>

import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_ITEMS = 20;

export async function POST(request: Request) {
  const botUrl = process.env.PALLET_BOT_URL;
  const shopKey = process.env.PALLET_BOT_SHOP_KEY;
  if (!botUrl || !shopKey) {
    return NextResponse.json({ error: "Checkout isn't set up yet." }, { status: 503 });
  }

  let items: unknown;
  try {
    items = (await request.json())?.items;
  } catch {
    return NextResponse.json({ error: "Bad request." }, { status: 400 });
  }
  if (
    !Array.isArray(items) ||
    items.length === 0 ||
    items.length > MAX_ITEMS ||
    !items.every((i) => typeof i === "string" && i.length <= 100)
  ) {
    return NextResponse.json({ error: "Your cart looks empty or too big." }, { status: 400 });
  }

  // The shopper's IP, so the bot rate-limits each shopper separately.
  const clientIp =
    request.headers.get("cf-connecting-ip") ??
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ??
    "unknown";

  try {
    const res = await fetch(`${botUrl.replace(/\/$/, "")}/shop/checkout`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Shop-Key": shopKey,
        "X-Client-IP": clientIp,
      },
      body: JSON.stringify({ items }),
      cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok && typeof data.url === "string" && data.url.startsWith("https://checkout.stripe.com/")) {
      return NextResponse.json({ url: data.url });
    }
    // 409 = sold / held by another shopper, 429 = slow down - pass the
    // bot's friendly message through.
    return NextResponse.json(
      { error: typeof data.error === "string" ? data.error : "Checkout isn't available right now." },
      { status: res.status >= 400 && res.status < 500 ? res.status : 502 },
    );
  } catch {
    return NextResponse.json({ error: "Checkout isn't available right now - please try again." }, { status: 502 });
  }
}
