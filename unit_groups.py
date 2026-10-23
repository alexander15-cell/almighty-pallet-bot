"""
Multi-unit eBay listings: one bot item per physical unit.

A listing made in Seller Hub with, say, 17 units is tied to ONE bot item
(the "anchor" - its Custom label, e.g. pallet-1-item-43, is the listing's
SKU on eBay and on the website). /admin sync-units turns it into a unit
group: the anchor plus one added item per extra unit (same pallet, write-up,
photos and listing details). Every unit then moves through the normal
pipeline on its own - sold, held, shipped, profit - exactly like any item.

What's sellable: a unit that's Listed and not reserved by a website
checkout. The website sees the group as one product (the anchor's SKU)
with available_quantity = that count (capped by eBay's own count).

Keeping eBay in step (sync): eBay's AVAILABLE quantity is set to exactly
the number of sellable units - so a unit reserved by a website checkout,
sold in person, or put on hold can't also sell on eBay. Before writing,
the bot re-reads the listing: if eBay shows more sales than the bot has
recorded (an eBay order not yet picked up by the order poll), it does NOT
write - that would put a sold unit back on sale - and reports the group as
unsafe until the order is recorded. When nothing is sellable the listing is
ended (and relisted, with its new listing ID saved, when a unit comes back,
e.g. an expired checkout). Only hand-made listings (Trading API) are
supported; a bot-made Inventory-API listing is reported and left alone.
The sync runs after every change and every 10 minutes as a safety net.

eBay orders take specific units (assign_units), recorded per order line so
re-reading an order never sells a different unit. Orders placed before the
group existed keep the old one-item behaviour.
"""
import asyncio
import logging
from datetime import datetime, timezone

import config
import database as db
import ebay_api
import ebay_listing_rules

log = logging.getLogger(__name__)
_locks = {}


def _lock(group_sku: str) -> asyncio.Lock:
    if group_sku not in _locks:
        _locks[group_sku] = asyncio.Lock()
    return _locks[group_sku]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ lookups

def group_of(item: dict):
    """The item's group row, or None."""
    sku = db.unit_group_of(item["id"])
    return db.get_unit_group(sku) if sku else None


def public_sku(item: dict) -> str:
    """The SKU the outside world uses for this unit - its group's (the
    listing's Custom label) for a group member, else its own."""
    return db.unit_group_of(item["id"]) or ebay_listing_rules.custom_label(item)


def anchor_for(item: dict) -> dict:
    group = group_of(item)
    return db.get_item(group["anchor_item_id"]) if group else item


def sellable_units(group_sku: str, exclude_held: bool = True) -> list:
    """Listed units of the group not reserved by an open website checkout."""
    units = [u for u in db.unit_group_member_items(group_sku) if u["status"] == db.STATUS_LISTED]
    if not exclude_held or not units:
        return units
    held = db.active_stripe_holds([u["id"] for u in units])
    return [u for u in units if u["id"] not in held]


def secondary_member_ids() -> set:
    """Added units (not anchors) - never shown as products of their own."""
    groups = {g["group_sku"]: g["anchor_item_id"] for g in db.all_unit_groups()}
    return {item_id for item_id, sku in db.all_unit_group_member_ids().items() if groups.get(sku) != item_id}


# ------------------------------------------------------- eBay order -> units

def _as_utc(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _order_predates_group(order_date, group: dict) -> bool:
    placed, created = _as_utc(order_date), _as_utc(group["created_at"])
    if not placed or not created:
        return False
    if len(str(order_date)) <= 10:  # a bare date: only an earlier DAY predates
        return placed.date() < created.date()
    return placed < created


def line_key(line: dict, line_index: int = 0) -> str:
    return str(line.get("line_item_id") or f"{line.get('sku') or line.get('legacy_item_id')}#{line_index}")


def assign_units(order_id: str, line: dict, base_item: dict, line_index: int = 0, order_date=None) -> list:
    """
    The units an eBay order line took. A non-group item (or an order placed
    before its group existed) is just [base_item]. Otherwise the line's
    quantity is taken from the group's sellable units - preferring ones not
    reserved by a website checkout - and recorded, so the same order always
    maps to the same units.
    """
    group = group_of(base_item)
    if group is None or _order_predates_group(order_date or line.get("order_date"), group):
        return [base_item]
    key = line_key(line, line_index)
    existing = db.order_units(order_id, key)
    if existing:
        return [u for u in (db.get_item(i) for i in existing) if u]
    quantity = max(1, int(line.get("quantity") or 1))
    taken = db.group_units_taken_by_orders(group["group_sku"])  # never give one unit to two orders
    free = [u for u in sellable_units(group["group_sku"]) if u["id"] not in taken]
    held = [u for u in sellable_units(group["group_sku"], exclude_held=False) if u not in free and u["id"] not in taken]
    chosen = (free + held)[:quantity]
    if len(chosen) < quantity:
        log.warning("eBay order %s wants %d unit(s) of %s but only %d are Listed", order_id, quantity,
                    group["group_sku"], len(chosen))
    if chosen:
        db.record_order_units(order_id, key, group["group_sku"], [u["id"] for u in chosen])
    return chosen


# --------------------------------------------------- physical-stock ceiling

UNSOLD = (db.STATUS_LISTED, db.STATUS_ON_HOLD)


def unsold_units(group_sku: str) -> list:
    return [u for u in db.unit_group_member_items(group_sku) if u["status"] in UNSOLD]


def physical_unsold(group_sku: str) -> int:
    """The real number of unsold units: what was counted when the cap was
    set (eBay's count at setup, or /admin unit-fix) minus units sold since.
    The bot never offers eBay more than this - so an extra unit item in the
    bot (e.g. a setup run twice) can't inflate eBay's stock."""
    cap = db.get_unit_group_cap(group_sku)
    if cap is None:
        # Groups made before the ceiling existed: start it from today's
        # unsold count, but never above what eBay last showed.
        live = (db.current_active_listings().get(group_sku) or {}).get("quantity")
        unsold = len(unsold_units(group_sku))
        total = unsold if live is None else min(unsold, int(live) + _held_count(group_sku))
        db.set_unit_group_cap(group_sku, total)
        cap = db.get_unit_group_cap(group_sku)
    sold_since = sum(1 for u in db.unit_group_member_items(group_sku)
                     if u["status"] in (db.STATUS_SOLD, db.STATUS_SHIPPED) and (u.get("sold_at") or "") > cap["set_at"])
    return max(0, cap["total_units"] - sold_since)


def _held_count(group_sku: str) -> int:
    """Unsold units not offered for sale right now: on hold, or reserved by
    an open website checkout."""
    listed = [u for u in db.unit_group_member_items(group_sku) if u["status"] == db.STATUS_LISTED]
    reserved = len(db.active_stripe_holds([u["id"] for u in listed])) if listed else 0
    on_hold = sum(1 for u in db.unit_group_member_items(group_sku) if u["status"] == db.STATUS_ON_HOLD)
    return reserved + on_hold


def reservable_count(group_sku: str) -> int:
    """Units a new buyer can have right now: sellable units, never more
    than the physical ceiling minus units held/reserved. This is what eBay
    is set to and what the website catalog reports."""
    sellable = len(sellable_units(group_sku))
    return max(0, min(sellable, physical_unsold(group_sku) - _held_count(group_sku)))


ebay_target = reservable_count


# ------------------------------------------------------------ eBay quantity

class SyncResult:
    def __init__(self, ok: bool, note: str, wrote: bool = False):
        self.ok, self.note, self.wrote = ok, note, wrote

    def __repr__(self):
        return f"SyncResult(ok={self.ok}, note={self.note!r})"


async def sync(group_sku: str) -> SyncResult:
    """Sets the listing's eBay available quantity to the group's sellable
    count (see the module docstring for the guard). Returns a SyncResult;
    ok=False means eBay could NOT be brought in line and the group isn't
    safe to sell on the website right now."""
    async with _lock(group_sku):
        group = db.get_unit_group(group_sku)
        if group is None:
            return SyncResult(False, "not a unit group")
        if group["is_test"] or not config.EBAY_ENABLED:
            return SyncResult(True, "nothing to sync (test group or eBay off)")
        want = ebay_target(group_sku)
        result = await _sync(group, want)
        db.update_unit_group(group_sku, last_sync_at=_now(), last_error=None if result.ok else result.note[:300],
                             **({"last_synced_qty": want} if result.ok else {}))
        if not result.ok:
            log.warning("eBay quantity sync for %s failed: %s", group_sku, result.note)
        return result


async def _sync(group: dict, want: int) -> SyncResult:
    sku = group["group_sku"]
    if group["model"] == "inventory":
        return SyncResult(False, "bot-made (Inventory API) listing - multi-unit sync isn't supported for it")
    listing_id = group["listing_id"]
    if not listing_id:
        return SyncResult(False, "no eBay listing ID on file")
    try:
        live = await ebay_api.get_listing_quantity(listing_id)
    except ebay_api.EbayAPIError as e:
        return SyncResult(False, f"couldn't read the eBay listing ({str(e)[:150]})")
    recorded = db.count_group_ebay_units(sku)
    if live["sold"] - group["ebay_sold_baseline"] > recorded:
        return SyncResult(False, (f"eBay shows {live['sold'] - group['ebay_sold_baseline']} sale(s) since the group "
                                  f"was made but the bot has recorded {recorded} - waiting for the order to sync"))
    active = live["status"] == "Active"
    if not active:
        if want == 0:
            return SyncResult(True, "listing not active and nothing to sell")
        if not group["ended_by_bot"]:
            return SyncResult(False, f"eBay listing is {live['status'] or 'not active'} - relist it in Seller Hub")
        return await _relist(group, want)
    if want == 0:
        try:
            await ebay_api.end_fixed_price_listing(listing_id)
        except ebay_api.EbayAPIError as e:
            return SyncResult(False, f"couldn't end the eBay listing at 0 units ({str(e)[:150]})")
        held_left = len(sellable_units(sku, exclude_held=False)) > 0
        db.update_unit_group(sku, ended_by_bot=1 if held_left else 0)
        return SyncResult(True, "no units left to sell - eBay listing ended", wrote=True)
    if live["available"] == want:
        return SyncResult(True, f"eBay already shows {want}")
    try:
        await ebay_api.set_available_quantity(listing_id, want)
    except ebay_api.EbayAPIError as e:
        return SyncResult(False, f"couldn't set the eBay quantity to {want} ({str(e)[:150]})")
    return SyncResult(True, f"eBay quantity {live['available']} -> {want}", wrote=True)


async def _relist(group: dict, want: int) -> SyncResult:
    sku = group["group_sku"]
    anchor = db.get_item(group["anchor_item_id"])
    try:
        new_id = await ebay_api.relist_listing_for_item(anchor, listing_id=group["listing_id"])
        live = await ebay_api.get_listing_quantity(new_id)
        if live["available"] != want:
            await ebay_api.set_available_quantity(new_id, want)
    except ebay_api.EbayAPIError as e:
        return SyncResult(False, f"couldn't relist on eBay ({str(e)[:150]}) - relist it in Seller Hub")
    for unit in db.unit_group_member_items(sku):
        db.set_ebay_item_id(unit["id"], new_id)
    db.update_unit_group(sku, listing_id=new_id, ebay_sold_baseline=live["sold"], ended_by_bot=0)
    return SyncResult(True, f"relisted on eBay as {new_id} with {want} unit(s)", wrote=True)


async def sync_for_items(items: list) -> list:
    """Syncs every group these items belong to. Returns [(sku, SyncResult)]."""
    out = []
    for sku in sorted({db.unit_group_of(i["id"]) for i in items if i} - {None}):
        out.append((sku, await sync(sku)))
    return out


async def sync_all() -> list:
    out = []
    for group in db.all_unit_groups(include_test=False):
        out.append((group["group_sku"], await sync(group["group_sku"])))
    return out


# ------------------------------------------------------------- setting up

def plan(snapshot_row: dict, anchor: dict) -> dict:
    """What /admin sync-units would do for one listing: units to add so the
    group's unsold units match eBay's available count."""
    sku = ebay_listing_rules.custom_label(anchor)
    group = db.get_unit_group(sku)
    members = db.unit_group_member_items(sku) if group else [anchor]
    unsold = [u for u in members if u["status"] in (db.STATUS_LISTED, db.STATUS_ON_HOLD)]
    ebay_available = int(snapshot_row.get("quantity") or 0)
    return {"sku": sku, "anchor": anchor, "grouped": group is not None, "ebay_available": ebay_available,
            "unsold": len(unsold), "add": max(0, ebay_available - len(unsold)),
            "excess": max(0, len(unsold) - ebay_available)}


async def fix_count(group_sku: str, real_units: int, actor_id: int) -> dict:
    """/admin unit-fix: the owner's real count of unsold units. Sets the
    ceiling, soft-deletes extra Listed unit items (rows kept, like /item
    delete - newest added units first, never the anchor, never one reserved
    by a checkout) and syncs eBay. Returns {'retired': [item ids],
    'short': units still missing (run /admin sync-units apply), 'sync'}."""
    if real_units < 0:
        raise ValueError("The count can't be negative.")
    group = db.get_unit_group(group_sku)
    if group is None:
        raise ValueError(f"{group_sku} isn't a multi-unit listing.")
    db.set_unit_group_cap(group_sku, real_units, actor_id)
    unsold = unsold_units(group_sku)
    extra = len(unsold) - real_units
    retired = []
    if extra > 0:
        reserved = db.active_stripe_holds([u["id"] for u in unsold])
        candidates = [u for u in reversed(unsold) if u["status"] == db.STATUS_LISTED
                      and u["id"] != group["anchor_item_id"] and u["id"] not in reserved]
        for unit in candidates[:extra]:
            db.update_status(unit["id"], db.STATUS_DELETED, actor_id=actor_id,
                             note=f"Extra unit removed by /admin unit-fix ({group_sku} really has {real_units})")
            retired.append(unit["id"])
    short = max(0, real_units - len(unsold_units(group_sku)))
    return {"retired": retired, "short": short, "sync": await sync(group_sku)}


async def apply_plan(p: dict, actor_id: int) -> list:
    """Creates the group (if new) and the missing units. Returns the new
    item IDs. The anchor and every existing record are left as they are."""
    anchor = p["anchor"]
    sku = p["sku"]
    if not p["grouped"]:
        listing = db.get_ebay_listing_data(anchor["id"]) or {}
        listing_id = listing.get("ebay_item_id") or (db.current_active_listings().get(sku) or {}).get("listing_id")
        model = "inventory" if await ebay_api.is_inventory_model_listing(anchor) else "trading"
        live = await ebay_api.get_listing_quantity(listing_id) if listing_id and model == "trading" else {"sold": 0}
        db.create_unit_group(sku, anchor["id"], listing_id, model, live["sold"])
        db.set_unit_group_cap(sku, p["ebay_available"], actor_id)  # eBay's count = the real stock at setup
    new_ids = []
    for _ in range(p["add"]):
        new_id = db.clone_item_as_unit(anchor["id"], actor_id=actor_id)
        db.add_unit_group_member(sku, new_id)
        new_ids.append(new_id)
    return new_ids
