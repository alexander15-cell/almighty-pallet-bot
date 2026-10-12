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
    """The access token caches are module-level state - reset them before
    and after every test so one test's cached token(s) can't leak into
    another's."""
    ebay_api._cached_access_token = None
    ebay_api._cached_expires_at = None
    ebay_api._cached_app_token = None
    ebay_api._cached_app_token_expires_at = None
    yield
    ebay_api._cached_access_token = None
    ebay_api._cached_expires_at = None
    ebay_api._cached_app_token = None
    ebay_api._cached_app_token_expires_at = None


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


def test_get_access_token_does_not_narrow_the_scope(monkeypatch):
    """Regression test: this grant must NOT send a `scope` field - doing
    so silently narrows the minted access token to only that scope no
    matter how wide EBAY_REFRESH_TOKEN's own granted scope actually is,
    which is exactly what caused get_orders() to fail with a
    permission-shaped 403 even once the refresh token genuinely carried
    sell.fulfillment."""
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "tok-1", "expires_in": 7200}))
    asyncio.run(ebay_api.get_access_token())
    assert "scope" not in calls[0]["data"]


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


# --------------------------------------------------------------- get_app_access_token


def test_get_app_access_token_uses_client_credentials_grant(monkeypatch):
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "app-tok-1", "expires_in": 7200}))
    token = asyncio.run(ebay_api.get_app_access_token())
    assert token == "app-tok-1"
    assert len(calls) == 1
    assert calls[0]["data"]["grant_type"] == "client_credentials"
    assert calls[0]["data"]["scope"] == "https://api.ebay.com/oauth/api_scope"
    assert "refresh_token" not in calls[0]["data"]


def test_get_app_access_token_reuses_a_still_valid_cached_token(monkeypatch):
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "app-tok-1", "expires_in": 7200}))
    asyncio.run(ebay_api.get_app_access_token())
    asyncio.run(ebay_api.get_app_access_token())
    assert len(calls) == 1


def test_app_token_cache_is_independent_of_user_token_cache(monkeypatch):
    """Fetching the application token shouldn't populate (or be satisfied
    by) the separate user-token cache get_access_token() uses, since
    they're different grants with different scopes."""
    calls = _install_fake_session(monkeypatch, lambda m, u, d: (200, {"access_token": "app-tok-1", "expires_in": 7200}))
    asyncio.run(ebay_api.get_app_access_token())
    assert ebay_api._cached_access_token is None
    asyncio.run(ebay_api.get_access_token())
    assert len(calls) == 2  # each grant type fetched its own token


def test_get_app_access_token_raises_on_error_response(monkeypatch):
    _install_fake_session(monkeypatch, lambda m, u, d: (400, {"error": "invalid_client", "error_description": "bad keys"}))
    with pytest.raises(ebay_api.EbayAPIError, match="bad keys"):
        asyncio.run(ebay_api.get_app_access_token())


# --------------------------------------------------------------- get_condition_policies / resolve_condition_id_with_live_check


def _condition_policy_responder(conditions):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "app-tok-1", "expires_in": 7200}
        if method == "GET" and "/item_condition_policies" in url:
            return 200, {
                "itemConditionPolicies": [
                    {"categoryId": "12345", "itemConditions": [{"conditionId": c} for c in conditions]},
                ],
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    return responder


def test_get_condition_policies_returns_the_live_allowed_set(monkeypatch):
    _install_fake_session(monkeypatch, _condition_policy_responder(["1000", "1500"]))
    allowed = asyncio.run(ebay_api.get_condition_policies("12345"))
    assert allowed == {"1000", "1500"}


def test_get_condition_policies_raises_when_category_has_no_match(monkeypatch):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "app-tok-1", "expires_in": 7200}
        return 200, {"itemConditionPolicies": []}

    _install_fake_session(monkeypatch, responder)
    with pytest.raises(ebay_api.EbayAPIError, match="no item condition policy"):
        asyncio.run(ebay_api.get_condition_policies("12345"))


def test_resolve_condition_id_with_live_check_accepts_a_live_allowed_condition(monkeypatch):
    """1500 isn't in the static EBAY_BROADLY_ACCEPTED_CONDITION_IDS safe-set,
    but the live lookup here says it IS valid for this category - the live
    check should win over the static fallback."""
    _install_fake_session(monkeypatch, _condition_policy_responder(["1000", "1500"]))
    resolved = asyncio.run(ebay_api.resolve_condition_id_with_live_check("12345", "1500", "test-item"))
    assert resolved == "1500"


def test_resolve_condition_id_with_live_check_falls_back_within_the_live_allowed_set(monkeypatch):
    """1750 isn't allowed per the live lookup for this category, but 3000
    (the usual static fallback) is - should fall back to 3000."""
    _install_fake_session(monkeypatch, _condition_policy_responder(["3000", "7000"]))
    resolved = asyncio.run(ebay_api.resolve_condition_id_with_live_check("12345", "1750", "test-item"))
    assert resolved == "3000"


def test_resolve_condition_id_with_live_check_falls_back_to_a_different_allowed_id_when_the_usual_fallback_isnt_live_allowed(monkeypatch):
    """Neither the requested condition (1750) nor the usual static fallback
    (3000) is in the live-allowed set for this category - should still pick
    something this bot knows about (1000) rather than giving up."""
    _install_fake_session(monkeypatch, _condition_policy_responder(["1000", "7000"]))
    resolved = asyncio.run(ebay_api.resolve_condition_id_with_live_check("12345", "1750", "test-item"))
    assert resolved == "1000"


def test_resolve_condition_id_with_live_check_falls_back_to_static_check_on_lookup_failure(monkeypatch):
    """The live lookup errors entirely (e.g. eBay 500s) - should fall back
    to exactly ebay_csv.resolved_condition_id's static behavior rather than
    raising or hard-failing the listing."""
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "app-tok-1", "expires_in": 7200}
        return 500, {"errors": [{"message": "Internal error"}]}

    _install_fake_session(monkeypatch, responder)
    resolved = asyncio.run(ebay_api.resolve_condition_id_with_live_check("12345", "1750", "test-item"))
    assert resolved == "3000"


def test_resolve_condition_id_with_live_check_falls_back_to_static_check_when_no_conditions_returned(monkeypatch):
    _install_fake_session(monkeypatch, _condition_policy_responder([]))
    resolved = asyncio.run(ebay_api.resolve_condition_id_with_live_check("12345", "1750", "test-item"))
    assert resolved == "3000"


def test_resolve_condition_id_with_live_check_accepts_a_statically_safe_condition_as_is(monkeypatch):
    _install_fake_session(monkeypatch, _condition_policy_responder(["1000", "3000"]))
    resolved = asyncio.run(ebay_api.resolve_condition_id_with_live_check("12345", "1000", "test-item"))
    assert resolved == "1000"


# --------------------------------------------------------------- search_categories


def _category_suggestions_responder(suggestions):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "app-tok-1", "expires_in": 7200}
        if method == "GET" and "/get_category_suggestions" in url:
            return 200, {"categorySuggestions": suggestions}
        raise AssertionError(f"unexpected call: {method} {url}")

    return responder


def test_search_categories_returns_live_results_root_first(monkeypatch):
    suggestions = [
        {
            "category": {"categoryId": "176937", "categoryName": "Ceiling Fans"},
            "categoryTreeNodeAncestors": [
                {"categoryName": "Lamps, Lighting & Ceiling Fans"},
                {"categoryName": "Home & Garden"},
            ],
        },
    ]
    _install_fake_session(monkeypatch, _category_suggestions_responder(suggestions))
    results = asyncio.run(ebay_api.search_categories("ceiling fan"))
    assert results == [("176937", "Home & Garden > Lamps, Lighting & Ceiling Fans > Ceiling Fans")]


def test_search_categories_handles_a_top_level_category_with_no_ancestors(monkeypatch):
    suggestions = [{"category": {"categoryId": "1", "categoryName": "Antiques"}, "categoryTreeNodeAncestors": []}]
    _install_fake_session(monkeypatch, _category_suggestions_responder(suggestions))
    results = asyncio.run(ebay_api.search_categories("antique"))
    assert results == [("1", "Antiques")]


def test_search_categories_respects_the_limit(monkeypatch):
    suggestions = [
        {"category": {"categoryId": str(i), "categoryName": f"Category {i}"}, "categoryTreeNodeAncestors": []}
        for i in range(5)
    ]
    _install_fake_session(monkeypatch, _category_suggestions_responder(suggestions))
    results = asyncio.run(ebay_api.search_categories("widget", limit=2))
    assert len(results) == 2


def test_search_categories_empty_query_returns_empty_list_without_a_network_call(monkeypatch):
    calls = _install_fake_session(monkeypatch, _category_suggestions_responder([]))
    assert asyncio.run(ebay_api.search_categories("")) == []
    assert asyncio.run(ebay_api.search_categories("   ")) == []
    assert calls == []


def test_search_categories_falls_back_to_local_taxonomy_on_api_error(monkeypatch):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "app-tok-1", "expires_in": 7200}
        return 500, {"errors": [{"message": "Internal error"}]}

    _install_fake_session(monkeypatch, responder)
    sentinel = [("999", "Fallback > Category")]
    monkeypatch.setattr(ebay_api.ebay_taxonomy, "search", lambda query, limit=10: sentinel)
    results = asyncio.run(ebay_api.search_categories("widget"))
    assert results == sentinel


def test_search_categories_falls_back_to_local_taxonomy_on_empty_live_result(monkeypatch):
    _install_fake_session(monkeypatch, _category_suggestions_responder([]))
    sentinel = [("999", "Fallback > Category")]
    monkeypatch.setattr(ebay_api.ebay_taxonomy, "search", lambda query, limit=10: sentinel)
    results = asyncio.run(ebay_api.search_categories("widget"))
    assert results == sentinel


def test_search_categories_skips_suggestions_missing_an_id_or_name(monkeypatch):
    suggestions = [
        {"category": {"categoryId": "", "categoryName": "No ID"}, "categoryTreeNodeAncestors": []},
        {"category": {"categoryId": "2", "categoryName": ""}, "categoryTreeNodeAncestors": []},
        {"category": {"categoryId": "3", "categoryName": "Valid"}, "categoryTreeNodeAncestors": []},
    ]
    _install_fake_session(monkeypatch, _category_suggestions_responder(suggestions))
    results = asyncio.run(ebay_api.search_categories("widget"))
    assert results == [("3", "Valid")]


# --------------------------------------------------------------- get_orders


def _orders_responder(orders):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "tok-1", "expires_in": 7200}
        if method == "GET" and "/sell/fulfillment/v1/order" in url:
            return 200, {"orders": orders}
        raise AssertionError(f"unexpected call: {method} {url}")

    return responder


def _since():
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_get_orders_parses_the_minimal_shape_needed(monkeypatch):
    raw_orders = [
        {
            "orderId": "12-34567-89012",
            "creationDate": "2026-02-03T15:22:10.000Z",
            "pricingSummary": {"total": {"value": "25.50", "currency": "USD"}},
            "lineItems": [{"sku": "pallet-5-item-3", "quantity": 1}],
        },
    ]
    _install_fake_session(monkeypatch, _orders_responder(raw_orders))
    orders = asyncio.run(ebay_api.get_orders(_since()))
    assert orders == [{
        "order_id": "12-34567-89012",
        "sale_date": "2026-02-03",
        "total_price": 25.50,
        "line_items": [{"sku": "pallet-5-item-3", "quantity": 1}],
    }]


def test_get_orders_sends_the_refresh_token_grant_not_the_app_token(monkeypatch):
    calls = _install_fake_session(monkeypatch, _orders_responder([]))
    asyncio.run(ebay_api.get_orders(_since()))
    token_call = next(c for c in calls if "oauth2/token" in c["url"])
    assert token_call["data"]["grant_type"] == "refresh_token"


def test_get_orders_sends_the_marketplace_id_header(monkeypatch):
    """Fulfillment API rejects a request missing this header with a
    permission-shaped 403 even with a correctly-scoped token - confirmed
    against a real account, not just eBay's docs."""
    calls = _install_fake_session(monkeypatch, _orders_responder([]))
    asyncio.run(ebay_api.get_orders(_since()))
    order_call = next(c for c in calls if "oauth2/token" not in c["url"])
    assert order_call["headers"]["X-EBAY-C-MARKETPLACE-ID"] == config.EBAY_MARKETPLACE_ID


def test_get_orders_skips_an_order_with_no_total(monkeypatch):
    raw_orders = [
        {"orderId": "1", "creationDate": "2026-02-03T15:22:10.000Z",
         "pricingSummary": {}, "lineItems": [{"sku": "pallet-5-item-3", "quantity": 1}]},
    ]
    _install_fake_session(monkeypatch, _orders_responder(raw_orders))
    assert asyncio.run(ebay_api.get_orders(_since())) == []


def test_get_orders_skips_an_order_with_no_sku_bearing_line_items(monkeypatch):
    raw_orders = [
        {"orderId": "1", "creationDate": "2026-02-03T15:22:10.000Z",
         "pricingSummary": {"total": {"value": "10.00"}}, "lineItems": [{"quantity": 1}]},
    ]
    _install_fake_session(monkeypatch, _orders_responder(raw_orders))
    assert asyncio.run(ebay_api.get_orders(_since())) == []


def test_get_orders_skips_an_order_with_no_order_id(monkeypatch):
    raw_orders = [
        {"creationDate": "2026-02-03T15:22:10.000Z",
         "pricingSummary": {"total": {"value": "10.00"}}, "lineItems": [{"sku": "pallet-1-item-1"}]},
    ]
    _install_fake_session(monkeypatch, _orders_responder(raw_orders))
    assert asyncio.run(ebay_api.get_orders(_since())) == []


def test_get_orders_handles_multiple_line_items_and_quantity_default(monkeypatch):
    raw_orders = [
        {
            "orderId": "1", "creationDate": "2026-02-03T15:22:10.000Z",
            "pricingSummary": {"total": {"value": "40.00"}},
            "lineItems": [{"sku": "pallet-1-item-1"}, {"sku": "pallet-1-item-2", "quantity": 3}],
        },
    ]
    _install_fake_session(monkeypatch, _orders_responder(raw_orders))
    orders = asyncio.run(ebay_api.get_orders(_since()))
    assert orders[0]["line_items"] == [
        {"sku": "pallet-1-item-1", "quantity": 1},
        {"sku": "pallet-1-item-2", "quantity": 3},
    ]


def test_get_orders_raises_ebay_api_error_on_failure(monkeypatch):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "tok-1", "expires_in": 7200}
        return 500, {"errors": [{"message": "Internal error"}]}

    _install_fake_session(monkeypatch, responder)
    with pytest.raises(ebay_api.EbayAPIError, match="Internal error"):
        asyncio.run(ebay_api.get_orders(_since()))


def test_get_orders_empty_result(monkeypatch):
    _install_fake_session(monkeypatch, _orders_responder([]))
    assert asyncio.run(ebay_api.get_orders(_since())) == []


# --------------------------------------------------------------- get_order


def _single_order_responder(raw_order):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "tok-1", "expires_in": 7200}
        if method == "GET" and "/sell/fulfillment/v1/order/" in url:
            return 200, raw_order
        raise AssertionError(f"unexpected call: {method} {url}")

    return responder


def test_get_order_parses_a_single_order(monkeypatch):
    raw_order = {
        "orderId": "12-34567-89012",
        "creationDate": "2026-02-03T15:22:10.000Z",
        "pricingSummary": {"total": {"value": "25.50"}},
        "lineItems": [{"sku": "pallet-5-item-3", "quantity": 1}],
    }
    _install_fake_session(monkeypatch, _single_order_responder(raw_order))
    order = asyncio.run(ebay_api.get_order("12-34567-89012"))
    assert order == {
        "order_id": "12-34567-89012",
        "sale_date": "2026-02-03",
        "total_price": 25.50,
        "line_items": [{"sku": "pallet-5-item-3", "quantity": 1}],
    }


def test_get_order_includes_the_order_id_in_the_url(monkeypatch):
    raw_order = {
        "orderId": "12-34567-89012", "creationDate": "2026-02-03T15:22:10.000Z",
        "pricingSummary": {"total": {"value": "10.00"}}, "lineItems": [{"sku": "pallet-1-item-1"}],
    }
    calls = _install_fake_session(monkeypatch, _single_order_responder(raw_order))
    asyncio.run(ebay_api.get_order("12-34567-89012"))
    order_call = next(c for c in calls if "oauth2/token" not in c["url"])
    assert "12-34567-89012" in order_call["url"]


def test_get_order_sends_the_marketplace_id_header(monkeypatch):
    raw_order = {
        "orderId": "1", "creationDate": "2026-02-03T15:22:10.000Z",
        "pricingSummary": {"total": {"value": "10.00"}}, "lineItems": [{"sku": "pallet-1-item-1"}],
    }
    calls = _install_fake_session(monkeypatch, _single_order_responder(raw_order))
    asyncio.run(ebay_api.get_order("1"))
    order_call = next(c for c in calls if "oauth2/token" not in c["url"])
    assert order_call["headers"]["X-EBAY-C-MARKETPLACE-ID"] == config.EBAY_MARKETPLACE_ID


def test_get_order_raises_when_order_has_no_usable_data(monkeypatch):
    raw_order = {"orderId": "1", "pricingSummary": {}, "lineItems": []}
    _install_fake_session(monkeypatch, _single_order_responder(raw_order))
    with pytest.raises(ebay_api.EbayAPIError, match="no usable total"):
        asyncio.run(ebay_api.get_order("1"))


def test_get_order_raises_on_api_failure(monkeypatch):
    def responder(method, url, body):
        if "oauth2/token" in url:
            return 200, {"access_token": "tok-1", "expires_in": 7200}
        return 404, {"errors": [{"message": "Order not found"}]}

    _install_fake_session(monkeypatch, responder)
    with pytest.raises(ebay_api.EbayAPIError, match="Order not found"):
        asyncio.run(ebay_api.get_order("nonexistent"))


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
    if method == "GET" and "/item_condition_policies" in url:
        # No live condition data for these fake test category IDs - this
        # exercises resolve_condition_id_with_live_check's fallback to the
        # same static check the pre-existing tests below assume.
        return 200, {"itemConditionPolicies": []}
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
    methods_and_urls = [
        (c["method"], c["url"]) for c in calls
        if "oauth2/token" not in c["url"] and "item_condition_policies" not in c["url"]
    ]
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
        if method == "GET" and "/item_condition_policies" in url:
            return 200, {"itemConditionPolicies": []}
        if method == "PUT":
            return 200, {}
        if method == "POST" and url.endswith("/offer"):
            return 201, {}  # missing offerId
        raise AssertionError(f"unexpected call: {method} {url}")

    _install_fake_session(monkeypatch, responder)
    with pytest.raises(ebay_api.EbayAPIError, match="offerId"):
        asyncio.run(ebay_api.create_listing(_item(), _listing()))
