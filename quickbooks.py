"""
QuickBooks Online integration - OAuth 2.0 connection + a thin Accounting API
v3 client, used by cogs/finance.py's credit-card poll loop and cost-
allocation workflows (see pallet_costs/awaiting_pallet_charges in
database.py).

Read/allocate/report only, by deliberate design: nothing in this module (or
anywhere else in the bot) initiates a payment, transfer, or card action
(freeze, limit change, etc). It only reads transactions/balances and writes
categorized Purchase expense entries that mirror allocations already made in
Discord - QuickBooks itself is always the one actually moving money, never
this bot.

Token lifecycle: QuickBooks access tokens last ~1 hour and refresh tokens
~100 days, ROTATING on every refresh (the old refresh_token stops working
the moment a new one is issued) - so get_access_token() below always
persists the new refresh_token via database.save_quickbooks_connection()
on every refresh, and refreshes proactively (a configurable safety margin
before actual expiry) rather than waiting for a request to fail with 401.

No HTTP server: this bot has no web endpoint to receive QuickBooks' OAuth
redirect. build_authorization_url() sends the user to Intuit's consent
page; the resulting redirect (to config.QUICKBOOKS_REDIRECT_URI, which
doesn't need anything actually listening on it) carries `code` and
`realmId` in its query string, which /finance connect-quickbooks has the
user paste back in and parse_redirect_url() below extracts.
"""
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp

import config
import database as db

logger = logging.getLogger(__name__)

AUTHORIZATION_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"

# Proactively refresh this long before the access token would actually
# expire, so an in-flight request never races a refresh.
_REFRESH_SAFETY_MARGIN = timedelta(minutes=5)


def _api_base_url() -> str:
    if config.QUICKBOOKS_ENVIRONMENT == "production":
        return "https://quickbooks.api.intuit.com"
    return "https://sandbox-quickbooks.api.intuit.com"


class QuickBooksError(Exception):
    """Raised for any QuickBooks API/auth failure - callers should catch
    this rather than aiohttp's own exceptions, which this module doesn't
    let escape."""


class QuickBooksNotConnected(QuickBooksError):
    """No stored connection yet - /finance connect-quickbooks hasn't been run."""


def is_configured() -> bool:
    """True once app credentials exist (config.py/.env) - enough to run
    /finance connect-quickbooks. Does NOT mean a company is connected yet;
    see is_connected()."""
    return config.QUICKBOOKS_ENABLED


def is_connected() -> bool:
    """True once a company has actually completed the OAuth flow."""
    return db.get_quickbooks_connection() is not None


def build_authorization_url(state: str) -> str:
    """
    The URL to send whoever runs /finance connect-quickbooks to. `state` is
    an opaque value the caller should generate per-attempt and verify on
    the way back (parse_redirect_url) to guard against a stale/mismatched
    authorization response being pasted in.
    """
    params = {
        "client_id": config.QUICKBOOKS_CLIENT_ID,
        "redirect_uri": config.QUICKBOOKS_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTHORIZATION_BASE_URL}?{urlencode(params)}"


def parse_redirect_url(redirect_url: str, expected_state: str = None) -> dict:
    """
    Pulls `code` and `realmId` out of the URL QuickBooks redirected to
    (pasted back in by whoever ran /finance connect-quickbooks - the page
    itself may have failed to load, which is fine, only the URL matters).
    Raises QuickBooksError with a user-facing message if it doesn't look
    like a valid authorization response.
    """
    try:
        parsed = urlparse(redirect_url.strip())
        query = parse_qs(parsed.query)
    except Exception as e:
        raise QuickBooksError(f"That doesn't look like a URL: {e}")

    if "error" in query:
        raise QuickBooksError(f"QuickBooks reported an error: {query['error'][0]}")

    code = query.get("code", [None])[0]
    realm_id = query.get("realmId", [None])[0]
    state = query.get("state", [None])[0]

    if not code or not realm_id:
        raise QuickBooksError(
            "Couldn't find `code` and `realmId` in that URL - make sure you copied the "
            "*entire* address bar contents after approving access, including everything "
            "after the `?`."
        )
    if expected_state is not None and state != expected_state:
        raise QuickBooksError(
            "That authorization link doesn't match this session (state mismatch) - run "
            "/finance connect-quickbooks again and use the link it gives you."
        )
    return {"code": code, "realm_id": realm_id}


async def _post_token_request(data: dict) -> dict:
    auth = aiohttp.BasicAuth(config.QUICKBOOKS_CLIENT_ID, config.QUICKBOOKS_CLIENT_SECRET)
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data, auth=auth, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                error_desc = body.get("error_description") or body.get("error") or body
                raise QuickBooksError(f"QuickBooks token request failed ({resp.status}): {error_desc}")
            return body


async def exchange_code_for_tokens(code: str, realm_id: str, actor_id: int = None) -> dict:
    """
    Finishes the OAuth flow: trades the authorization code from
    parse_redirect_url() for an access/refresh token pair, and persists
    the connection. Returns the saved connection dict.
    """
    body = await _post_token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.QUICKBOOKS_REDIRECT_URI,
    })
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    db.save_quickbooks_connection(
        realm_id=realm_id,
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        access_token_expires_at=expires_at.isoformat(),
        actor_id=actor_id,
    )
    return db.get_quickbooks_connection()


async def get_access_token() -> str:
    """
    Returns a valid access token, transparently refreshing (and persisting
    the rotated refresh_token) if the stored one is at or past its safety
    margin. This is the entry point every API call below goes through -
    nothing else in this module reads quickbooks_connection directly.
    """
    connection = db.get_quickbooks_connection()
    if connection is None:
        raise QuickBooksNotConnected(
            "QuickBooks isn't connected yet - run /finance connect-quickbooks first."
        )

    expires_at = datetime.fromisoformat(connection["access_token_expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) < expires_at - _REFRESH_SAFETY_MARGIN:
        return connection["access_token"]

    body = await _post_token_request({
        "grant_type": "refresh_token",
        "refresh_token": connection["refresh_token"],
    })
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(body["expires_in"]))
    db.save_quickbooks_connection(
        realm_id=connection["realm_id"],
        access_token=body["access_token"],
        # QuickBooks rotates the refresh token on every use - fall back to
        # the existing one only in the unexpected case the response omits it.
        refresh_token=body.get("refresh_token", connection["refresh_token"]),
        access_token_expires_at=new_expires_at.isoformat(),
        actor_id=connection.get("connected_by"),
    )
    return body["access_token"]


async def _request(method: str, path: str, *, params: dict = None, json_body: dict = None) -> dict:
    """Shared authenticated request helper against the connected realm."""
    connection = db.get_quickbooks_connection()
    if connection is None:
        raise QuickBooksNotConnected(
            "QuickBooks isn't connected yet - run /finance connect-quickbooks first."
        )
    access_token = await get_access_token()
    url = f"{_api_base_url()}/v3/company/{connection['realm_id']}/{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, params=params, json=json_body, headers=headers) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                fault = (body or {}).get("Fault", body)
                raise QuickBooksError(f"QuickBooks API request failed ({resp.status}): {fault}")
            return body


def _quote_query_string(value: str) -> str:
    return value.replace("'", "\\'")


async def get_account_balance(account_id: str) -> dict:
    """Current balance of one account (e.g. the configured credit card
    account) - {'id', 'name', 'balance'}."""
    result = await _request("GET", "query", params={
        "query": f"SELECT Id, Name, CurrentBalance FROM Account WHERE Id = '{_quote_query_string(account_id)}'"
    })
    rows = result.get("QueryResponse", {}).get("Account", [])
    if not rows:
        raise QuickBooksError(f"No QuickBooks account found with id {account_id!r}.")
    account = rows[0]
    return {"id": account["Id"], "name": account.get("Name"), "balance": account.get("CurrentBalance", 0.0)}


async def list_recent_transactions(account_id: str, since: datetime = None, max_results: int = 100) -> list:
    """
    Purchase transactions (QuickBooks' entity for card charges/expenses)
    posted against `account_id`, newest first. `since` defaults to 24
    hours ago - the credit-card poll loop passes a wider window and relies
    on database.has_seen_quickbooks_txn() for de-duplication rather than
    a tight time filter, since a backdated/edited transaction could
    otherwise slip past a narrow window.

    Returns a list of dicts: {id, date, amount, merchant, account_id}.
    """
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=1)
    since_date = since.strftime("%Y-%m-%d")

    result = await _request("GET", "query", params={
        "query": (
            "SELECT * FROM Purchase WHERE AccountRef = "
            f"'{_quote_query_string(account_id)}' AND TxnDate >= '{since_date}' "
            f"ORDERBY TxnDate DESC MAXRESULTS {int(max_results)}"
        )
    })
    rows = result.get("QueryResponse", {}).get("Purchase", [])

    transactions = []
    for row in rows:
        entity_ref = (row.get("EntityRef") or {})
        transactions.append({
            "id": row["Id"],
            "date": row.get("TxnDate"),
            "amount": row.get("TotalAmt", 0.0),
            "merchant": entity_ref.get("name") or row.get("PrivateNote") or "Unknown merchant",
            "account_id": account_id,
        })
    return transactions


async def get_transaction(txn_id: str) -> dict:
    """
    Looks up one Purchase transaction by its QuickBooks Id. Used by the
    persistent "Allocate to a pallet" button (cogs/finance.py) to re-fetch a
    charge's current amount/date/merchant on click - only the txn_id itself
    is durably encoded in the button's custom_id (so it survives a bot
    restart), everything else is looked up fresh rather than trusted from
    whatever was last rendered in the message.
    """
    result = await _request("GET", "query", params={
        "query": f"SELECT * FROM Purchase WHERE Id = '{_quote_query_string(txn_id)}'"
    })
    rows = result.get("QueryResponse", {}).get("Purchase", [])
    if not rows:
        raise QuickBooksError(f"QuickBooks transaction {txn_id!r} not found (deleted/voided?).")
    row = rows[0]
    entity_ref = row.get("EntityRef") or {}
    return {
        "id": row["Id"],
        "date": row.get("TxnDate"),
        "amount": row.get("TotalAmt", 0.0),
        "merchant": entity_ref.get("name") or row.get("PrivateNote") or "Unknown merchant",
    }


async def create_expense(account_id: str, amount: float, date: str, memo: str,
                          expense_account_ref: str = None) -> dict:
    """
    Pushes a categorized expense (a QuickBooks Purchase) into the connected
    company - used to mirror a Discord-side cost allocation back into the
    books. `memo` should carry the pallet name/ID for traceability (per the
    original spec). `expense_account_ref` is the QuickBooks expense/category
    account to post the line against; if omitted, QuickBooks' own default
    for the payment account is used.

    Returns {'id': <new Purchase id>}. This only ever CREATES a categorized
    expense record - it never moves money or touches the card itself.
    """
    line = {
        "Amount": round(float(amount), 2),
        "DetailType": "AccountBasedExpenseLineDetail",
        "AccountBasedExpenseLineDetail": {},
        "Description": memo,
    }
    if expense_account_ref:
        line["AccountBasedExpenseLineDetail"]["AccountRef"] = {"value": expense_account_ref}

    payload = {
        "PaymentType": "CreditCard",
        "AccountRef": {"value": account_id},
        "TxnDate": date,
        "PrivateNote": memo,
        "Line": [line],
    }
    result = await _request("POST", "purchase", json_body=payload)
    purchase = result.get("Purchase", {})
    return {"id": purchase.get("Id")}
