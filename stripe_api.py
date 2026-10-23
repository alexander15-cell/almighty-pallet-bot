"""
A small Stripe API client (aiohttp, no stripe package needed - same
approach as ebay_api.py / quickbooks.py) for website checkout
(stripe_shop.py): create / read / expire Checkout Sessions, and verify
webhook signatures.

Keys come only from .env (STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET) - never
from code or the database. A restricted key needs: Checkout Sessions write,
PaymentIntents read, Charges read, Balance transactions read.
"""
import hashlib
import hmac
import json
import time

import aiohttp

import config

API_BASE = "https://api.stripe.com/v1"


TIMEOUT = aiohttp.ClientTimeout(total=30)


class StripeError(Exception):
    """A Stripe call failed. ambiguous=True: no answer came back, so Stripe
    may or may not have done it."""

    def __init__(self, message: str = "", ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


def _flatten(data, prefix="") -> list:
    """Nested dicts/lists -> Stripe's form encoding (a[b][0][c]=...)."""
    pairs = []
    if isinstance(data, dict):
        for key, value in data.items():
            pairs += _flatten(value, f"{prefix}[{key}]" if prefix else str(key))
    elif isinstance(data, (list, tuple)):
        for i, value in enumerate(data):
            pairs += _flatten(value, f"{prefix}[{i}]")
    elif isinstance(data, bool):
        pairs.append((prefix, "true" if data else "false"))
    elif data is not None:
        pairs.append((prefix, str(data)))
    return pairs


def is_test_key(key: str) -> bool:
    """A Stripe test-mode secret key: standard (sk_test_) or restricted (rk_test_)."""
    return (key or "").strip().startswith(("sk_test_", "rk_test_"))


def _key(test: bool) -> str:
    """The live key, or (test=True) the separate test key - falling back to
    STRIPE_SECRET_KEY when that itself is a sk_test_ key."""
    if test:
        key = config.STRIPE_TEST_SECRET_KEY or (
            config.STRIPE_SECRET_KEY if is_test_key(config.STRIPE_SECRET_KEY) else "")
        if not key:
            raise StripeError("STRIPE_TEST_SECRET_KEY isn't set in .env (needed for the checkout test item).")
        return key
    if not config.STRIPE_SECRET_KEY:
        raise StripeError("STRIPE_SECRET_KEY isn't set in .env.")
    return config.STRIPE_SECRET_KEY


async def _request(method: str, path: str, params: dict = None, idempotency_key: str = None,
                   test: bool = False) -> dict:
    headers = {"Authorization": f"Bearer {_key(test)}"}
    if config.STRIPE_API_VERSION:
        headers["Stripe-Version"] = config.STRIPE_API_VERSION
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    pairs = _flatten(params or {})
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            if method == "GET":
                async with session.get(f"{API_BASE}{path}", params=pairs, headers=headers) as resp:
                    body = await resp.json(content_type=None)
                    status, request_id = resp.status, resp.headers.get("Request-Id")
            else:
                async with session.request(method, f"{API_BASE}{path}", data=pairs, headers=headers) as resp:
                    body = await resp.json(content_type=None)
                    status, request_id = resp.status, resp.headers.get("Request-Id")
    except (aiohttp.ClientError, TimeoutError) as e:
        # We can't tell whether Stripe acted on it - callers that create
        # things retry with the same Idempotency-Key (see stripe_shop).
        raise StripeError(f"Stripe {method} {path}: no answer ({type(e).__name__})", ambiguous=True)
    if status >= 300:
        err = (body or {}).get("error") or {}
        raise StripeError(f"Stripe {method} {path} failed ({status}, request {request_id or '?'}): "
                          f"{err.get('message') or body}")
    return body


def is_test_session(session_id: str) -> bool:
    return str(session_id).startswith("cs_test_")


async def create_checkout_session(params: dict, idempotency_key: str = None, test: bool = False) -> dict:
    return await _request("POST", "/checkout/sessions", params, idempotency_key=idempotency_key, test=test)


async def retrieve_checkout_session(session_id: str) -> dict:
    """With the payment's charge and balance transaction, so the Stripe fee
    can be read (payment_intent.latest_charge.balance_transaction.fee)."""
    return await _request("GET", f"/checkout/sessions/{session_id}", {
        "expand": ["payment_intent.latest_charge.balance_transaction"]}, test=is_test_session(session_id))


async def expire_checkout_session(session_id: str) -> dict:
    return await _request("POST", f"/checkout/sessions/{session_id}/expire", test=is_test_session(session_id))


def verify_webhook(payload: bytes, signature_header: str, secret: str, tolerance: int = 300, now: int = None) -> dict:
    """
    Checks Stripe's Stripe-Signature header (t=timestamp, v1=HMAC-SHA256 of
    "timestamp.payload" with the endpoint's signing secret) and returns the
    parsed event. Raises StripeError for a missing/wrong/stale signature -
    nothing unverified is ever acted on.
    """
    if not secret:
        raise StripeError("STRIPE_WEBHOOK_SECRET isn't set.")
    parts = {}
    for item in (signature_header or "").split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts.get("t", [""])[0])
    except ValueError:
        raise StripeError("Webhook signature has no timestamp.")
    if abs((now or int(time.time())) - timestamp) > tolerance:
        raise StripeError("Webhook signature is too old.")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise StripeError("Webhook signature doesn't match.")
    try:
        return json.loads(payload)
    except ValueError:
        raise StripeError("Webhook body isn't JSON.")
