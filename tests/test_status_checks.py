"""
/ebay status (status_checks.py) - read-only health check of every eBay
permission and QuickBooks setting the bot depends on. eBay is faked at the
HTTP level, QuickBooks at quickbooks._request.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import ebay_api
import quickbooks
import status_checks
import cogs.ebay as ebay_cog


class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, responder, calls):
        self._responder, self._calls = responder, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, auth=None, headers=None, json=None):
        return _Resp(200, {"access_token": "tok", "expires_in": 7200})

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append((method, url))
        return _Resp(*self._responder(url))


POLICIES = {
    "fulfillment_policy": {"fulfillmentPolicies": [{"fulfillmentPolicyId": "F1", "name": "Ground"}]},
    "payment_policy": {"paymentPolicies": [{"paymentPolicyId": "P1", "name": "Managed"}]},
    "return_policy": {"returnPolicies": [{"returnPolicyId": "R1", "name": "30 day"}]},
}


def _all_good(url):
    for path, body in POLICIES.items():
        if f"/sell/account/v1/{path}" in url:
            return 200, body
    return 200, {}


@pytest.fixture
def ebay(monkeypatch):
    ebay_api._cached_access_token = ebay_api._cached_app_token = None
    for key, value in {"EBAY_ENABLED": True, "EBAY_APP_ID": "a", "EBAY_CERT_ID": "c", "EBAY_REFRESH_TOKEN": "r",
                       "EBAY_ENVIRONMENT": "production", "EBAY_MERCHANT_LOCATION_KEY": "warehouse",
                       "EBAY_FULFILLMENT_POLICY_ID": "F1", "EBAY_PAYMENT_POLICY_ID": "P1",
                       "EBAY_RETURN_POLICY_ID": "R1"}.items():
        monkeypatch.setattr(config, key, value)
    calls = []

    def install(responder):
        monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _Session(responder, calls))
        return calls
    yield install
    ebay_api._cached_access_token = ebay_api._cached_app_token = None


def _by_label(checks):
    return {c.label: c for c in checks}


def test_everything_working(ebay):
    calls = ebay(_all_good)
    checks = asyncio.run(status_checks.ebay_checks())
    assert all(c.state is True for c in checks), status_checks.format_checks(checks)
    assert _by_label(checks)["Shipping policy"].detail == '`F1` = "Ground"'
    assert all(method == "GET" for method, _ in calls)  # read-only


def test_missing_finances_scope_is_named(ebay):
    ebay(lambda url: (403, {}) if "/sell/finances/" in url else _all_good(url))
    check = _by_label(asyncio.run(status_checks.ebay_checks()))["Fees & payouts (sync-finances, fee cards)"]
    assert check.state is False and "sell.finances" in check.detail


def test_wrong_policy_id_and_missing_location(ebay, monkeypatch):
    monkeypatch.setattr(config, "EBAY_RETURN_POLICY_ID", "R-WRONG")
    ebay(lambda url: (404, {}) if "/location/" in url else _all_good(url))
    checks = _by_label(asyncio.run(status_checks.ebay_checks()))
    assert checks["Return policy"].state is False and "R-WRONG" in checks["Return policy"].detail
    assert checks["Merchant location"].state is False and "not found" in checks["Merchant location"].detail


def test_policies_unverifiable_without_sell_account_is_not_a_failure(ebay):
    ebay(lambda url: (403, {}) if "/sell/account/" in url else _all_good(url))
    check = _by_label(asyncio.run(status_checks.ebay_checks()))["Payment policy"]
    assert check.state is None and "sell.account" in check.detail


def test_ebay_not_configured(monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    checks = asyncio.run(status_checks.ebay_checks())
    assert len(checks) == 1 and checks[0].state is False


@pytest.fixture
def qb(monkeypatch):
    accounts = {"24": ("Undeposited Funds", "Other Current Asset"), "53": ("eBay Fees", "Expense"),
                "99": ("Shipping", "Expense"), "49": ("Sales", "Income")}

    async def fake_request(method, path, *, params=None, json_body=None):
        assert method == "GET"
        account_id = params["query"].split("'")[1]
        if account_id not in accounts:
            return {"QueryResponse": {}}
        name, kind = accounts[account_id]
        return {"QueryResponse": {"Account": [{"Id": account_id, "Name": name, "AccountType": kind, "Active": True}]}}
    monkeypatch.setattr(quickbooks, "_request", fake_request)
    monkeypatch.setattr(quickbooks, "is_connected", lambda: True)
    for key, value in {"QUICKBOOKS_ENABLED": True, "QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID": "24",
                       "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID": "53", "QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID": "99",
                       "QUICKBOOKS_EBAY_ADS_ACCOUNT_ID": "49"}.items():
        monkeypatch.setattr(config, key, value)


def test_quickbooks_accounts_checked_by_type(qb):
    checks = _by_label(asyncio.run(status_checks.quickbooks_checks()))
    assert checks["Undeposited Funds"].state is True
    assert checks["eBay fees account"].state is True and '"eBay Fees"' in checks["eBay fees account"].detail
    ads = checks["eBay ads account"]
    assert ads.state is False and "**Income**" in ads.detail  # an income account would mis-book fees


def test_missing_or_unknown_fee_accounts(qb, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID", "")
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID", "12345")
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_ADS_ACCOUNT_ID", "")
    checks = _by_label(asyncio.run(status_checks.quickbooks_checks()))
    assert checks["eBay fees account"].state is False and "isn't set" in checks["eBay fees account"].detail
    assert checks["eBay shipping account"].state is False and "12345" in checks["eBay shipping account"].detail
    assert checks["eBay ads account"].state is None


def test_quickbooks_not_connected(monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_ENABLED", True)
    monkeypatch.setattr(quickbooks, "is_connected", lambda: False)
    checks = asyncio.run(status_checks.quickbooks_checks())
    assert checks[0].state is False and "connect-quickbooks" in checks[0].detail


def test_status_command_summarizes_problems(monkeypatch):
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: "admin")
    monkeypatch.setattr(ebay_cog.status_checks, "ebay_checks", AsyncMock(return_value=[
        status_checks.Check("Orders", True, "ok"), status_checks.Check("Fees", False, "needs sell.finances")]))
    monkeypatch.setattr(ebay_cog.status_checks, "quickbooks_checks", AsyncMock(return_value=[
        status_checks.Check("QuickBooks", True, "connected")]))
    event = SimpleNamespace(guild=object(), user=SimpleNamespace(roles=["admin"]),
                            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                            followup=SimpleNamespace(send=AsyncMock()))
    cog = ebay_cog.Ebay.__new__(ebay_cog.Ebay)

    asyncio.run(ebay_cog.Ebay.status.callback(cog, event))

    embed = event.followup.send.call_args.kwargs["embed"]
    assert "1 problem" in embed.description
    assert "❌ **Fees** - needs sell.finances" in embed.fields[0].value
