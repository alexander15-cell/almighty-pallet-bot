"""
Direct eBay API listing path - the "List on eBay (API)" button on Awaiting
Listing cards, gated behind config.EBAY_ENABLED (see item_flow.list_on_ebay_api).

Talks to eBay's modern Sell Inventory API (REST/JSON), not the older XML
Trading API - OAuth2 user access, refreshed on demand from a long-lived
refresh token (config.EBAY_REFRESH_TOKEN) obtained once, by hand, through
eBay's own OAuth consent flow. Unlike quickbooks.py there's no in-Discord
"connect" command: the refresh token doesn't rotate on use (confirmed
against eBay's own sample token-refresh response, which omits refresh_token
entirely - unlike QuickBooks, which rotates its refresh token on every
single use and so must persist the new one every time), so it's just read
from config and never written back anywhere. Only the short-lived (~2 hour)
access token is cached, in memory, module-level - cheap enough to re-mint
after a bot restart that persisting it isn't worth a database table.

One real listing is three sequential calls, per eBay's documented flow:
  1. createOrReplaceInventoryItem (PUT  /inventory_item/{sku})
  2. createOffer                  (POST /offer)                -> offerId
  3. publishOffer                 (POST /offer/{offerId}/publish) -> listingId

Reuses ebay_listing_rules.py's hard-won field-level business rules (condition-ID
fallback, Brand/MPN/Type "Does Not Apply" placeholders, category-keyword
weight estimate) rather than re-deriving them - those were tuned against
real eBay error codes from the CSV batch path, and the same category-level
requirements apply regardless of which API submits the listing.

Condition IDs are eBay's classic numeric codes (1000/1500/1750/3000/7000 -
see config.EBAY_CONDITIONS); the Inventory API wants its own ConditionEnum
strings instead (NEW/NEW_OTHER/etc. - see _CONDITION_ID_TO_ENUM below),
confirmed against eBay's own condition-id-values reference page.

Condition validity is also checked against eBay's real per-category data
(getItemConditionPolicies, the Metadata API) before falling back to
ebay_listing_rules.py's coarser static safe-set check - see
resolve_condition_id_with_live_check. That lookup uses a second, separate
OAuth grant from the listing calls above: an *application* token
(client_credentials, scope api_scope - just EBAY_APP_ID/EBAY_CERT_ID, no
user consent/refresh token involved), since Metadata API's standard-
condition data is public and doesn't need this seller's own consent. Its
own cache (_cached_app_token) is kept separate from the user token's
cache above for exactly that reason.

search_categories() reuses that same application token for eBay's
Taxonomy API (getCategorySuggestions) - a live, search-relevance-ranked
category lookup that replaces ebay_taxonomy.py's local keyword matching
wherever Queue Review or Automated Review need to find a category
(falling back to that local search on any failure/timeout, same pattern
as the condition check above). Hardcoded to category tree ID "0" (eBay's
stable ID for the EBAY_US marketplace) rather than resolved via a separate
getDefaultCategoryTreeId call, since this bot only supports EBAY_US.

get_orders() is the one live-data function here that needs the *user*
token's scope actually widened beyond what listing creation requires - it
calls the Fulfillment API (sell.fulfillment scope), a real seller-specific
endpoint the application token can't touch regardless of what it's
widened to. See cogs/ebay.py's order_poll_loop for what consumes it.
"""
import asyncio
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp

import config
import ebay_listing_rules
import ebay_taxonomy

# Proactively refresh this long before the access token would actually
# expire (eBay's own tokens last ~7200s/2h), so an in-flight request never
# races a refresh - same safety-margin pattern as quickbooks.py.
_REFRESH_SAFETY_MARGIN = timedelta(minutes=5)

# In-memory only - see this module's docstring for why no DB persistence is
# needed here, unlike quickbooks_connection.
_cached_access_token: str = None
_cached_expires_at: datetime = None

# Separate cache for the application (client_credentials) token used by
# get_condition_policies() below - a different grant type/scope than the
# user token above, so it's kept as its own module-level pair rather than
# reusing _cached_access_token.
_cached_app_token: str = None
_cached_app_token_expires_at: datetime = None

# How long to wait for the Metadata API condition-policy lookup before
# giving up and falling back to the static safe-set in ebay_listing_rules.py - this
# is a nice-to-have accuracy improvement, never worth blocking a listing
# over a slow/unreachable eBay endpoint.
_CONDITION_POLICY_TIMEOUT_SECONDS = 10

# Same reasoning for the Taxonomy API category search below - an accuracy
# improvement over ebay_taxonomy.py's local keyword search, never worth
# blocking a reviewer over.
_CATEGORY_SEARCH_TIMEOUT_SECONDS = 10

# eBay's stable category tree ID for the EBAY_US marketplace - this bot
# only supports EBAY_US (see config.EBAY_MARKETPLACE_ID and
# ebay_taxonomy.py's own docstring), so this is hardcoded rather than
# fetched via a separate getDefaultCategoryTreeId call on every search.
_EBAY_US_CATEGORY_TREE_ID = "0"

# eBay's classic numeric condition IDs (config.EBAY_CONDITIONS) mapped to
# the Inventory API's own ConditionEnum string values - confirmed against
# eBay's condition-id-values reference page (1000/1500/1750/3000) and
# general knowledge for 7000, a long-stable, unchanged enum value.
_CONDITION_ID_TO_ENUM = {
    "1000": "NEW",
    "1500": "NEW_OTHER",
    "1750": "NEW_WITH_DEFECTS",
    "3000": "USED_EXCELLENT",
    "7000": "FOR_PARTS_OR_NOT_WORKING",
}

# ebay_listing_data.auction_duration stores File Exchange-style values
# ("Days_7" - see config.EBAY_AUCTION_DURATIONS); the Inventory API's
# ListingDurationEnum uses its own uppercase form instead.
_AUCTION_DURATION_TO_ENUM = {
    "Days_3": "DAYS_3",
    "Days_5": "DAYS_5",
    "Days_7": "DAYS_7",
    "Days_10": "DAYS_10",
}


class EbayAPIError(Exception):
    """Raised for any eBay API/auth failure - callers should catch this
    rather than aiohttp's own exceptions, which this module doesn't let
    escape."""


def _identity_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com"
    return "https://api.ebay.com"


def _api_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/sell/inventory/v1"
    return "https://api.ebay.com/sell/inventory/v1"


def _metadata_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/sell/metadata/v1"
    return "https://api.ebay.com/sell/metadata/v1"


def _taxonomy_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/commerce/taxonomy/v1"
    return "https://api.ebay.com/commerce/taxonomy/v1"


def _browse_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/buy/browse/v1"
    return "https://api.ebay.com/buy/browse/v1"


async def get_live_listing_price(legacy_item_id: str):
    """
    A live eBay listing's current price, from the Browse API (public data,
    application token - works for hand-made Seller Hub listings too).
    Returns {'price': float, 'currency': str} for a fixed-price listing,
    None if the listing has ended / can't be found, or has no fixed price
    (an auction - its current bid isn't a price to copy). Raises
    EbayAPIError for any other failure.
    """
    token = await get_app_access_token()
    url = f"{_browse_base_url()}/item/get_item_by_legacy_id?legacy_item_id={urllib.parse.quote(str(legacy_item_id))}"
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE_ID}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 404:
                return None
            if resp.status >= 300:
                _raise_for_ebay_error(resp.status, body, "Browse getItemByLegacyId")
    price = (body or {}).get("price") or {}
    if "FIXED_PRICE" not in ((body or {}).get("buyingOptions") or ["FIXED_PRICE"]) or price.get("value") is None:
        return None
    try:
        return {"price": round(float(price["value"]), 2), "currency": price.get("currency") or "USD"}
    except (TypeError, ValueError):
        return None


_TRADING_NS = "{urn:ebay:apis:eBLBaseComponents}"


def _trading_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/ws/api.dll"
    return "https://api.ebay.com/ws/api.dll"


def _parse_active_listings(xml_text: str) -> tuple:
    """(listings, total_pages) from a GetMyeBaySelling response. Raises
    EbayAPIError if eBay answered with Ack=Failure."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    ns = _TRADING_NS
    if (root.findtext(f"{ns}Ack") or "") == "Failure":
        msg = root.findtext(f"{ns}Errors/{ns}LongMessage") or root.findtext(f"{ns}Errors/{ns}ShortMessage") or xml_text[:300]
        raise EbayAPIError(f"eBay GetMyeBaySelling failed: {msg}")
    listings = []
    for item in root.iter(f"{ns}Item"):
        listing_id = item.findtext(f"{ns}ItemID")
        if not listing_id:
            continue
        price = item.findtext(f"{ns}SellingStatus/{ns}CurrentPrice") or item.findtext(f"{ns}BuyItNowPrice")
        quantity = item.findtext(f"{ns}QuantityAvailable") or item.findtext(f"{ns}Quantity")
        listings.append({
            "listing_id": listing_id.strip(),
            "sku": (item.findtext(f"{ns}SKU") or "").strip(),
            "title": (item.findtext(f"{ns}Title") or "").strip(),
            "price": float(price) if price else None,
            "quantity": int(quantity) if quantity and quantity.isdigit() else None,
            "listing_type": item.findtext(f"{ns}ListingType") or "",
            "category_id": item.findtext(f"{ns}PrimaryCategory/{ns}CategoryID") or "",
            "condition_id": item.findtext(f"{ns}ConditionID") or "",
        })
    pages = root.findtext(f"{ns}ActiveList/{ns}PaginationResult/{ns}TotalNumberOfPages")
    return listings, int(pages) if pages and pages.isdigit() else 1


async def get_active_listings() -> list:
    """
    Every active listing on the seller's eBay account (Trading API
    GetMyeBaySelling - includes listings made by hand in Seller Hub), with
    its Custom label (SKU), listing ID, title, price, quantity, category
    and condition. Uses the seller's own token. Raises EbayAPIError.
    """
    token = await get_access_token()
    headers = {
        "X-EBAY-API-CALL-NAME": "GetMyeBaySelling", "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1349", "X-EBAY-API-IAF-TOKEN": token, "Content-Type": "text/xml",
    }
    listings, page, pages = [], 1, 1
    async with aiohttp.ClientSession() as session:
        while page <= pages and page <= 50:
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
                "<ActiveList><Include>true</Include><Pagination><EntriesPerPage>200</EntriesPerPage>"
                f"<PageNumber>{page}</PageNumber></Pagination></ActiveList><DetailLevel>ReturnAll</DetailLevel>"
                "</GetMyeBaySellingRequest>"
            )
            async with session.post(_trading_url(), data=body.encode(), headers=headers) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise EbayAPIError(f"eBay GetMyeBaySelling failed ({resp.status}): {text[:300]}")
            found, pages = _parse_active_listings(text)
            listings += found
            page += 1
    if page <= pages:
        raise EbayAPIError(f"eBay returned more than {page - 1} pages of active listings - stopped early")
    return listings


def _fulfillment_base_url() -> str:
    if config.EBAY_ENVIRONMENT == "sandbox":
        return "https://api.sandbox.ebay.com/sell/fulfillment/v1"
    return "https://api.ebay.com/sell/fulfillment/v1"


async def get_access_token() -> str:
    """
    Returns a valid access token, transparently refreshing (via
    config.EBAY_REFRESH_TOKEN) if the cached one is at or past its safety
    margin. Every API call below goes through this - nothing else in this
    module reads the cache directly.

    Deliberately omits `scope` from the refresh grant rather than
    hardcoding one - per OAuth spec, an omitted scope on a refresh_token
    grant defaults to everything the refresh token itself was actually
    granted, so the resulting access token's permissions track whatever
    EBAY_REFRESH_TOKEN currently covers automatically. An earlier version
    of this function hardcoded "sell.inventory" here, which silently
    narrowed every access token it minted to JUST that scope even after
    EBAY_REFRESH_TOKEN was re-consented with sell.fulfillment/sell.account
    added - confirmed as the real cause of get_orders() failing with a
    permission-shaped 403 even though the refresh token itself genuinely
    carried the wider scope (verified by requesting that scope alone,
    directly, outside this function, and having it succeed).
    """
    global _cached_access_token, _cached_expires_at

    if _cached_access_token and datetime.now(timezone.utc) < _cached_expires_at - _REFRESH_SAFETY_MARGIN:
        return _cached_access_token

    auth = aiohttp.BasicAuth(config.EBAY_APP_ID, config.EBAY_CERT_ID)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": config.EBAY_REFRESH_TOKEN,
    }
    url = f"{_identity_base_url()}/identity/v1/oauth2/token"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=data, auth=auth, headers={"Content-Type": "application/x-www-form-urlencoded"}
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                error_desc = body.get("error_description") or body.get("error") or body
                raise EbayAPIError(f"eBay token refresh failed ({resp.status}): {error_desc}")

    _cached_access_token = body["access_token"]
    _cached_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    return _cached_access_token


async def get_app_access_token() -> str:
    """
    Returns a valid *application* access token - a client_credentials grant
    (App ID + Cert ID only, no user consent/refresh token involved) good for
    public-data endpoints like the Metadata API's getItemConditionPolicies
    below. Deliberately a separate cache from get_access_token()'s user
    token above: different grant type, different scope, and this one needs
    no EBAY_REFRESH_TOKEN at all, so it stays usable even for an app that
    only ever calls public endpoints.
    """
    global _cached_app_token, _cached_app_token_expires_at

    if _cached_app_token and datetime.now(timezone.utc) < _cached_app_token_expires_at - _REFRESH_SAFETY_MARGIN:
        return _cached_app_token

    auth = aiohttp.BasicAuth(config.EBAY_APP_ID, config.EBAY_CERT_ID)
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    url = f"{_identity_base_url()}/identity/v1/oauth2/token"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=data, auth=auth, headers={"Content-Type": "application/x-www-form-urlencoded"}
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                error_desc = body.get("error_description") or body.get("error") or body
                raise EbayAPIError(f"eBay application token request failed ({resp.status}): {error_desc}")

    _cached_app_token = body["access_token"]
    _cached_app_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    return _cached_app_token


async def get_condition_policies(category_id: str) -> set:
    """
    Returns the set of condition-ID strings eBay's Metadata API says are
    actually valid for `category_id`, via getItemConditionPolicies. Raises
    EbayAPIError/TimeoutError/aiohttp errors on any failure - callers that
    want the existing static-safe-set fallback behavior should catch those
    themselves (see resolve_condition_id_with_live_check below), since a
    bare lookup failure here shouldn't by itself decide what the fallback
    is.
    """
    access_token = await get_app_access_token()
    # The path really is "get_item_condition_policies" (verb included) per
    # eBay's Metadata API spec - the shorter "item_condition_policies" 404s
    # (errorId 2002, "Resource not found"), which silently sent every
    # lookup to the static safe-set fallback. The filter value is
    # URL-encoded, as eBay's docs require.
    filter_value = urllib.parse.quote(f"categoryIds:{{{category_id}}}", safe="")
    url = (
        f"{_metadata_base_url()}/marketplace/{config.EBAY_MARKETPLACE_ID}/get_item_condition_policies"
        f"?filter={filter_value}"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                _raise_for_ebay_error(resp.status, body, "getItemConditionPolicies")

    policies = body.get("itemConditionPolicies") or []
    match = next((p for p in policies if str(p.get("categoryId")) == str(category_id)), None)
    if match is None and policies:
        match = policies[0]
    if match is None:
        raise EbayAPIError(f"eBay returned no item condition policy for category {category_id!r}: {body}")

    return {str(c["conditionId"]) for c in (match.get("itemConditions") or []) if c.get("conditionId")}


async def resolve_condition_id_with_live_check(category_id: str, condition_id: str, item_label: str) -> str:
    """
    Like ebay_listing_rules.resolved_condition_id, but tries a real per-category
    lookup via get_condition_policies() first instead of only checking the
    hardcoded config.EBAY_BROADLY_ACCEPTED_CONDITION_IDS safe-set. Falls
    back to that exact static check - never hard-fails a listing - if the
    live lookup errors, times out, or (shouldn't happen, but) comes back
    with no usable condition list, consistent with this bot's existing
    "never hard-block on a flaky external dependency" approach (AI review's
    own timeout+fallback, QuickBooks allocation's graceful failure).
    """
    try:
        allowed = await asyncio.wait_for(
            get_condition_policies(category_id), timeout=_CONDITION_POLICY_TIMEOUT_SECONDS
        )
    except Exception as exc:
        print(
            f"[ebay_api] {item_label}: live condition-policy lookup for category {category_id!r} "
            f"failed ({exc!r}) - falling back to the static safe-set check."
        )
        return ebay_listing_rules.resolved_condition_id(condition_id, item_label)

    if not allowed:
        print(
            f"[ebay_api] {item_label}: live condition-policy lookup for category {category_id!r} "
            f"returned no conditions - falling back to the static safe-set check."
        )
        return ebay_listing_rules.resolved_condition_id(condition_id, item_label)

    if condition_id in allowed:
        return condition_id

    fallback = config.EBAY_CONDITION_FALLBACK_ID if config.EBAY_CONDITION_FALLBACK_ID in allowed else next(
        (cid for cid, _ in config.EBAY_CONDITIONS if cid in allowed), None
    )
    if fallback is None:
        print(
            f"[ebay_api] {item_label}: condition ID {condition_id!r} isn't eBay-valid for category "
            f"{category_id!r} per the live lookup (allowed: {sorted(allowed)}), and none of this bot's "
            f"known condition IDs are either - submitting {condition_id!r} unchanged and letting eBay reject it."
        )
        return condition_id

    print(
        f"[ebay_api] {item_label}: condition ID {condition_id!r} isn't eBay-valid for category "
        f"{category_id!r} per the live lookup (allowed: {sorted(allowed)}) - falling back to {fallback!r}."
    )
    return fallback


async def _fetch_category_suggestions(query: str) -> list:
    """
    Raw Taxonomy API call - raises on any failure, same as
    get_condition_policies. search_categories() below is what callers
    should actually use (it adds the timeout + local-fallback behavior).
    """
    access_token = await get_app_access_token()
    encoded_query = urllib.parse.quote(query)
    url = f"{_taxonomy_base_url()}/category_tree/{_EBAY_US_CATEGORY_TREE_ID}/get_category_suggestions?q={encoded_query}"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                _raise_for_ebay_error(resp.status, body, "getCategorySuggestions")

    results = []
    for suggestion in body.get("categorySuggestions") or []:
        category = suggestion.get("category") or {}
        category_id = category.get("categoryId")
        category_name = category.get("categoryName")
        if not category_id or not category_name:
            continue
        # eBay returns ancestors nearest-parent-first; ebay_taxonomy.py's
        # "L1 > L2 > ... > leaf" breadcrumb convention is root-first, so
        # reverse before appending the leaf's own name.
        ancestors = suggestion.get("categoryTreeNodeAncestors") or []
        ancestor_names = [a["categoryName"] for a in reversed(ancestors) if a.get("categoryName")]
        full_path = " > ".join(ancestor_names + [category_name])
        results.append((category_id, full_path))
    return results


async def search_categories(query: str, limit: int = 10) -> list:
    """
    Live eBay category search via the Taxonomy API's getCategorySuggestions,
    ranked by eBay's own search relevance - a drop-in replacement for
    ebay_taxonomy.search() (same (category_id, full_path) list shape and
    argument order) that's meaningfully more accurate, since the local
    taxonomy search is plain keyword matching against category names with
    no understanding of what items actually sell as. Falls back to
    ebay_taxonomy.search() on any failure/timeout or an empty result -
    never worth blocking a reviewer over a flaky/slow external call, same
    pattern as resolve_condition_id_with_live_check above.
    """
    if not (query or "").strip():
        return []
    try:
        results = await asyncio.wait_for(_fetch_category_suggestions(query), timeout=_CATEGORY_SEARCH_TIMEOUT_SECONDS)
    except Exception as exc:
        print(f"[ebay_api] live category search for {query!r} failed ({exc!r}) - falling back to the local taxonomy search.")
        return ebay_taxonomy.search(query, limit=limit)
    if not results:
        return ebay_taxonomy.search(query, limit=limit)
    return results[:limit]


async def get_orders(since: datetime, limit: int = 50) -> list:
    """
    Fetches this seller's recent eBay orders via the Fulfillment API (GET
    /sell/fulfillment/v1/order), filtered to orders created on/after
    `since`. Uses the USER access token (get_access_token()) - this needs
    the sell.fulfillment scope on EBAY_REFRESH_TOKEN, a different scope
    than the sell.inventory one listing creation uses, so this only works
    once that token's been re-consented with the wider scope list (see
    README's eBay API section). Also sends X-EBAY-C-MARKETPLACE-ID
    (config.EBAY_MARKETPLACE_ID) - unlike the Inventory/Metadata/Taxonomy
    calls elsewhere in this file, the Fulfillment API rejects a request
    missing this header with a permission-shaped 403 (errorId 1100,
    "Insufficient permissions to fulfill the request") even with a token
    that genuinely carries the right scope - confirmed against a real
    account during this feature's rollout, not just eBay's docs. Unlike
    search_categories/
    resolve_condition_id_with_live_check above, there's no local fallback
    for live order data - a failure here is raised as EbayAPIError, not
    swallowed, since the order-sync poll loop (cogs/ebay.py) is what
    decides "failed, try again next poll", the same way credit_card_poll_loop
    handles a QuickBooksError.

    Returns a list of plain dicts - just what the poll loop needs to match
    a SKU back to a tracked item and log a sale, not eBay's full raw order
    object: {'order_id', 'sale_date' (YYYY-MM-DD), 'total_price',
    'line_items': [{'sku', 'quantity'}, ...]}. An order missing a usable
    total or with no identifiable line items (no SKU and no listing ID) is
    left out entirely - there's
    nothing useful this bot could do with either.
    """
    access_token = await get_access_token()
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    filter_value = f"creationdate:[{since_iso}..]"
    url = f"{_fulfillment_base_url()}/order?filter={urllib.parse.quote(filter_value)}&limit={limit}"
    headers = {"Authorization": f"Bearer {access_token}", "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE_ID}
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                _raise_for_ebay_error(resp.status, body, "getOrders")

    orders = []
    for raw_order in body.get("orders") or []:
        parsed = _parse_order(raw_order)
        if parsed:
            orders.append(parsed)
    return orders


async def get_order(order_id: str) -> dict:
    """
    Fetches one eBay order fresh by ID (GET /sell/fulfillment/v1/order/
    {orderId}) - what EbayOrderConfirmButton (cogs/ebay.py) calls on click,
    rather than trusting whatever the confirm card last showed, same
    "never trust stale card state" reasoning as AllocateChargeButton
    re-fetching the QuickBooks transaction fresh in cogs/finance.py.

    Returns the same shape as one entry from get_orders(), or raises
    EbayAPIError if the order can't be fetched or parsed (e.g. it no
    longer has a usable total or identifiable line items - vanishingly
    unlikely for an order that already passed through get_orders() once,
    but not something to silently paper over on a financial action).
    """
    access_token = await get_access_token()
    url = f"{_fulfillment_base_url()}/order/{urllib.parse.quote(order_id, safe='')}"
    headers = {"Authorization": f"Bearer {access_token}", "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE_ID}
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                _raise_for_ebay_error(resp.status, body, "getOrder")

    parsed = _parse_order(body)
    if not parsed:
        raise EbayAPIError(f"eBay order {order_id!r} has no usable total or identifiable line items: {body}")
    return parsed


def _parse_order(raw_order: dict):
    """Shared by get_orders/get_order - see get_orders' docstring for the
    returned shape and why an order missing a total or SKUs is dropped
    entirely rather than returned half-usable."""
    order_id = raw_order.get("orderId")
    if not order_id:
        return None
    total = ((raw_order.get("pricingSummary") or {}).get("total") or {}).get("value")
    if total is None:
        return None
    # A line is usable if it has this bot's SKU OR an eBay listing ID - a
    # listing created/edited by hand in Seller Hub may carry no SKU, and
    # used to make the whole order unreadable here, so its Confirm & log
    # sale card could never be posted. cogs/ebay.py's
    # _match_order_line_items matches by SKU first, then by listing ID.
    line_items = []
    for li in raw_order.get("lineItems") or []:
        if not (li.get("sku") or li.get("legacyItemId")):
            continue
        line = {"sku": li.get("sku") or "", "quantity": li.get("quantity") or 1}
        if li.get("legacyItemId"):
            line["legacy_item_id"] = str(li["legacyItemId"])
        line_items.append(line)
    if not line_items:
        return None
    creation_date = raw_order.get("creationDate") or ""
    sale_date = creation_date[:10] if len(creation_date) >= 10 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "order_id": order_id,
        "sale_date": sale_date,
        "total_price": float(total),
        "line_items": line_items,
        "payment_status": raw_order.get("orderPaymentStatus"),
        "cancel_state": (raw_order.get("cancelStatus") or {}).get("cancelState"),
    }


# getOrders' page-size maximum, per eBay's Fulfillment API spec.
_ORDERS_PAGE_LIMIT = 200


async def get_recently_modified_orders(since: datetime) -> list:
    """
    Every order eBay has changed on/after `since` (lastmodifieddate filter
    - a new order, a payment, a shipment, a cancellation all bump it),
    paginated, with the status fields cogs/ebay.py's order automation acts
    on. Separate from get_orders() above (which the #ebay-orders confirm
    cards depend on, unchanged) because this needs the per-line shipping
    status and the order's cancel/payment state, and every page rather than
    just the first. Needs the sell.fulfillment scope, same as get_orders().

    Returns [{'order_id', 'order_date' (raw ISO creationDate),
    'payment_status', 'cancel_state', 'line_items': [{'line_item_id',
    'sku', 'legacy_item_id', 'fulfillment_status'}, ...]}, ...] - an order
    with no ID is skipped. No buyer data is returned.
    """
    access_token = await get_access_token()
    headers = {"Authorization": f"Bearer {access_token}", "X-EBAY-C-MARKETPLACE-ID": config.EBAY_MARKETPLACE_ID}
    since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    filter_value = urllib.parse.quote(f"lastmodifieddate:[{since_iso}..]")
    orders, offset = [], 0
    while True:
        url = f"{_fulfillment_base_url()}/order?filter={filter_value}&limit={_ORDERS_PAGE_LIMIT}&offset={offset}"
        async with aiohttp.ClientSession() as session:
            async with session.request("GET", url, headers=headers) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 300:
                    _raise_for_ebay_error(resp.status, body, "getOrders (modified)")
        page = body.get("orders") or []
        for raw in page:
            if not raw.get("orderId"):
                continue
            orders.append({
                "order_id": raw["orderId"],
                "order_date": raw.get("creationDate"),
                "payment_status": raw.get("orderPaymentStatus"),
                "cancel_state": (raw.get("cancelStatus") or {}).get("cancelState"),
                "line_items": [
                    {
                        "line_item_id": li.get("lineItemId"),
                        "sku": li.get("sku"),
                        "legacy_item_id": str(li["legacyItemId"]) if li.get("legacyItemId") else None,
                        "fulfillment_status": li.get("lineItemFulfillmentStatus"),
                    }
                    for li in raw.get("lineItems") or []
                ],
            })
        offset += len(page)
        if not page or offset >= (body.get("total") or 0):
            break
    return orders


def _raise_for_ebay_error(resp_status: int, body: dict, context: str):
    errors = (body or {}).get("errors")
    detail = errors if errors else body
    raise EbayAPIError(f"eBay {context} failed ({resp_status}): {detail}")


async def _request(method: str, path: str, *, json_body: dict = None, extra_headers: dict = None) -> dict:
    """Shared authenticated request helper against the Inventory API."""
    access_token = await get_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Content-Language": "en-US",
    }
    if extra_headers:
        headers.update(extra_headers)
    url = f"{_api_base_url()}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, json=json_body, headers=headers) as resp:
            if resp.status == 204:  # publishOffer-adjacent calls can return no body
                return {}
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                _raise_for_ebay_error(resp.status, body, f"{method} {path}")
            return body


def _build_aspects(listing: dict, title: str, item_label: str) -> dict:
    """
    The same Brand/MPN/Type handling the removed CSV batch path used -
    see that function's own comments for why these three always need a real
    value (confirmed via real-upload error codes, not a style preference).
    Inventory API aspects are Map<String, List<String>>, so every value gets
    wrapped in a single-element list.
    """
    specifics = dict(listing.get("item_specifics") or {})

    brand_key = next((k for k in specifics if k.strip().lower() == "brand"), None)
    if not (brand_key and specifics[brand_key]):
        specifics[brand_key or "Brand"] = "Does Not Apply"

    mpn_key = next((k for k in specifics if k.strip().lower() == "mpn"), None)
    if not (mpn_key and specifics[mpn_key]):
        specifics[mpn_key or "MPN"] = "Does Not Apply"

    type_key = next((k for k in specifics if k.strip().lower() == "type"), None)
    if not (type_key and specifics[type_key]):
        inferred_type = ebay_listing_rules.infer_type(title)
        specifics[type_key or "Type"] = inferred_type or "Does Not Apply"
        if not inferred_type:
            print(f"[ebay_api] {item_label}: couldn't infer a Type from the title {title!r} - using 'Does Not Apply'.")

    return {key: [str(value)] for key, value in specifics.items()}


async def _build_inventory_item(item: dict, listing: dict, label: str) -> dict:
    title = listing.get("ebay_title") or ""
    condition_id = await resolve_condition_id_with_live_check(
        listing.get("category_id") or "", listing.get("condition_id") or config.EBAY_DEFAULT_CONDITION_ID, label
    )
    condition_enum = _CONDITION_ID_TO_ENUM.get(condition_id, "USED_EXCELLENT")

    weight_lb = listing.get("weight_lb")
    if weight_lb is None:
        category_path = ebay_taxonomy.get_path(listing.get("category_id")) or ""
        weight_lb = ebay_listing_rules.estimated_weight_lb(category_path or title)
        print(f"[ebay_api] {label}: no captured weight - estimating {weight_lb:g} lb.")

    package = {"weight": {"value": weight_lb, "unit": "POUND"}}
    if listing.get("length_in") is not None and listing.get("width_in") is not None and listing.get("height_in") is not None:
        package["dimensions"] = {
            "length": listing["length_in"], "width": listing["width_in"], "height": listing["height_in"],
            "unit": "INCH",
        }

    public_urls = [u for u in json.loads(item.get("photo_public_urls") or "[]") if u]

    return {
        "condition": condition_enum,
        "product": {
            "title": title,
            "description": item.get("ai_description") or item.get("raw_description") or "",
            "imageUrls": public_urls,
            "aspects": _build_aspects(listing, title, label),
        },
        "packageWeightAndSize": package,
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
    }


def _build_offer(item: dict, listing: dict, sku: str) -> dict:
    is_auction = (listing.get("listing_format") or "FixedPrice") == "Auction"
    offer = {
        "sku": sku,
        "marketplaceId": config.EBAY_MARKETPLACE_ID,
        "format": "AUCTION" if is_auction else "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": listing.get("category_id") or "",
        "listingDescription": item.get("ai_description") or item.get("raw_description") or "",
        "listingPolicies": {
            "fulfillmentPolicyId": config.EBAY_FULFILLMENT_POLICY_ID,
            "paymentPolicyId": config.EBAY_PAYMENT_POLICY_ID,
            "returnPolicyId": config.EBAY_RETURN_POLICY_ID,
        },
        "merchantLocationKey": config.EBAY_MERCHANT_LOCATION_KEY,
    }
    price = {"value": f"{listing['price']:.2f}", "currency": "USD"}
    if is_auction:
        offer["pricingSummary"] = {"auctionStartPrice": price}
        offer["listingDuration"] = _AUCTION_DURATION_TO_ENUM.get(listing.get("auction_duration"), "DAYS_7")
    else:
        offer["pricingSummary"] = {"price": price}
        offer["listingDuration"] = "GTC"
    return offer


def _require_publish_config():
    missing = [
        name for name, value in (
            ("EBAY_MERCHANT_LOCATION_KEY", config.EBAY_MERCHANT_LOCATION_KEY),
            ("EBAY_FULFILLMENT_POLICY_ID", config.EBAY_FULFILLMENT_POLICY_ID),
            ("EBAY_PAYMENT_POLICY_ID", config.EBAY_PAYMENT_POLICY_ID),
            ("EBAY_RETURN_POLICY_ID", config.EBAY_RETURN_POLICY_ID),
        ) if not value
    ]
    if missing:
        raise EbayAPIError(
            f"Can't publish to eBay - missing from .env: {', '.join(missing)}. See README's eBay API section "
            f"for where to find each of these on your eBay seller account."
        )


# Trading API errors that mean "it's already over" - nothing left to end.
_ALREADY_ENDED_CODES = {"1047"}  # "Auction has already been closed"


async def end_fixed_price_listing(listing_id: str) -> str:
    """Ends a fixed-price listing by its eBay item ID (Trading API
    EndFixedPriceItem) - works for listings made by hand in Seller Hub, which
    the Inventory API can't touch. Returns a short note; raises EbayAPIError."""
    import xml.etree.ElementTree as ET
    token = await get_access_token()
    headers = {"X-EBAY-API-CALL-NAME": "EndFixedPriceItem", "X-EBAY-API-SITEID": "0",
               "X-EBAY-API-COMPATIBILITY-LEVEL": "1349", "X-EBAY-API-IAF-TOKEN": token, "Content-Type": "text/xml"}
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<EndFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ItemID>{int(listing_id)}</ItemID><EndingReason>NotAvailable</EndingReason>"
            "</EndFixedPriceItemRequest>")
    async with aiohttp.ClientSession() as session:
        async with session.post(_trading_url(), data=body.encode(), headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise EbayAPIError(f"eBay EndFixedPriceItem failed ({resp.status}): {text[:300]}")
    root = ET.fromstring(text)
    ns = _TRADING_NS
    if (root.findtext(f"{ns}Ack") or "") in ("Failure", "PartialFailure"):
        codes = {e.findtext(f"{ns}ErrorCode") for e in root.iter(f"{ns}Errors")
                 if e.findtext(f"{ns}SeverityCode") == "Error"}
        if codes and codes <= _ALREADY_ENDED_CODES:
            return "eBay listing had already ended."
        msg = root.findtext(f"{ns}Errors/{ns}LongMessage") or text[:300]
        raise EbayAPIError(f"eBay EndFixedPriceItem failed: {msg}")
    return "eBay listing ended."


async def end_listing_for_item(item: dict, listing_id: str = None) -> str:
    """
    Ends this item's live eBay listing - used when it sells somewhere else,
    so it can't sell twice. Bot-made listings (Inventory API, found by the
    bot's SKU) are withdrawn; anything else - e.g. listed by hand in Seller
    Hub - is ended by its listing ID (Trading API). Returns a short note
    saying what happened. Raises EbayAPIError on failure.
    """
    if not config.EBAY_ENABLED:
        return "eBay API isn't enabled - end the listing in Seller Hub."
    sku = ebay_listing_rules.custom_label(item)
    try:
        offers = (await _request("GET", f"/offer?sku={urllib.parse.quote(sku)}")).get("offers") or []
    except EbayAPIError as e:
        if "25713" not in str(e) and "404" not in str(e):  # anything but "no offer for this SKU"
            raise
        offers = []
    ended = 0
    for offer in offers:
        if offer.get("status") == "PUBLISHED" and offer.get("offerId"):
            await _request("POST", f"/offer/{offer['offerId']}/withdraw")
            ended += 1
    if ended:
        return "eBay listing ended."
    if listing_id:
        return await end_fixed_price_listing(listing_id)
    return "No live eBay listing found for it."


async def _trading_call(call_name: str, inner_xml: str) -> "object":
    """One Trading API call with the seller's token; returns the parsed XML
    root. Raises EbayAPIError on HTTP or Ack=Failure."""
    import xml.etree.ElementTree as ET
    token = await get_access_token()
    headers = {"X-EBAY-API-CALL-NAME": call_name, "X-EBAY-API-SITEID": "0",
               "X-EBAY-API-COMPATIBILITY-LEVEL": "1349", "X-EBAY-API-IAF-TOKEN": token, "Content-Type": "text/xml"}
    body = (f'<?xml version="1.0" encoding="utf-8"?><{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"{inner_xml}</{call_name}Request>")
    async with aiohttp.ClientSession() as session:
        async with session.post(_trading_url(), data=body.encode(), headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise EbayAPIError(f"eBay {call_name} failed ({resp.status}): {text[:300]}")
    root = ET.fromstring(text)
    ns = _TRADING_NS
    if (root.findtext(f"{ns}Ack") or "") == "Failure":
        msg = root.findtext(f"{ns}Errors/{ns}LongMessage") or text[:300]
        raise EbayAPIError(f"eBay {call_name} failed: {msg}")
    return root


async def get_listing_quantity(listing_id: str) -> dict:
    """{'total', 'sold', 'available', 'status'} for a listing, straight from
    eBay (Trading GetItem). Raises EbayAPIError."""
    ns = _TRADING_NS
    root = await _trading_call("GetItem", f"<ItemID>{int(listing_id)}</ItemID><DetailLevel>ReturnAll</DetailLevel>")
    total = int(root.findtext(f"{ns}Item/{ns}Quantity") or 0)
    sold = int(root.findtext(f"{ns}Item/{ns}SellingStatus/{ns}QuantitySold") or 0)
    return {"total": total, "sold": sold, "available": max(0, total - sold),
            "status": root.findtext(f"{ns}Item/{ns}SellingStatus/{ns}ListingStatus") or ""}


async def set_available_quantity(listing_id: str, quantity: int):
    """Sets how many units a hand-made fixed-price listing has AVAILABLE
    (Trading ReviseInventoryStatus - its Quantity is the available count).
    Not for Inventory-API listings. Raises EbayAPIError."""
    await _trading_call("ReviseInventoryStatus",
                        f"<InventoryStatus><ItemID>{int(listing_id)}</ItemID><Quantity>{int(quantity)}</Quantity>"
                        "</InventoryStatus>")


async def is_inventory_model_listing(item: dict) -> bool:
    """True when the bot made this listing (Inventory API offer for its SKU)."""
    sku = ebay_listing_rules.custom_label(item)
    try:
        offers = (await _request("GET", f"/offer?sku={urllib.parse.quote(sku)}")).get("offers") or []
    except EbayAPIError as e:
        if "25713" in str(e) or "404" in str(e):
            return False
        raise
    return bool(offers)


async def relist_fixed_price_listing(listing_id: str) -> str:
    """Relists an ended fixed-price listing (Trading API RelistFixedPriceItem
    - works for listings made by hand in Seller Hub). Returns the NEW
    listing ID. Raises EbayAPIError."""
    import xml.etree.ElementTree as ET
    token = await get_access_token()
    headers = {"X-EBAY-API-CALL-NAME": "RelistFixedPriceItem", "X-EBAY-API-SITEID": "0",
               "X-EBAY-API-COMPATIBILITY-LEVEL": "1349", "X-EBAY-API-IAF-TOKEN": token, "Content-Type": "text/xml"}
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<RelistFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<Item><ItemID>{int(listing_id)}</ItemID></Item></RelistFixedPriceItemRequest>")
    async with aiohttp.ClientSession() as session:
        async with session.post(_trading_url(), data=body.encode(), headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise EbayAPIError(f"eBay RelistFixedPriceItem failed ({resp.status}): {text[:300]}")
    root = ET.fromstring(text)
    ns = _TRADING_NS
    new_id = root.findtext(f"{ns}ItemID")
    if (root.findtext(f"{ns}Ack") or "") == "Failure" or not new_id:
        msg = root.findtext(f"{ns}Errors/{ns}LongMessage") or text[:300]
        raise EbayAPIError(f"eBay RelistFixedPriceItem failed: {msg}")
    return new_id


async def relist_listing_for_item(item: dict, listing_id: str = None) -> str:
    """Puts an ended listing back up after a hold. Bot-made listings
    (Inventory API) get their withdrawn offer published again; anything
    else is relisted by its old listing ID. Returns the live listing ID.
    Raises EbayAPIError."""
    if not config.EBAY_ENABLED:
        raise EbayAPIError("eBay API isn't enabled.")
    sku = ebay_listing_rules.custom_label(item)
    try:
        offers = (await _request("GET", f"/offer?sku={urllib.parse.quote(sku)}")).get("offers") or []
    except EbayAPIError as e:
        if "25713" not in str(e) and "404" not in str(e):
            raise
        offers = []
    for offer in offers:
        if offer.get("status") == "UNPUBLISHED" and offer.get("offerId"):
            published = await _request("POST", f"/offer/{offer['offerId']}/publish")
            return str(published.get("listingId") or listing_id)
    if not listing_id:
        raise EbayAPIError("No eBay listing ID on file to relist.")
    return await relist_fixed_price_listing(listing_id)


async def create_listing(item: dict, listing: dict) -> dict:
    """
    Creates a live eBay listing from `item` (items table dict) and `listing`
    (ebay_listing_data dict, from database.get_ebay_listing_data) via eBay's
    Sell Inventory API. Returns {'ebay_item_id': <eBay listing ID>} on
    success. Raises EbayAPIError with eBay's own error detail on failure -
    never raises a bare aiohttp exception.
    """
    if not config.EBAY_ENABLED:
        raise NotImplementedError("Direct eBay API listing is disabled (config.EBAY_ENABLED is False).")
    _require_publish_config()

    sku = ebay_listing_rules.custom_label(item)
    label = sku

    inventory_item = await _build_inventory_item(item, listing, label)
    await _request("PUT", f"/inventory_item/{sku}", json_body=inventory_item)

    offer = _build_offer(item, listing, sku)
    offer_result = await _request("POST", "/offer", json_body=offer)
    offer_id = offer_result.get("offerId")
    if not offer_id:
        raise EbayAPIError(f"eBay createOffer succeeded but returned no offerId: {offer_result}")

    publish_result = await _request("POST", f"/offer/{offer_id}/publish")
    listing_id = publish_result.get("listingId")
    if not listing_id:
        raise EbayAPIError(f"eBay publishOffer succeeded but returned no listingId: {publish_result}")

    return {"ebay_item_id": listing_id}
