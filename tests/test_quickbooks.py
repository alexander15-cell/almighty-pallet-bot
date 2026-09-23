"""
Unit tests for quickbooks.py's OAuth flow and API client, all with aiohttp
mocked out (no real network calls) via a tiny fake ClientSession. Covers:
token exchange/refresh persistence (including refresh_token rotation),
proactive refresh-before-expiry, the redirect-URL parser, and request/
response shaping for the three API calls the bot actually uses.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import config
import quickbooks as qb


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
    """Stands in for aiohttp.ClientSession - `responder(method, url, kwargs)`
    returns the (status, payload) tuple for any given call, and every call
    is recorded in `calls` for assertions."""

    def __init__(self, responder, calls):
        self._responder = responder
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, auth=None, headers=None):
        self._calls.append({"method": "POST", "url": url, "data": data, "headers": headers})
        status, payload = self._responder("POST", url, data)
        return _FakeResponse(status, payload)

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        status, payload = self._responder(method, url, params or json)
        return _FakeResponse(status, payload)


def _install_fake_session(monkeypatch, responder):
    calls = []

    def factory(*args, **kwargs):
        return _FakeSession(responder, calls)

    monkeypatch.setattr(qb.aiohttp, "ClientSession", factory)
    # BasicAuth (used for the token endpoint) requires non-None credentials -
    # a real deployment always has these set (see config.QUICKBOOKS_ENABLED),
    # but the test .env doesn't configure a QuickBooks app.
    monkeypatch.setattr(config, "QUICKBOOKS_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(config, "QUICKBOOKS_CLIENT_SECRET", "test-client-secret")
    return calls


def _connected(fresh_db, monkeypatch=None, expires_in=timedelta(minutes=30)):
    fresh_db.save_quickbooks_connection(
        realm_id="12345",
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        access_token_expires_at=(datetime.now(timezone.utc) + expires_in).isoformat(),
        actor_id=1,
    )


# --------------------------------------------------------------- URL helpers


def test_build_authorization_url_includes_required_params():
    url = qb.build_authorization_url(state="abc123")
    assert url.startswith(qb.AUTHORIZATION_BASE_URL)
    assert "state=abc123" in url
    assert "response_type=code" in url
    assert "scope=com.intuit.quickbooks.accounting" in url


def test_parse_redirect_url_extracts_code_and_realm_id():
    url = "http://localhost:8000/callback?code=XYZ&realmId=999&state=abc"
    result = qb.parse_redirect_url(url, expected_state="abc")
    assert result == {"code": "XYZ", "realm_id": "999"}


def test_parse_redirect_url_rejects_state_mismatch():
    url = "http://localhost:8000/callback?code=XYZ&realmId=999&state=wrong"
    with pytest.raises(qb.QuickBooksError, match="doesn't match"):
        qb.parse_redirect_url(url, expected_state="abc")


def test_parse_redirect_url_rejects_missing_code():
    url = "http://localhost:8000/callback?realmId=999"
    with pytest.raises(qb.QuickBooksError, match="Couldn't find"):
        qb.parse_redirect_url(url)


def test_parse_redirect_url_surfaces_oauth_error():
    url = "http://localhost:8000/callback?error=access_denied"
    with pytest.raises(qb.QuickBooksError, match="access_denied"):
        qb.parse_redirect_url(url)


# ------------------------------------------------------------- token exchange


def test_exchange_code_for_tokens_persists_connection(fresh_db, monkeypatch):
    def responder(method, url, body):
        assert url == qb.TOKEN_URL
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "the-code"
        return 200, {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    _install_fake_session(monkeypatch, responder)

    result = asyncio.run(qb.exchange_code_for_tokens("the-code", "555", actor_id=42))
    assert result["realm_id"] == "555"
    assert result["access_token"] == "new-access"
    assert result["refresh_token"] == "new-refresh"

    stored = fresh_db.get_quickbooks_connection()
    assert stored["access_token"] == "new-access"
    assert stored["connected_by"] == 42


def test_exchange_code_for_tokens_raises_on_error_response(fresh_db, monkeypatch):
    def responder(method, url, body):
        return 400, {"error": "invalid_grant", "error_description": "bad code"}

    _install_fake_session(monkeypatch, responder)

    with pytest.raises(qb.QuickBooksError, match="bad code"):
        asyncio.run(qb.exchange_code_for_tokens("bad-code", "555"))


# ------------------------------------------------------------- access tokens


def test_get_access_token_without_connection_raises(fresh_db):
    with pytest.raises(qb.QuickBooksNotConnected):
        asyncio.run(qb.get_access_token())


def test_get_access_token_returns_cached_token_when_not_near_expiry(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch, expires_in=timedelta(minutes=30))

    def responder(method, url, body):
        raise AssertionError("should not have made a refresh call")

    _install_fake_session(monkeypatch, responder)

    token = asyncio.run(qb.get_access_token())
    assert token == "old-access-token"


def test_get_access_token_refreshes_when_within_safety_margin(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch, expires_in=timedelta(minutes=2))

    def responder(method, url, body):
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-refresh-token"
        return 200, {"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 3600}

    _install_fake_session(monkeypatch, responder)

    token = asyncio.run(qb.get_access_token())
    assert token == "rotated-access"

    stored = fresh_db.get_quickbooks_connection()
    assert stored["access_token"] == "rotated-access"
    assert stored["refresh_token"] == "rotated-refresh"


def test_get_access_token_falls_back_to_existing_refresh_token_if_omitted(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch, expires_in=timedelta(minutes=0))

    def responder(method, url, body):
        return 200, {"access_token": "rotated-access", "expires_in": 3600}

    _install_fake_session(monkeypatch, responder)

    asyncio.run(qb.get_access_token())
    stored = fresh_db.get_quickbooks_connection()
    assert stored["refresh_token"] == "old-refresh-token"


# ------------------------------------------------------------------ API calls


def test_get_account_balance(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)

    def responder(method, url, params):
        assert "/v3/company/12345/query" in url
        return 200, {"QueryResponse": {"Account": [{"Id": "77", "Name": "Business Card", "CurrentBalance": 123.45}]}}

    _install_fake_session(monkeypatch, responder)

    result = asyncio.run(qb.get_account_balance("77"))
    assert result == {"id": "77", "name": "Business Card", "balance": 123.45}


def test_get_account_balance_raises_when_account_missing(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)

    def responder(method, url, params):
        return 200, {"QueryResponse": {}}

    _install_fake_session(monkeypatch, responder)

    with pytest.raises(qb.QuickBooksError, match="No QuickBooks account"):
        asyncio.run(qb.get_account_balance("missing"))


def test_list_recent_transactions_parses_purchase_rows(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)

    def responder(method, url, params):
        return 200, {"QueryResponse": {"Purchase": [
            {"Id": "1", "TxnDate": "2026-09-20", "TotalAmt": 42.5, "EntityRef": {"name": "Home Depot"}},
            {"Id": "2", "TxnDate": "2026-09-21", "TotalAmt": 15.0},
        ]}}

    _install_fake_session(monkeypatch, responder)

    transactions = asyncio.run(qb.list_recent_transactions("77"))
    assert transactions[0] == {"id": "1", "date": "2026-09-20", "amount": 42.5, "merchant": "Home Depot", "account_id": "77"}
    assert transactions[1]["merchant"] == "Unknown merchant"


def test_list_recent_transactions_without_connection_raises(fresh_db):
    with pytest.raises(qb.QuickBooksNotConnected):
        asyncio.run(qb.list_recent_transactions("77"))


def test_create_expense_builds_purchase_payload_and_returns_id(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)
    calls = []

    def responder(method, url, body):
        assert method == "POST"
        assert "/v3/company/12345/purchase" in url
        assert body["AccountRef"] == {"value": "77"}
        assert body["Line"][0]["Amount"] == 88.0
        assert body["PrivateNote"] == "Pallet #12 charge"
        calls.append(body)
        return 200, {"Purchase": {"Id": "999"}}

    _install_fake_session(monkeypatch, responder)

    result = asyncio.run(qb.create_expense("77", 88.0, "2026-09-22", "Pallet #12 charge"))
    assert result == {"id": "999"}
    assert len(calls) == 1


def test_create_expense_raises_on_api_fault(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)

    def responder(method, url, body):
        return 400, {"Fault": {"Error": [{"Message": "Invalid Reference"}]}}

    _install_fake_session(monkeypatch, responder)

    with pytest.raises(qb.QuickBooksError, match="Invalid Reference"):
        asyncio.run(qb.create_expense("77", 10.0, "2026-09-22", "memo"))


def test_get_transaction_returns_merchant_and_amount(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)

    def responder(method, url, params):
        return 200, {"QueryResponse": {"Purchase": [
            {"Id": "t9", "TxnDate": "2026-09-18", "TotalAmt": 12.34, "EntityRef": {"name": "Uline"}}
        ]}}

    _install_fake_session(monkeypatch, responder)

    result = asyncio.run(qb.get_transaction("t9"))
    assert result == {"id": "t9", "date": "2026-09-18", "amount": 12.34, "merchant": "Uline"}


def test_get_transaction_raises_when_not_found(fresh_db, monkeypatch):
    _connected(fresh_db, monkeypatch)

    def responder(method, url, params):
        return 200, {"QueryResponse": {}}

    _install_fake_session(monkeypatch, responder)

    with pytest.raises(qb.QuickBooksError, match="not found"):
        asyncio.run(qb.get_transaction("missing"))


# ------------------------------------------------------------- state helpers


def test_is_connected_reflects_stored_connection(fresh_db):
    assert qb.is_connected() is False
    fresh_db.save_quickbooks_connection(
        realm_id="1", access_token="a", refresh_token="r",
        access_token_expires_at=datetime.now(timezone.utc).isoformat(),
    )
    assert qb.is_connected() is True
