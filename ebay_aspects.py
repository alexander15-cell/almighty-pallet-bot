"""
eBay's own item specifics ("aspects" - Brand, Type, Color, Size, ...) per
category, from the Taxonomy API's getItemAspectsForCategory, and the AI
pre-fill built on top of them.

Why: eBay ranks listings with complete item specifics higher, and some
categories reject a listing outright without certain ones. Until now the bot
only ever guessed three of them (Brand/MPN/Type, "Does Not Apply" when it
couldn't - see ebay_api._build_aspects), so most listings went out with almost none.
Now:

  1. Automated Review (cogs/item_flow.py's run_ai_review) matches the AI's
     suggested category to a real eBay category, fetches that category's
     required + recommended aspects (cached here - they rarely change), and
     asks the AI to fill them from the photo/title/note
     (ai_review.fill_item_aspects). clean_suggestions() keeps only values
     that fit eBay's rules (allowed values for pick-list aspects, max
     length), and the result is saved as a SUGGESTION for that item.
  2. Queue Review's Approve form pre-fills its Item Specifics box with it -
     only when the reviewer keeps that same category, always editable.
  3. After Approve, missing_required() names any aspect eBay requires that
     still has no value, so it can be fixed before listing.

Nothing here ever blocks an item: every eBay/AI failure just means no
pre-fill, exactly like before this existed. The existing Brand/MPN/Type
"Does Not Apply" safety net at export time is unchanged.

Uses the same application token as the live category search
(ebay_api.get_app_access_token) - no new eBay permission needed.
"""
import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import aiohttp

import ai_review
import config
import database as db
import ebay_api

log = logging.getLogger(__name__)

# eBay asks callers to cache aspect metadata; it changes rarely.
CACHE_MAX_AGE = timedelta(days=30)
_FETCH_TIMEOUT_SECONDS = 10
# How many aspects to hand the AI / show - required first, then eBay's
# recommended ones. Keeps the prompt small and the Approve box readable.
MAX_ASPECTS = 20
# eBay's general per-value cap when a category doesn't state its own.
_DEFAULT_MAX_LENGTH = 65
# Discord's TextInput hard cap for the Approve form's Item Specifics box.
SPECIFICS_TEXT_LIMIT = 1000
_PLACEHOLDER_VALUES = {"", "unknown", "n/a", "na", "none", "null", "not specified", "unbranded/unknown"}


def parse_aspects(body: dict) -> list:
    """getItemAspectsForCategory's response -> [{'name', 'required',
    'usage', 'mode', 'multi', 'max_length', 'values'}], required first,
    then recommended, then optional (eBay's own order within each)."""
    aspects = []
    for raw in (body or {}).get("aspects") or []:
        name = raw.get("localizedAspectName")
        if not name:
            continue
        c = raw.get("aspectConstraint") or {}
        aspects.append({
            "name": name,
            "required": bool(c.get("aspectRequired")),
            "usage": c.get("aspectUsage") or "OPTIONAL",
            "mode": c.get("aspectMode") or "FREE_TEXT",
            "multi": c.get("itemToAspectCardinality") == "MULTI",
            "max_length": c.get("aspectMaxLength"),
            "values": [v["localizedValue"] for v in raw.get("aspectValues") or [] if v.get("localizedValue")],
        })
    rank = {True: 0, False: 1}
    return sorted(aspects, key=lambda a: (rank[a["required"]], a["usage"] != "RECOMMENDED"))


async def _fetch_from_ebay(category_id: str) -> list:
    token = await ebay_api.get_app_access_token()
    url = (
        f"{ebay_api._taxonomy_base_url()}/category_tree/{ebay_api._EBAY_US_CATEGORY_TREE_ID}"
        f"/get_item_aspects_for_category?category_id={urllib.parse.quote(str(category_id))}"
    )
    async with aiohttp.ClientSession() as session:
        async with session.request("GET", url, headers={"Authorization": f"Bearer {token}"}) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 300:
                ebay_api._raise_for_ebay_error(resp.status, body, "getItemAspectsForCategory")
    return parse_aspects(body)


async def get_category_aspects(category_id: str, now: datetime = None):
    """
    The category's aspects (parse_aspects shape), from the cache when it's
    fresh, else from eBay (and cached). On any eBay failure: a stale cached
    copy if there is one, else None - never raises.
    """
    if not category_id or not config.EBAY_ENABLED:
        return None
    now = now or datetime.now(timezone.utc)
    cached = db.get_cached_category_aspects(category_id)
    if cached:
        aspects, fetched_at = cached
        if now - datetime.fromisoformat(fetched_at) < CACHE_MAX_AGE:
            return aspects
    try:
        aspects = await asyncio.wait_for(_fetch_from_ebay(category_id), timeout=_FETCH_TIMEOUT_SECONDS)
    except Exception as e:
        log.warning("Item specifics lookup for eBay category %s failed (%r)%s", category_id, e,
                    " - using the cached copy" if cached else "")
        return cached[0] if cached else None
    db.save_category_aspects(category_id, aspects)
    return aspects


def relevant_aspects(aspects: list, limit: int = MAX_ASPECTS) -> list:
    """Required + eBay-recommended aspects (required first), capped."""
    return [a for a in aspects if a["required"] or a["usage"] == "RECOMMENDED"][:limit]


def clean_suggestions(raw: dict, aspects: list) -> dict:
    """
    Keeps only AI suggestions that fit eBay's own rules for this category:
    the name must be one of the given aspects (matched case-insensitively,
    returned with eBay's spelling); a pick-list (SELECTION_ONLY) aspect's
    value must be one of eBay's allowed values; values are trimmed to the
    aspect's max length; placeholders like "Unknown"/"N/A" are dropped
    (an empty box is more honest - the export's own Brand/MPN/Type safety
    net still applies). A list value (multi-value aspect) is joined.
    """
    by_name = {a["name"].strip().lower(): a for a in aspects}
    cleaned = {}
    for key, value in (raw or {}).items():
        aspect = by_name.get(str(key).strip().lower())
        if not aspect:
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v is not None)
        if value is None or isinstance(value, (dict, bool)):
            continue
        value = str(value).strip()
        if value.lower() in _PLACEHOLDER_VALUES:
            continue
        if aspect["mode"] == "SELECTION_ONLY" and aspect["values"]:
            match = next((v for v in aspect["values"] if v.lower() == value.lower()), None)
            if not match:
                continue
            value = match
        value = value[: aspect.get("max_length") or _DEFAULT_MAX_LENGTH].strip()
        if value:
            cleaned[aspect["name"]] = value
    return cleaned


def missing_required(aspects: list, specifics: dict) -> list:
    """Names of eBay-required aspects with no value in `specifics`."""
    have = {k.strip().lower() for k, v in (specifics or {}).items() if str(v).strip()}
    return [a["name"] for a in aspects or [] if a["required"] and a["name"].strip().lower() not in have]


def format_specifics(specifics: dict, limit: int = SPECIFICS_TEXT_LIMIT) -> str:
    """'Key: Value' lines - the Approve form's own Item Specifics format -
    stopping before Discord's text-box limit rather than cutting a line."""
    lines = []
    for key, value in specifics.items():
        if len("\n".join(lines + [f"{key}: {value}"])) > limit:
            break
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


async def prefill_for_items(item_ids: list, suggested_category: str, photo_paths: list,
                            title: str, description: str, note: str) -> dict:
    """
    The Automated Review step (see this module's docstring): resolves
    `suggested_category` to an eBay category, fetches its aspects, asks the
    AI to fill them, cleans the answer and saves it as each item's
    suggestion. Returns {'category_id', 'specifics'} or None if any step
    had nothing to work with. Never raises.
    """
    try:
        if not (config.EBAY_ENABLED and config.AI_ENABLED and suggested_category):
            return None
        matches = await ebay_api.search_categories(suggested_category, limit=1)
        if not matches:
            return None
        category_id = matches[0][0]
        aspects = await get_category_aspects(category_id)
        if not aspects:
            return None
        wanted = relevant_aspects(aspects)
        raw = await ai_review.fill_item_aspects(photo_paths, title, description, note, wanted)
        specifics = clean_suggestions(raw, wanted)
        if not specifics:
            return None
        for item_id in item_ids:
            db.save_item_aspect_suggestion(item_id, category_id, specifics)
        return {"category_id": category_id, "specifics": specifics}
    except Exception:
        log.exception("Item specifics pre-fill failed - continuing without it")
        return None
