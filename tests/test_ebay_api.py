"""
Unit tests for ebay_api.py's OAuth token refresh and the three-call Sell
Inventory API listing flow, all with aiohttp mocked out (no real network
calls) via the same tiny fake ClientSession pattern as test_quickbooks.py.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import ebay_api


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responder, calls):
        self._responder = responder
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, auth=None, headers=None, json=None):
        self._calls.append({"method": "POST", "url": url, "data": data, "json": json, "headers": headers})
        status, payload = self._responder("POST", url, data if data is not None else json)
        return _FakeResponse(status, payload)

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append({"method": method, "url": url, "json": json, "headers": headers})
        status, payload = self._responder(method, url, json)
        return _FakeResponse(status, payload)


def _install_fake_session(monkeypatch, responder):
    calls = []

    def factory(*args, **kwargs):
        return _FakeSession(responder, calls)

    monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(config, "EBAY_APP_ID", "test-app-id")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "test-cert-id")
    monkeypatch.setattr(config, "EBAY_REFRESH_TOKEN", "test-refresh-token")
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(config, "EBAY_MERCHANT_LOCATION_KEY", "warehouse-1")
    monkeypatch.setattr(config, "EBAY_FULFILLMENT_POLICY_ID", "fulfillment-1")
    monkeypatch.setattr(config, "EBAY_PAYMENT_POLICY_ID", "payment-1")
    monkeypatch.setattr(config, "EBAY_RETURN_POLICY_ID", "return-1")
    return calls


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """The access token cache is module-level state - reset it before and
    after every test so one test's cached token can't leak into another's."""
    ebay_api._cached_access_token = None
    ebay_api._cached_expires_at = None
    yield
    ebay_api._cached_access_token = None
    ebay_api._cached_expires_at = None


def _item(pallet_id=1, item_number=1, photo_urls=None):
    return {
        "id": 42,
        "pallet_id": pallet_id,
        "item_number": item_number,
        "ai_description": "A nice widget. Sold as-is; please review photos closely for exact condition.",
        "raw_description": "a widget",
        "photo_public_urls": json.dumps(photo_urls or ["https://example.com/photo1.jpg"]),
    }


def _listing(**overrides):
    base = {
        "ebay_title": "Nice Widget",
        "category_id": "12345",
        "condition_id": "1500",
        "price": 19.99,
        "listing_format": "FixedPrice",
        "auction_duration": None,
        "item_specifics": {"Brand": "Acme"},
        "weight_lb": 2.5,
        "length_in": 10.0,
        "width_in": 8.0,
        "height_in": 4.0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- get_access_token


def test_get_access_token_requests_a_fresh_token(monkeypatch):
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "tok-1", "expires_in": 7200}))
    token = asyncio.run(ebay_api.get_access_token())
    assert token == "tok-1"
    assert len(calls) == 1
    assert calls[0]["data"]["grant_type"] == "refresh_token"
    assert calls[0]["data"]["refresh_token"] == "test-refresh-token"


def test_get_access_token_reuses_a_still_valid_cached_token(monkeypatch):
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "tok-1", "expires_in": 7200}))
    asyncio.run(ebay_api.get_access_token())
    asyncio.run(ebay_api.get_access_token())
    assert len(calls) == 1  # second call reused the cache, no new HTTP request


def test_get_access_token_refreshes_when_within_safety_margin(monkeypatch):
    ebay_api._cached_access_token = "stale-token"
    ebay_api._cached_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)  # inside the 5-min margin
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "fresh-token", "expires_in": 7200}))

    token = asyncio.run(ebay_api.get_access_token())

    assert token == "fresh-token"
    assert len(calls) == 1


def test_get_access_token_raises_on_error_response(monkeypatch):
    _install_fake_session(monkeypatch, lambda m, u, d: (400, {"error": "invalid_grant", "error_description": "expired"}))
    with pytest.raises(ebay_api.EbayAPIError, match="expired"):
        asyncio.run(ebay_api.get_access_token())


# --------------------------------------------------------------- create_listing


def test_create_listing_raises_not_implemented_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    with pytest.raises(NotImplementedError):
        asyncio.run(ebay_api.create_listing(_item(), _listing()))


def test_create_listing_requires_publish_config(monkeypatch):
    _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "tok", "expires_in": 7200}))
    monkeypatch.setattr(config, "EBAY_MERCHANT_LOCATION_KEY", "")
    with pytest.raises(ebay_api.EbayAPIError, match="EBAY_MERCHANT_LOCATION_KEY"):
        asyncio.run(ebay_api.create_listing(_item(), _listing()))


def _happy_path_responder(method, url, body):
    if "oauth2/token" in url:
        return 200, {"access_token": "tok-1", "expires_in": 7200}
    if method == "PUT" and "/inventory_item/" in url:
        return 200, {}
    if method == "POST" and url.endswith("/offer"):
        return 201, {"offerId": "offer-123"}
    if method == "POST" and "/offer/offer-123/publish" in url:
        return 200, {"listingId": "ebay-item-999"}
    raise AssertionError(f"unexpected call: {method} {url}")


def test_create_listing_happy_path_returns_the_real_ebay_item_id(monkeypatch):
    calls = _install_fake_session(monkeypatch, _happy_path_responder)

    result = asyncio.run(ebay_api.create_listing(_item(), _listing()))

    assert result == {"ebay_item_id": "ebay-item-999"}
    methods_and_urls = [(c["method"], c["url"]) for c in calls if "oauth2/token" not in c["url"]]
    assert methods_and_urls == [
        ("PUT", f"{ebay_api._api_base_url()}/inventory_item/pallet-1-item-1"),
        ("POST", f"{ebay_api._api_base_url()}/offer"),
        ("POST", f"{ebay_api._api_base_url()}/offer/offer-123/publish"),
    ]


def test_create_listing_sends_content_language_header(monkeypatch):
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    asyncio.run(ebay_api.create_listing(_item(), _listing()))
    inventory_call = next(c for c in calls if c["method"] == "PUT")
    assert inventory_call["headers"]["Content-Language"] == "en-US"
    assert inventory_call["headers"]["Authorization"] == "Bearer tok-1"


def test_create_listing_maps_condition_id_to_the_inventory_enum(monkeypatch):
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    asyncio.run(ebay_api.create_listing(_item(), _listing(condition_id="1000")))
    inventory_call = next(c for c in calls if c["method"] == "PUT")
    assert inventory_call["json"]["condition"] == "NEW"


def test_create_listing_falls_back_an_unsafe_condition_before_mapping(monkeypatch):
    """1750 isn't in EBAY_BROADLY_ACCEPTED_CONDITION_IDS, so
    ebay_csv.resolved_condition_id falls it back to "3000" first - the
    enum mapping should reflect that fallback, not 1750's own mapping."""
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    asyncio.run(ebay_api.create_listing(_item(), _listing(condition_id="1750")))
    inventory_call = next(c for c in calls if c["method"] == "PUT")
    assert inventory_call["json"]["condition"] == "USED_EXCELLENT"


def test_create_listing_fills_brand_mpn_type_placeholders(monkeypatch):
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    asyncio.run(ebay_api.create_listing(_item(), _listing(item_specifics={})))
    inventory_call = next(c for c in calls if c["method"] == "PUT")
    aspects = inventory_call["json"]["product"]["aspects"]
    assert aspects["Brand"] == ["Does Not Apply"]
    assert aspects["MPN"] == ["Does Not Apply"]
    assert "Type" in aspects


def test_create_listing_fixed_price_offer_shape(monkeypatch):
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    asyncio.run(ebay_api.create_listing(_item(), _listing(price=25.5)))
    offer_call = next(c for c in calls if c["method"] == "POST" and c["url"].endswith("/offer"))
    body = offer_call["json"]
    assert body["format"] == "FIXED_PRICE"
    assert body["pricingSummary"]["price"] == {"value": "25.50", "currency": "USD"}
    assert body["listingDuration"] == "GTC"
    assert body["merchantLocationKey"] == "warehouse-1"
    assert body["listingPolicies"] == {
        "fulfillmentPolicyId": "fulfillment-1",
        "paymentPolicyId": "payment-1",
        "returnPolicyId": "return-1",
    }


def test_create_listing_auction_offer_shape(monkeypatch):
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    asyncio.run(ebay_api.create_listing(
        _item(), _listing(listing_format="Auction", auction_duration="Days_5", price=9.99),
    ))
    offer_call = next(c for c in calls if c["method"] == "POST" and c["url"].endswith("/offer"))
    body = offer_call["json"]
    assert body["format"] == "AUCTION"
    assert body["pricingSummary"]["auctionStartPrice"] == {"value": "9.99", "currency": "USD"}
    assert body["listingDuration"] == "DAYS_5"


def test_create_listing_uses_the_same_sku_as_the_csv_batch_path(monkeypatch):
    import ebay_csv
    calls = _install_fake_session(monkeypatch, _happy_path_responder)
    item = _item(pallet_id=7, item_number=3)
    asyncio.run(ebay_api.create_listing(item, _listing()))
    offer_call = next(c for c in calls if c["method"] == "POST" and c["url"].endswith("/offer"))
    assert offer_call["json"]["sku"] == ebay_csv.custom_label(item)


def test_create_listing_raises_with_ebay_error_detail_on_failure(monkeypatch):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "tok-1", "expires_in": 7200}
        return 400, {"errors": [{"errorId": 25002, "message": "Invalid category ID."}]}

    _install_fake_session(monkeypatch, responder)
    with pytest.raises(ebay_api.EbayAPIError, match="Invalid category ID"):
        asyncio.run(ebay_api.create_listing(_item(), _listing()))


def test_create_listing_raises_if_offer_has_no_offer_id(monkeypatch):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "tok-1", "expires_in": 7200}
        if method == "PUT":
            return 200, {}
        if method == "POST" and url.endswith("/offer"):
            return 201, {}  # missing offerId
        raise AssertionError(f"unexpected call: {method} {url}")

    _install_fake_session(monkeypatch, responder)
    with pytest.raises(ebay_api.EbayAPIError, match="offerId"):
        asyncio.run(ebay_api.create_listing(_item(), _listing()))
