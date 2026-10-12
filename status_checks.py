"""
/ebay status - one command that checks every eBay permission and QuickBooks
setting the bot depends on, against the live services, and says what works.

Strictly read-only: every check is a GET (or a token refresh); nothing is
created, changed, or listed. Each check returns a Check(label, state,
detail) - state True (works), False (broken - detail says what to fix), or
None (not applicable / not configured, e.g. no separate ads account set).
"""
import asyncio
import urllib.parse
from dataclasses import dataclass

import aiohttp

import config
import ebay_api
import ebay_finances
import quickbooks

_TIMEOUT_SECONDS = 15

# QuickBooks AccountType values that make sense as the target of an expense
# debit (the eBay fee Journal Entries) / as Undeposited Funds.
EXPENSE_TYPES = {"Expense", "Other Expense", "Cost of Goods Sold"}
UNDEPOSITED_TYPES = {"Other Current Asset"}


@dataclass
class Check:
    label: str
    state: object  # True / False / None
    detail: str = ""


def _account_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/sell/account/v1"
    return "https://api.ebay.com/sell/account/v1"


async def _get(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE_ID}
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers=headers) as resp:
            return resp.status, await resp.json(content_type=None)


def _explain(status: int, scope: str) -> str:
    if status in (401, 403):
        return f"permission denied ({status}) - EBAY_REFRESH_TOKEN needs the `{scope}` scope (redo the eBay sign-in)"
    return f"eBay returned {status}"


async def _scope_check(label: str, url: str, token: str, scope: str) -> Check:
    try:
        status, _ = await asyncio.wait_for(_get(url, token), timeout=_TIMEOUT_SECONDS)
    except Exception as e:
        return Check(label, False, f"couldn't reach eBay ({e!r})")
    return Check(label, True, f"`{scope}` OK") if status < 300 else Check(label, False, _explain(status, scope))


async def ebay_checks() -> list:
    if not config.EBAY_ENABLED:
        return [Check("eBay API", False, "EBAY_APP_ID / EBAY_CERT_ID / EBAY_REFRESH_TOKEN aren't all set in .env")]

    checks = []
    try:
        await ebay_api.get_app_access_token()
        checks.append(Check("App keys", True, "category search, item specifics, condition check"))
    except Exception as e:
        checks.append(Check("App keys", False, f"EBAY_APP_ID/EBAY_CERT_ID rejected: {e}"))

    try:
        token = await ebay_api.get_access_token()
    except Exception as e:
        checks.append(Check("Seller sign-in token", False, f"EBAY_REFRESH_TOKEN rejected (expired?): {e}"))
        return checks
    checks.append(Check("Seller sign-in token", True, "refreshes OK"))

    inventory = ebay_api._api_base_url()
    checks.append(await _scope_check("Orders (#ebay-orders, auto Sold/Shipped)",
                                     f"{ebay_api._fulfillment_base_url()}/order?limit=1", token, "sell.fulfillment"))
    checks.append(await _scope_check("Fees & payouts (sync-finances, fee cards)",
                                     f"{ebay_finances._finances_base_url()}/transaction?limit=1", token, "sell.finances"))
    checks.append(await _scope_check("Listing (List on eBay)", f"{inventory}/inventory_item?limit=1", token,
                                     "sell.inventory"))

    key = config.EBAY_MERCHANT_LOCATION_KEY
    if not key:
        checks.append(Check("Merchant location", False, "EBAY_MERCHANT_LOCATION_KEY isn't set - API listing needs it"))
    else:
        try:
            status, _ = await asyncio.wait_for(
                _get(f"{inventory}/location/{urllib.parse.quote(key, safe='')}", token), timeout=_TIMEOUT_SECONDS)
        except Exception as e:
            checks.append(Check("Merchant location", False, f"couldn't reach eBay ({e!r})"))
        else:
            if status < 300:
                checks.append(Check("Merchant location", True, f"`{key}` found"))
            elif status in (401, 403):
                checks.append(Check("Merchant location", False, f"`{key}`: {_explain(status, 'sell.inventory')}"))
            else:
                checks.append(Check("Merchant location", False, f"`{key}` not found on your eBay account ({status})"))

    for label, path, container, id_field, configured in (
        ("Shipping policy", "fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId",
         config.EBAY_FULFILLMENT_POLICY_ID),
        ("Payment policy", "payment_policy", "paymentPolicies", "paymentPolicyId", config.EBAY_PAYMENT_POLICY_ID),
        ("Return policy", "return_policy", "returnPolicies", "returnPolicyId", config.EBAY_RETURN_POLICY_ID),
    ):
        if not configured:
            checks.append(Check(label, False, f"EBAY_{path.split('_')[0].upper()}_POLICY_ID isn't set - API listing needs it"))
            continue
        url = f"{_account_base_url()}/{path}?marketplace_id={config.EBAY_MARKETPLACE_ID}"
        try:
            status, body = await asyncio.wait_for(_get(url, token), timeout=_TIMEOUT_SECONDS)
        except Exception as e:
            checks.append(Check(label, False, f"couldn't reach eBay ({e!r})"))
            continue
        if status >= 300:
            checks.append(Check(label, None if status in (401, 403) else False,
                                "can't verify - token lacks `sell.account` (optional; listing may still work)"
                                if status in (401, 403) else f"eBay returned {status}"))
            continue
        policies = {str(p.get(id_field)): p.get("name") for p in (body or {}).get(container) or []}
        if str(configured) in policies:
            checks.append(Check(label, True, f"`{configured}` = \"{policies[str(configured)]}\""))
        else:
            checks.append(Check(label, False, f"`{configured}` isn't one of your eBay {label.lower()}s"))
    return checks


async def _account_check(label: str, account_id: str, allowed_types: set, purpose: str) -> Check:
    try:
        account = await quickbooks.get_account(account_id)
    except quickbooks.QuickBooksError as e:
        return Check(label, False, f"ID `{account_id}`: {e}")
    if not account["active"]:
        return Check(label, False, f"`{account_id}` \"{account['name']}\" is inactive in QuickBooks")
    if account["type"] not in allowed_types:
        return Check(label, False, f"`{account_id}` \"{account['name']}\" is a **{account['type']}** account - "
                                   f"{purpose} needs {' / '.join(sorted(allowed_types))}")
    return Check(label, True, f"`{account_id}` \"{account['name']}\" ({account['type']})")


async def quickbooks_checks() -> list:
    if not config.QUICKBOOKS_ENABLED:
        return [Check("QuickBooks", None, "not configured (QUICKBOOKS_CLIENT_ID/SECRET blank)")]
    if not quickbooks.is_connected():
        return [Check("QuickBooks", False, "not connected - run /finance connect-quickbooks")]

    checks = [Check("QuickBooks", True, "connected")]
    checks.append(await _account_check("Undeposited Funds", config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID,
                                       UNDEPOSITED_TYPES, "Undeposited Funds"))
    if not config.QUICKBOOKS_EBAY_FEES_ACCOUNT_ID:
        checks.append(Check("eBay fees account", False, "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID isn't set - fee cards can't post"))
    else:
        checks.append(await _account_check("eBay fees account", config.QUICKBOOKS_EBAY_FEES_ACCOUNT_ID,
                                           EXPENSE_TYPES, "an expense debit"))
    for label, account_id in (("eBay shipping account", config.QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID),
                              ("eBay ads account", config.QUICKBOOKS_EBAY_ADS_ACCOUNT_ID)):
        if account_id:
            checks.append(await _account_check(label, account_id, EXPENSE_TYPES, "an expense debit"))
        else:
            checks.append(Check(label, None, "not set - uses the eBay fees account"))
    return checks


def format_checks(checks: list) -> str:
    icon = {True: "✅", False: "❌", None: "➖"}
    return "\n".join(f"{icon[c.state]} **{c.label}** - {c.detail}" for c in checks)
