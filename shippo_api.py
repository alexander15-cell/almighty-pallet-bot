"""
Shippo - live carrier rates (USPS / UPS / FedEx...) for website orders.

Only rate quotes for now (POST /shipments, synchronous): no labels are
bought here. The token comes only from .env (SHIPPO_API_TOKEN -
shippo_test_... while testing, shippo_live_... for real rates).
"""
import logging

import aiohttp

import config

log = logging.getLogger(__name__)

API_BASE = "https://api.goshippo.com"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class ShippoError(Exception):
    pass


def ship_from() -> dict:
    return {"name": config.SHIP_FROM_NAME, "street1": config.SHIP_FROM_STREET1, "street2": config.SHIP_FROM_STREET2,
            "city": config.SHIP_FROM_CITY, "state": config.SHIP_FROM_STATE, "zip": config.SHIP_FROM_ZIP,
            "country": "US", "phone": config.SHIP_FROM_PHONE}


def ready() -> str:
    """'' when everything needed is set, else what's missing."""
    missing = [name for name, value in (
        ("SHIPPO_API_TOKEN", config.SHIPPO_API_TOKEN), ("SHIP_FROM_STREET1", config.SHIP_FROM_STREET1),
        ("SHIP_FROM_CITY", config.SHIP_FROM_CITY), ("SHIP_FROM_STATE", config.SHIP_FROM_STATE),
        ("SHIP_FROM_ZIP", config.SHIP_FROM_ZIP)) if not value]
    return ", ".join(missing)


async def _call(method: str, path: str, body: dict = None) -> dict:
    if not config.SHIPPO_API_TOKEN:
        raise ShippoError("SHIPPO_API_TOKEN isn't set in .env.")
    headers = {"Authorization": f"ShippoToken {config.SHIPPO_API_TOKEN}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.request(method, f"{API_BASE}{path}", json=body, headers=headers) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ShippoError(f"Couldn't reach Shippo ({type(e).__name__}).")
    if status >= 300:
        raise ShippoError(f"Shippo {method} {path} answered {status}: {str(data)[:300]}")
    return data or {}


async def create_order(order: dict) -> dict:
    return await _call("POST", "/orders/", order)


async def get_order(order_id: str) -> dict:
    return await _call("GET", f"/orders/{order_id}")


async def get_transaction(transaction_id: str) -> dict:
    return await _call("GET", f"/transactions/{transaction_id}")


async def get_rate(rate_id: str) -> dict:
    return await _call("GET", f"/rates/{rate_id}")


async def get_rates(parcel: dict, to_zip: str) -> list:
    """Rates for one parcel {length, width, height (in), weight (lb)} to a
    US ZIP: [{'amount' (dollars), 'provider', 'service', 'days', 'id'}],
    cheapest first. Raises ShippoError."""
    missing = ready()
    if missing:
        raise ShippoError(f"Shippo isn't set up ({missing} missing in .env).")
    body = {
        "address_from": ship_from(),
        "address_to": {"zip": to_zip, "country": "US"},
        "parcels": [{"length": f"{parcel['length']:.1f}", "width": f"{parcel['width']:.1f}",
                     "height": f"{parcel['height']:.1f}", "distance_unit": "in",
                     "weight": f"{parcel['weight']:.2f}", "mass_unit": "lb"}],
        "async": False,
    }
    headers = {"Authorization": f"ShippoToken {config.SHIPPO_API_TOKEN}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(f"{API_BASE}/shipments/", json=body, headers=headers) as resp:
                data = await resp.json(content_type=None)
                status = resp.status
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ShippoError(f"Couldn't reach Shippo ({type(e).__name__}).")
    if status >= 300:
        raise ShippoError(f"Shippo answered {status}: {str(data)[:300]}")
    rates = []
    allowed = {c.lower() for c in config.SHIPPO_CARRIERS}
    for rate in (data or {}).get("rates") or []:
        if (rate.get("currency") or "USD").upper() != "USD" or not rate.get("amount"):
            continue
        provider = rate.get("provider") or ""
        if allowed and provider.lower() not in allowed:
            continue
        rates.append({"amount": float(rate["amount"]), "provider": provider,
                      "service": ((rate.get("servicelevel") or {}).get("name") or ""),
                      "days": rate.get("estimated_days"), "id": rate.get("object_id")})
    if not rates:
        messages = "; ".join(m.get("text", "") for m in (data or {}).get("messages") or [])[:300]
        raise ShippoError("Shippo returned no rates" + (f": {messages}" if messages else "."))
    return sorted(rates, key=lambda r: r["amount"])
