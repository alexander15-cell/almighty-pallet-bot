"""
eBay slash commands and background jobs. Every eBay listing goes through the
eBay API (the Awaiting Listing card's "List on eBay" - cogs/item_flow.py's
list_on_ebay_api); the old CSV batch upload path was removed.

/ebay category-search  - looks up real eBay leaf category IDs by keyword
                         (eBay's live Taxonomy API, falling back to the
                         bundled taxonomy - see ebay_taxonomy.py).
/ebay category-aspects - the item specifics eBay requires/recommends for a
                         category (see ebay_aspects.py).
/ebay status           - read-only check of every eBay permission and
                         QuickBooks setting the bot needs (status_checks.py).
/ebay sync-finances    - pulls eBay's own fee/shipping/refund/payout records
                         into read-only mirror tables (see ebay_finances.py).
/ebay net-report       - recorded revenue vs. eBay's real net payout, per
                         pallet or business-wide, with a per-item CSV.
                         Finance Management or Pallet Admin.
/ebay fee-cards        - posts "eBay fees to log" cards for older orders.

Pallet Admin unless noted. This file also owns two background loops:
order_poll_loop (new eBay orders -> #ebay-orders confirm cards matched to a
tracked item by SKU, plus the Sold/Shipped order automation) and
fee_card_loop (the eBay fee expense cards - see ebay_fee_expenses.py).
EbayOrderConfirmButton's click reuses finance_utils.post_sale_to_quickbooks/
split_evenly, the same functions /finance log-sale uses, so a sale logged
either way is handled identically.
"""
import csv
import io
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database as db
import discord_resilience
import ebay_api
import ebay_aspects
import ebay_books
import ebay_catalog
import price_sync
import ebay_fee_expenses
import ebay_finances
import ebay_listing_rules
import finance_utils
import runtime_settings
import status_checks

log = logging.getLogger(__name__)

NL = chr(10)


def _is_pallet_admin(interaction: discord.Interaction) -> bool:
    admin_role = runtime_settings.resolve_role(interaction.guild, config.ROLE_ADMIN)
    return bool(admin_role and admin_role in interaction.user.roles)


async def _require_admin(interaction: discord.Interaction) -> bool:
    if _is_pallet_admin(interaction):
        return True
    await interaction.response.send_message(
        f"You need the **{config.ROLE_ADMIN}** role to do that.", ephemeral=True
    )
    return False


async def _require_finance_role(interaction: discord.Interaction) -> bool:
    """Gates EbayOrderConfirmButton - this is effectively an automated
    /finance log-sale, so it's gated the same way that command is
    (ROLE_FINANCE_MGMT, not ROLE_ADMIN)."""
    finance_role = runtime_settings.resolve_role(interaction.guild, config.ROLE_FINANCE_MGMT)
    if _is_pallet_admin(interaction) or (finance_role and finance_role in interaction.user.roles):
        return True
    await interaction.response.send_message(
        f"You need the **{config.ROLE_FINANCE_MGMT}** role to do that.", ephemeral=True
    )
    return False


async def log_ebay_order_sale(bot, order_id: str, actor_id: int) -> dict:
    """
    The "Confirm & log sale" action for one eBay order - shared by the
    #ebay-orders button and the web dashboard. Re-fetches the order from
    eBay, refuses if an item was already logged, saves the sale, posts the
    Sales Receipt and updates #accounting. Returns {'ok', 'message'} (plus
    'sale_id' once a sale was saved).
    """
    try:
        order = await ebay_api.get_order(order_id)
    except ebay_api.EbayAPIError as e:
        return {"ok": False, "message": f"Couldn't re-fetch this order from eBay: {e}"}

    matched_items = _match_order_line_items(order)
    if not matched_items:
        return {"ok": False, "message": "None of this order's items match a tracked item anymore - nothing was submitted."}

    # Re-checked here, not just when the poll loop first matched this
    # order - the card can sit for a while, and another sale could
    # have claimed one of these items in the meantime, same reasoning
    # as /finance log-sale's own confirmation step.
    already_sold = [item for item in matched_items if db.get_item_sale(item["id"])]
    if already_sold:
        names = ", ".join(f"#{item['item_number']}" for item in already_sold)
        return {"ok": False, "message": f"Item(s) {names} were already logged in another sale - nothing was submitted."}

    n = len(matched_items)
    allocated_shares = finance_utils.split_evenly(order["total_price"], n)
    sale_id = db.create_sale("eBay", order["sale_date"], order["total_price"], False, created_by=actor_id)

    affected_pallets = set()
    for i, item in enumerate(matched_items):
        db.add_sale_item(sale_id, item["id"], allocated_shares[i])
        db.record_item_sale(item["id"], allocated_shares[i], "eBay", actor_id=actor_id)
        affected_pallets.add(item["pallet_id"])

    for pallet_id in affected_pallets:
        await finance_utils.refresh_finance_message(bot, pallet_id)

    result = await finance_utils.post_sale_to_quickbooks(
        sale_id, "eBay", order["total_price"], False, order["sale_date"],
    )
    await finance_utils.update_accounting_entries(
        bot, sale_id, receipt_doc=result.get("sales_receipt_doc"), error=result.get("error"),
    )
    item_list = ", ".join(
        f"{db.get_pallet(item['pallet_id'])['name']}#{item['item_number']}" for item in matched_items
    )

    if result["ok"]:
        message = (
            f"✅ Logged sale #{sale_id} ({item_list}) via **eBay** (order `{order_id}`).\n"
            f"Sales Receipt: `{result['sales_receipt_doc']}`"
        )
        await finance_utils.post_to_finance_audit_log(
            bot,
            f"💰 **Sale #{sale_id}** ({item_list}) via **eBay** - ${order['total_price']:.2f} "
            f"(Sales Receipt `{result['sales_receipt_doc']}`) - via eBay order auto-sync",
        )
    else:
        message = (
            f"⚠️ Sale #{sale_id} saved, but QuickBooks failed: {result['error']}\n"
            f"Run `/finance retry-sale sale_id:{sale_id}` once it's fixed - nothing was lost."
        )
    return {"ok": True, "message": message, "sale_id": sale_id}


class EbayOrderConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ebay_order_confirm:(?P<order_id>.+)"):
    """
    The button on every posted #ebay-orders card. A DynamicItem (same as
    cogs/finance.py's AllocateChargeButton) so it keeps working across bot
    restarts - only the order_id is encoded in its custom_id, everything
    else about the order (items, price, date) is re-fetched fresh from
    eBay on click (see ebay_api.get_order) rather than trusted from
    whatever the card last showed.
    """

    def __init__(self, order_id: str):
        super().__init__(discord.ui.Button(
            label="Confirm & log sale", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"ebay_order_confirm:{order_id}",
        ))
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: dict):
        return cls(match["order_id"])

    async def callback(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await log_ebay_order_sale(interaction.client, self.order_id, interaction.user.id)
        if result.get("sale_id"):
            try:
                await interaction.message.edit(view=None)
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                pass
        await interaction.followup.send(result["message"], ephemeral=True)


AUTO_ACTOR = 0  # actor_id recorded for anything the bot books by itself


def _auto_allowed(amount) -> bool:
    return config.EBAY_AUTO_BOOK and amount is not None and abs(float(amount)) <= config.EBAY_AUTO_BOOK_MAX


def _mark_auto(embed: discord.Embed, text: str) -> discord.Embed:
    embed.color = discord.Color.green()
    embed.set_footer(text=f"✅ Booked automatically - {text}"[:2000])
    return embed


def _mark_manual(embed: discord.Embed, reason: str) -> discord.Embed:
    embed.add_field(name="Needs a person", value=reason[:1000], inline=False)
    return embed


def _order_auto_reason(order: dict, matched: list):
    """None when an order can be logged automatically, else why not."""
    if not config.EBAY_AUTO_BOOK:
        return "automatic booking is off"
    if order.get("payment_status") != "PAID":
        return f"eBay says the order isn't paid yet ({order.get('payment_status') or 'unknown'})"
    if order.get("cancel_state") not in (None, "NONE_REQUESTED"):
        return f"there's a cancellation on it ({order['cancel_state']})"
    if len(matched) != len(order["line_items"]) or any((li.get("quantity") or 1) != 1 for li in order["line_items"]):
        return "not every line matches exactly one tracked item"
    if not _auto_allowed(order["total_price"]):
        return f"it's over the ${config.EBAY_AUTO_BOOK_MAX:,.0f} automatic limit"
    return None


async def _handle_new_order(bot: discord.Client, order: dict, matched_items: list):
    """A newly seen eBay order: logged automatically when it checks out
    (paid, no cancellation, every line matched, under the limit),
    otherwise posted with its Confirm & log sale button as before."""
    reason = _order_auto_reason(order, matched_items)
    if reason is None and db.claim_auto_book("sale", order["order_id"]):
        result = await log_ebay_order_sale(bot, order["order_id"], AUTO_ACTOR)
        db.finish_auto_book("sale", order["order_id"], result["message"])
        if result.get("sale_id"):
            await _post_ebay_order_match(bot, order, matched_items, auto_note=result["message"])
            return
        reason = result["message"]
    await _post_ebay_order_match(bot, order, matched_items, manual_reason=reason)


def _match_order_line_items(order: dict) -> list:
    """
    Resolves an eBay order's line items back to tracked items - by this
    bot's SKU (ebay_listing_rules.parse_custom_label) first, then by the
    item's confirmed eBay listing ID (a listing created/edited by hand in
    Seller Hub may carry no SKU - the same fallback the order automation
    uses). A line that matches neither is silently skipped, not an error:
    an order can still be worth confirming for whichever of its items DO
    match. Used by the poll loop (whether to post a card), _ensure_order_card,
    /ebay order-card, and EbayOrderConfirmButton (re-resolved fresh on click).
    """
    matched, seen_ids = [], set()
    for line_item in order["line_items"]:
        item = None
        parsed = ebay_listing_rules.parse_custom_label(line_item.get("sku") or "")
        if parsed:
            item = db.get_item_by_pallet_and_number(*parsed)
        if not item and line_item.get("legacy_item_id"):
            item = db.get_item_by_ebay_listing_id(line_item["legacy_item_id"])
        if item and item["id"] not in seen_ids:
            matched.append(item)
            seen_ids.add(item["id"])
    return matched


async def _post_ebay_order_match(bot: discord.Client, order: dict, matched_items: list, auto_note: str = None,
                                 manual_reason: str = None):
    """Posts one newly-seen eBay order to #ebay-orders - with its Confirm &
    log sale button, or (auto_note) as already logged by the bot."""
    channel_id = db.get_shared_channel_id("ebay-orders")
    if not channel_id:
        log.warning("eBay order %s seen but #ebay-orders isn't set up - run /setup shared-channels.", order["order_id"])
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        log.warning("Configured #ebay-orders channel %s not found.", channel_id)
        return

    n = len(matched_items)
    allocated_shares = finance_utils.split_evenly(order["total_price"], n)
    lines = []
    for i, item in enumerate(matched_items):
        pallet = db.get_pallet(item["pallet_id"])
        lines.append(f"**{pallet['name']}#{item['item_number']}** - ${allocated_shares[i]:.2f}")

    embed = discord.Embed(
        title="📦 New eBay order",
        description=f"Order `{order['order_id']}` on {order['sale_date']}",
        color=discord.Color.blue(),
    )
    embed.add_field(name=f"{n} item(s)", value="\n".join(lines), inline=False)
    embed.add_field(name="Total", value=f"${order['total_price']:.2f}", inline=True)
    if auto_note:
        _mark_auto(embed, auto_note.replace("✅ ", ""))
        await channel.send(embed=embed)
        return
    embed.set_footer(text="Nothing is sent to QuickBooks until you confirm.")
    if manual_reason and config.EBAY_AUTO_BOOK:
        _mark_manual(embed, manual_reason)
    view = discord.ui.View(timeout=None)
    view.add_item(EbayOrderConfirmButton(order["order_id"]))
    await channel.send(embed=embed, view=view)


# ------------------------------------------------------ eBay fee cards --

def build_fee_card_embed(breakdown: dict) -> discord.Embed:
    """The "💸 eBay fees" card for one order - what Confirm & log fees will
    book. See ebay_fee_expenses.py."""
    totals = breakdown["totals"]
    items = ", ".join(_item_label(i) for i in breakdown["items"]) or "(no tracked items)"
    embed = discord.Embed(
        title="💸 eBay fees to log",
        description=f"Order `{breakdown['order_id']}` - {items}",
        color=discord.Color.orange(),
    )
    lines = [f"{label}: {_money(totals[cat])}" for cat, label in ebay_fee_expenses.CATEGORY_LABELS.items()
             if totals[cat]]
    embed.add_field(name="What eBay kept", value="\n".join(lines) or "-", inline=True)
    embed.add_field(name="Total", value=f"**{_money(totals['total'])}**", inline=True)
    embed.add_field(
        name="Confirm will",
        value=(
            "• post a QuickBooks Journal Entry - debit these expenses, credit Undeposited Funds "
            "(so it matches eBay's real payout)\n"
            "• add these to the pallet's cost, so its profit is real"
        ),
        inline=False,
    )
    embed.set_footer(text="Nothing is sent to QuickBooks until you confirm.")
    return embed


class EbayFeeConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ebay_fees_confirm:(?P<order_id>.+)"):
    """The button on a "💸 eBay fees" card - a DynamicItem like
    EbayOrderConfirmButton, so it survives restarts. Re-reads everything
    fresh on click (ebay_fee_expenses.confirm_order_fees) rather than
    trusting the card's numbers."""

    def __init__(self, order_id: str):
        super().__init__(discord.ui.Button(
            label="Confirm & log fees", style=discord.ButtonStyle.success, emoji="💸",
            custom_id=f"ebay_fees_confirm:{order_id}",
        ))
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: dict):
        return cls(match["order_id"])

    async def callback(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await ebay_fee_expenses.confirm_order_fees(self.order_id, actor_id=interaction.user.id)
        if not result["ok"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return

        try:
            await interaction.message.edit(view=None)
        except discord_resilience.TRANSIENT_DISCORD_ERRORS:
            pass
        for pallet_id in result["pallet_ids"]:
            await finance_utils.refresh_finance_message(interaction.client, pallet_id)

        breakdown = result["breakdown"]
        totals = breakdown["totals"]
        items = ", ".join(_item_label(i) for i in breakdown["items"])
        await finance_utils.post_to_finance_audit_log(
            interaction.client,
            f"💸 **eBay fees** for order `{self.order_id}` ({items}) - {_money(totals['total'])} "
            f"(Journal Entry `{result['je_doc']}`: fees {_money(totals['fees'])}, labels {_money(totals['shipping'])}, "
            f"ads {_money(totals['ads'])}; credited Undeposited Funds)",
        )
        await interaction.followup.send(
            f"✅ Logged {_money(totals['total'])} of eBay fees for order `{self.order_id}` - QuickBooks Journal Entry "
            f"`{result['je_doc']}`, and added to the pallet's cost. Pinned card updated.",
            ephemeral=True,
        )


async def _auto_fees(bot: discord.Client, breakdown: dict):
    """Books one order's fee card by itself if allowed. Returns (embed note,
    manual reason) - exactly one is set."""
    order_id = breakdown["order_id"]
    if not _auto_allowed(breakdown["totals"]["total"]):
        return None, (f"it's over the ${config.EBAY_AUTO_BOOK_MAX:,.0f} automatic limit"
                      if config.EBAY_AUTO_BOOK else None)
    if not db.claim_auto_book("fees", order_id):
        return None, "the bot already tried this one - check it by hand"
    result = await ebay_fee_expenses.confirm_order_fees(order_id, actor_id=AUTO_ACTOR)
    db.finish_auto_book("fees", order_id, result.get("message") or f"JE {result.get('je_doc')}")
    if not result["ok"]:
        return None, result["message"]
    for pallet_id in result["pallet_ids"]:
        await finance_utils.refresh_finance_message(bot, pallet_id)
    totals = result["breakdown"]["totals"]
    await finance_utils.post_to_finance_audit_log(
        bot, f"💸 **eBay fees** for order `{order_id}` - {_money(totals['total'])} booked automatically "
             f"(Journal Entry `{result['je_doc']}`; credited Undeposited Funds)")
    return f"Journal Entry {result['je_doc']}, added to the pallet's cost.", None


async def _post_fee_card(bot: discord.Client, breakdown: dict) -> bool:
    channel_id = db.get_shared_channel_id("ebay-orders")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        log.warning("eBay fee card for order %s not posted - #ebay-orders isn't set up.", breakdown["order_id"])
        return False
    embed = build_fee_card_embed(breakdown)
    auto_note, reason = await _auto_fees(bot, breakdown) if config.EBAY_AUTO_BOOK else (None, None)
    view = None
    if auto_note:
        _mark_auto(embed, auto_note)
    else:
        if reason:
            _mark_manual(embed, reason)
        view = discord.ui.View(timeout=None)
        view.add_item(EbayFeeConfirmButton(breakdown["order_id"]))
    try:
        await channel.send(embed=embed, view=view)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        log.exception("Couldn't post eBay fee card for order %s - will retry next run", breakdown["order_id"])
        return False
    db.mark_ebay_fee_txns_carded(
        breakdown["order_id"], [(c["transaction_id"], c["transaction_type"]) for c in breakdown["charges"]]
    )
    return True


async def _run_fee_cards(bot: discord.Client, since_date: str = None, sync: bool = True) -> int:
    """One pass: refresh the last 14 days of eBay fee data (unless `sync`
    is False), then post a card for every due order placed on/after
    `since_date` (default: EBAY_FEE_CARDS_START_DATE, or the day this first
    ran). Returns how many cards were posted."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since_date = since_date or config.EBAY_FEE_CARDS_START_DATE or db.get_ebay_fee_card_start_date(today)
    if sync:
        try:
            await ebay_finances.sync(14)
        except ebay_api.EbayAPIError:
            log.exception("eBay fee data sync failed - posting cards from the last good sync")
    posted = 0
    for breakdown in ebay_fee_expenses.orders_needing_cards(since_date):
        if await _post_fee_card(bot, breakdown):
            posted += 1
    if posted:
        log.info("Posted %d eBay fee card(s)", posted)
    return posted


# ----------------------------------------------- /ebay category-aspects --

def build_category_aspects_text(category_id: str, aspects: list, limit: int = 1900) -> str:
    """Required, then recommended item specifics, with a few example values
    for pick-list ones - kept under Discord's 2000-char message limit."""
    def describe(a):
        line = f"• **{a['name']}**"
        if a["mode"] == "SELECTION_ONLY" and a["values"]:
            line += f" - pick one: {', '.join(a['values'][:6])}{'…' if len(a['values']) > 6 else ''}"
        return line

    required = [describe(a) for a in aspects if a["required"]]
    recommended = [describe(a) for a in aspects if not a["required"] and a["usage"] == "RECOMMENDED"]
    parts = [f"Item specifics eBay uses for category `{category_id}`:"]
    parts.append("**Required:**\n" + ("\n".join(required) if required else "None"))
    if recommended:
        parts.append("**Recommended:**\n" + "\n".join(recommended))
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n…"
    return text


# ---------------------------------------------------- order automation --

# eBay orderPaymentStatus values where the buyer hasn't actually paid (or
# has been fully refunded) - never auto-mark Sold on these.
_UNPAID_PAYMENT_STATUSES = {"FAILED", "PENDING", "FULLY_REFUNDED"}


def _order_predates_listing(order: dict, item: dict) -> bool:
    """True if the order was placed before the item's most recent listing -
    i.e. it belongs to an earlier listing of this item (returned and
    relisted), so it must not mark the current listing Sold again."""
    order_dt = ebay_finances._parse_dt(order.get("order_date"))
    listed_dt = ebay_finances._parse_dt(item.get("listed_at"))
    return bool(order_dt and listed_dt and order_dt < listed_dt)


async def _post_order_alert(bot: discord.Client, text: str):
    channel_id = db.get_shared_channel_id("ebay-orders")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        log.warning("eBay order alert not posted (#ebay-orders isn't set up): %s", text)
        return
    try:
        await channel.send(text)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        log.exception("Could not post eBay order alert: %s", text)


async def _reconcile_ebay_order_statuses(bot: discord.Client, orders: list) -> dict:
    """
    Moves tracked items along to match what eBay says happened to their
    orders - see config.EBAY_ORDER_AUTOMATION. Per order line matched to an
    item (ebay_finances.match_line_to_item: SKU, then listing ID, only items
    created before the order):

      - paid, not cancelled, item Listed -> Sold
      - eBay shows the line shipped, item Sold -> Shipped
      - order cancelled, item already Sold/Shipped -> one alert in
        #ebay-orders (never reversed automatically - a cancelled order may
        already have a QuickBooks Sales Receipt that needs voiding first)
      - item On Hold -> one alert, item left alone

    Guards against acting twice: an order that predates the item's latest
    listing is ignored (returned + relisted), and each order+item is only
    ever auto-sold/auto-shipped once (ebay_order_automation_log, recorded
    only after the move succeeds, so a transient failure retries next poll
    while a human moving an item back afterwards sticks). Returns counts.

    Also makes sure every paid order whose item ended up Sold/Shipped has a
    "Confirm & log sale" card (_ensure_order_card) - the new-order check in
    order_poll_loop only looks at orders CREATED in the last 3 days, so an
    older order that eBay updated later (e.g. when it shipped) used to get
    moved to Sold here with no card to log it from.
    """
    cog = bot.get_cog("ItemFlow")
    counts = {"sold": 0, "shipped": 0, "alerts": 0}
    if cog is None:
        return counts

    for order in orders:
        order_id = order["order_id"]
        cancel_state = order.get("cancel_state") or "NONE_REQUESTED"
        cancelled = cancel_state == "CANCELED"
        cancel_requested = cancel_state not in ("NONE_REQUESTED", "CANCELED")
        paid = order.get("payment_status") not in _UNPAID_PAYMENT_STATUSES
        sold_items = []

        for line in order["line_items"]:
            item = ebay_finances.match_line_to_item({**line, "order_date": order.get("order_date")})
            if not item:
                continue
            label = _item_label(item)
            try:
                if cancelled:
                    if item["status"] in (db.STATUS_SOLD, db.STATUS_SHIPPED) and \
                            db.record_ebay_order_automation(order_id, item["id"], "cancel_alert"):
                        await _post_order_alert(
                            bot,
                            f"⚠️ eBay order `{order_id}` was **cancelled**, but **{label}** is marked "
                            f"**{item['status']}** here. If it's back on the shelf, use `/item move-back` to "
                            f"return it to Listed (void its QuickBooks Sales Receipt first if the sale was logged).",
                        )
                        counts["alerts"] += 1
                    continue

                if item["status"] == db.STATUS_ON_HOLD:
                    if db.record_ebay_order_automation(order_id, item["id"], "hold_alert"):
                        await _post_order_alert(
                            bot,
                            f"⚠️ **{label}** sold on eBay (order `{order_id}`) but is **on hold** here - "
                            f"resolve the hold, then mark it sold. Nothing was changed automatically.",
                        )
                        counts["alerts"] += 1
                    continue

                if paid and not cancel_requested \
                        and item["status"] == db.STATUS_LISTED \
                        and not _order_predates_listing(order, item) \
                        and not db.has_ebay_order_automation(order_id, item["id"], "sold"):
                    if await cog.auto_mark_sold_from_ebay(item, order_id, ebay_item_id=line.get("legacy_item_id")):
                        db.record_ebay_order_automation(order_id, item["id"], "sold")
                        counts["sold"] += 1
                        await _post_order_alert(bot, f"🤖 **{label}** sold on eBay (order `{order_id}`) - moved to Sold.")
                    item = db.get_item(item["id"])

                if line.get("fulfillment_status") == "FULFILLED" and item["status"] == db.STATUS_SOLD \
                        and not db.has_ebay_order_automation(order_id, item["id"], "shipped"):
                    if await cog.auto_mark_shipped_from_ebay(item, order_id):
                        db.record_ebay_order_automation(order_id, item["id"], "shipped")
                        counts["shipped"] += 1
                    item = db.get_item(item["id"])

                if item["status"] in (db.STATUS_SOLD, db.STATUS_SHIPPED) and not _order_predates_listing(order, item):
                    sold_items.append(item)
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                log.exception("eBay order automation failed for %s (order %s) - will retry next poll", label, order_id)

        if sold_items and paid and not cancelled and not cancel_requested \
                and not db.has_seen_ebay_order(order_id) \
                and not any(db.get_item_sale(i["id"]) for i in sold_items):
            await _ensure_order_card(bot, order_id)
    return counts


async def _ensure_order_card(bot: discord.Client, order_id: str) -> bool:
    """
    Posts the "Confirm & log sale" card for an order the new-order check
    never carded (see _reconcile_ebay_order_statuses), fetching it fresh
    from eBay for the price/date/SKUs the card needs. Marks the order seen
    once posted, so it's never carded twice. Returns True if a card was
    posted. An eBay or Discord failure is logged and left un-seen, so the
    next poll tries again.
    """
    try:
        order = await ebay_api.get_order(order_id)
    except ebay_api.EbayAPIError:
        log.exception("Couldn't fetch eBay order %s to post its sale card - will retry next poll", order_id)
        return False
    matched = _match_order_line_items(order)
    if not matched or any(db.get_item_sale(i["id"]) for i in matched):
        db.mark_ebay_order_seen(order_id)
        return False
    try:
        await _handle_new_order(bot, order, matched)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        log.exception("Couldn't post eBay order %s's sale card - will retry next poll", order_id)
        return False
    db.mark_ebay_order_seen(order_id)
    log.info("Posted a missing Confirm & log sale card for eBay order %s", order_id)
    return True


async def _run_order_automation(bot: discord.Client) -> dict:
    """One pass of the order automation over every order eBay changed in the
    last EBAY_ORDER_AUTOMATION_LOOKBACK_DAYS - called at the end of each
    order_poll_loop run. A failed eBay call is logged and retried next poll,
    same as the new-order check."""
    since = datetime.now(timezone.utc) - timedelta(days=config.EBAY_ORDER_AUTOMATION_LOOKBACK_DAYS)
    try:
        orders = await ebay_api.get_recently_modified_orders(since)
    except ebay_api.EbayAPIError:
        log.exception("eBay order automation poll failed")
        return {}
    counts = await _reconcile_ebay_order_statuses(bot, orders)
    if any(counts.values()):
        log.info("eBay order automation: %s", counts)
    return counts


# ------------------------------------------------------ /ebay net-report --

def _money(value) -> str:
    if value is None:
        return "-"
    value = round(value, 2) + 0.0  # + 0.0 turns -0.0 into 0.0
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _sync_note(last_sync: dict) -> str:
    if not last_sync:
        return "No eBay data synced yet - run `/ebay sync-finances` first."
    return f"eBay data as of {last_sync['finished_at'][:16].replace('T', ' ')} UTC (`/ebay sync-finances` to refresh)."


def _item_label(item: dict) -> str:
    pallet = db.get_pallet(item["pallet_id"])
    return f"{pallet['name'] if pallet else 'pallet ' + str(item['pallet_id'])}#{item['item_number']}"


def build_pallet_net_embed(report: dict, last_sync: dict) -> discord.Embed:
    """One pallet's /ebay net-report embed - recorded numbers next to eBay's.
    Only displays; computes nothing new beyond ebay_finances' report."""
    pallet, totals, fin = report["pallet"], report["totals"], report["financials"]
    embed = discord.Embed(
        title=f"🧾 {pallet['name']} - eBay Net vs. Recorded",
        description=(
            "Read-only comparison - **nothing here changes any recorded number**, the pinned card, or QuickBooks.\n"
            + _sync_note(last_sync)
        ),
        color=discord.Color.teal(),
    )
    n = len(report["matched"])
    if n:
        embed.add_field(
            name=f"eBay sales matched: {n} item(s)",
            value=(
                f"Recorded as revenue: **{_money(report['recorded_for_matched'])}**\n"
                f"eBay item price: {_money(totals['item_price'])}\n"
                f"Buyer-paid shipping: {_money(totals['buyer_shipping'])}\n"
                f"eBay fees: {_money(-totals['ebay_fees'])}\n"
                f"eBay shipping labels: {_money(-totals['shipping_labels'])}\n"
                f"Refunds & adjustments: {_money(-totals['refunds'])}\n"
                f"Ad/listing fees: {_money(-totals['ad_fees'])}\n"
                f"**eBay net payout: {_money(totals['net_payout'])}**"
            ),
            inline=False,
        )
        gap = round(report["recorded_for_matched"] - totals["net_payout"], 2)
        embed.add_field(name="Recorded minus eBay net", value=_money(gap), inline=True)
    else:
        embed.add_field(name="eBay sales matched", value="None - no synced eBay order maps to this pallet's items.", inline=False)

    if fin["profit_so_far"] is not None:
        embed.add_field(
            name="Profit/Loss",
            value=(
                f"On the card now: {_money(fin['profit_so_far'])}\n"
                f"Using eBay net for matched sales: **{_money(report['profit_with_ebay_net'])}**"
            ),
            inline=True,
        )

    notes = []
    if report["shipping_in_revenue"]:
        shown = ", ".join(_item_label(e["item"]) for e in report["shipping_in_revenue"][:8])
        more = f" +{len(report['shipping_in_revenue']) - 8} more" if len(report["shipping_in_revenue"]) > 8 else ""
        notes.append(f"📦 Recorded price includes buyer-paid shipping: {shown}{more}")
    if report["missing"]:
        shown = ", ".join(f"#{i['item_number']}" for i in report["missing"][:10])
        more = f" +{len(report['missing']) - 10} more" if len(report["missing"]) > 10 else ""
        notes.append(f"❓ Recorded as eBay sales but no matching eBay order synced: {shown}{more}")
    if report["manual_entries_on_matched"]:
        notes.append(
            f"⚠️ {report['manual_entries_on_matched']} manual `/finance refund`/`expense` entr(ies) on matched items - "
            "if those were eBay refunds/fees, eBay's net already includes them, so the adjusted profit counts them twice."
        )
    if report["any_estimates"]:
        notes.append("≈ Some orders have no eBay payout record synced yet - their net is an estimate (total minus eBay's fee).")
    if notes:
        embed.add_field(name="Notes", value="\n".join(notes)[:1024], inline=False)
    embed.set_footer(text="Per-item detail is in the attached CSV.")
    return embed


def build_overview_net_embed(overview: dict) -> discord.Embed:
    """Business-wide /ebay net-report embed: one line per pallet, plus eBay
    money that isn't tied to any tracked item."""
    embed = discord.Embed(
        title="🧾 eBay Net vs. Recorded - All Pallets",
        description=(
            "Read-only comparison - **nothing here changes any recorded number**, the pinned cards, or QuickBooks.\n"
            + _sync_note(overview["last_sync"])
        ),
        color=discord.Color.teal(),
    )
    lines = []
    total_recorded = total_net = 0.0
    for report in overview["pallets"]:
        total_recorded += report["recorded_for_matched"]
        total_net += report["totals"]["net_payout"]
        lines.append(
            f"**{report['pallet']['name']}** ({len(report['matched'])}): recorded {_money(report['recorded_for_matched'])}"
            f" → eBay net {_money(report['totals']['net_payout'])}"
        )
    if lines:
        value = "\n".join(lines)
        if len(value) > 1024:
            value = value[:1000].rsplit("\n", 1)[0] + "\n… (see CSV)"
        embed.add_field(name="By pallet (matched eBay sales)", value=value, inline=False)
        embed.add_field(name="Total recorded", value=_money(round(total_recorded, 2)), inline=True)
        embed.add_field(name="Total eBay net", value=_money(round(total_net, 2)), inline=True)
    else:
        embed.add_field(name="By pallet", value="No synced eBay orders match any tracked item yet.", inline=False)

    other = []
    if overview["unmatched_lines"]:
        other.append(
            f"{len(overview['unmatched_lines'])} eBay order line(s) with no matching tracked item "
            f"(net {_money(overview['unmatched_net'])})"
        )
    if overview["orders_without_breakdown"]:
        other.append(
            f"{overview['orders_without_breakdown']} older order(s) with payout data but no SKU breakdown "
            f"(net {_money(overview['orders_without_breakdown_net'])})"
        )
    for label, amount in sorted(overview["account_level"].items(), key=lambda kv: -abs(kv[1])):
        other.append(f"{label.replace('_', ' ').title()}: {_money(-amount)}")
    if other:
        embed.add_field(name="eBay money not tied to any item", value="\n".join(other)[:1024], inline=False)
    embed.set_footer(text="Per-item detail is in the attached CSV.")
    return embed


_NET_CSV_FIELDS = [
    "pallet", "item_number", "ebay_orders", "recorded_sale_price", "ebay_item_price", "buyer_shipping",
    "ebay_fees", "shipping_labels", "refunds_adjustments", "ad_listing_fees", "ebay_net_payout",
    "recorded_minus_net", "net_is_estimate",
]


def build_net_report_csv(reports: list) -> bytes:
    """Per-item rows across the given pallet reports, as CSV bytes."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_NET_CSV_FIELDS)
    for report in reports:
        for e in report["matched"]:
            recorded = e["recorded"]
            writer.writerow([
                report["pallet"]["name"], e["item"]["item_number"], " ".join(e["orders"]),
                f"{recorded:.2f}" if recorded is not None else "",
                f"{e['item_price']:.2f}", f"{e['buyer_shipping']:.2f}", f"{e['ebay_fees']:.2f}",
                f"{e['shipping_labels']:.2f}", f"{e['refunds']:.2f}", f"{e['ad_fees']:.2f}",
                f"{e['net_payout']:.2f}",
                f"{recorded - e['net_payout']:.2f}" if recorded is not None else "",
                "yes" if e["estimate"] else "no",
            ])
    return buf.getvalue().encode("utf-8")


# ------------------------------------- refunds / account charges / payouts --
# See ebay_books.py. Same pattern as the fee cards: a DynamicItem button
# (survives restarts) that re-checks everything on click.

def _bullet_lines(rows: list, limit: int = 1000) -> str:
    text = NL.join(rows)
    return text if len(text) <= limit else text[:limit].rsplit(NL, 1)[0] + NL + "…"


def build_refund_card_embed(b: dict) -> discord.Embed:
    items = ", ".join(_item_label(i) for i in b["items"]) or "(no tracked items)"
    embed = discord.Embed(title="↩️ eBay refund to log", description=f"Order `{b['order_id']}` - {items}",
                          color=discord.Color.red())
    embed.add_field(name="On eBay", value=_bullet_lines([f"{c['label']}: {_money(c['amount'])}" for c in b["charges"]]),
                    inline=False)
    embed.add_field(name="Total", value=f"**{_money(b['total'])}**", inline=True)
    embed.add_field(name="Confirm will", value=(
        "• post a QuickBooks Journal Entry - debit refunds (lowers sales), credit Undeposited Funds" + NL +
        "• lower the pallet's revenue by this amount" + NL +
        "If the item came back, `/item move-back` puts it back up for sale."), inline=False)
    embed.set_footer(text="Nothing is sent to QuickBooks until you confirm.")
    return embed


def build_account_card_embed(b: dict) -> discord.Embed:
    month = datetime.strptime(b["month"], "%Y-%m").strftime("%B %Y")
    embed = discord.Embed(title=f"🧾 eBay account charges - {month}",
                          description="eBay charges not tied to any order (store subscription, listing fees, ...).",
                          color=discord.Color.orange())
    embed.add_field(name="Charges", value=_bullet_lines(
        [f"{c['date']} {c['label']}: {_money(c['amount'])}" for c in b["charges"]]), inline=False)
    embed.add_field(name="Total", value=f"**{_money(b['totals']['total'])}**", inline=True)
    embed.add_field(name="Confirm will", value=(
        "post a QuickBooks Journal Entry - debit the eBay fee/ad accounts, credit Undeposited Funds. "
        "A listing fee for a tracked item also goes on its pallet's cost."), inline=False)
    embed.set_footer(text="Nothing is sent to QuickBooks until you confirm.")
    return embed


def build_payout_card_embed(b: dict) -> discord.Embed:
    payout = b["payout"] or {}
    bank = payout.get("instrument_nickname") or "bank"
    if payout.get("instrument_last4"):
        bank += f" (…{payout['instrument_last4']})"
    embed = discord.Embed(title=f"🏦 eBay payout {_money(b['amount'])}",
                          description=f"Sent {(payout.get('payout_date') or '')[:10]} to {bank} · `{b['payout_id']}`",
                          color=discord.Color.green() if b["ready"] else discord.Color.gold())
    by_cat = b["booked_by_category"]
    labels = {"fees": "eBay fees", "shipping": "Labels", "ads": "Ads/listing fees", "refunds": "Refunds"}
    rows = [f"Sales ({len(b['sales'])}): {_money(b['sales_total'])}"]
    rows += [f"{labels.get(k, k)}: -{_money(v)}" for k, v in by_cat.items() if v]
    rows.append(f"**Undeposited Funds for this payout: {_money(b['expected'])}**")
    embed.add_field(name="What's in it", value=NL.join(rows), inline=False)
    if b["deposit"]:
        embed.add_field(name="Status", value="✅ Already recorded in QuickBooks.", inline=False)
    elif b["ready"]:
        embed.add_field(name="Status", value=(
            "✅ Everything in this payout is logged. Confirm records the QuickBooks Bank Deposit into the bank "
            "account; then **Match** it to the bank-feed deposit in QuickBooks."), inline=False)
    else:
        embed.add_field(name="Waiting on", value=_bullet_lines([f"• {p}" for p in b["problems"]]), inline=False)
    embed.set_footer(text="Confirm re-checks everything first. Nothing is sent to QuickBooks until you confirm.")
    return embed


class EbayRefundConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ebay_refund_confirm:(?P<order_id>.+)"):
    def __init__(self, order_id: str):
        super().__init__(discord.ui.Button(label="Confirm & log refund", style=discord.ButtonStyle.danger, emoji="↩️",
                                           custom_id=f"ebay_refund_confirm:{order_id}"))
        self.order_id = order_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["order_id"])

    async def callback(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await ebay_books.confirm_order_refund(self.order_id, actor_id=interaction.user.id)
        if not result["ok"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return
        await _finish_book_card(interaction, result["pallet_ids"])
        b = result["breakdown"]
        await finance_utils.post_to_finance_audit_log(
            interaction.client,
            f"↩️ **eBay refund** for order `{self.order_id}` - {_money(b['total'])} "
            f"(Journal Entry `{result['je_doc']}`: debit refunds, credit Undeposited Funds)")
        await interaction.followup.send(
            f"✅ Logged the {_money(b['total'])} refund for order `{self.order_id}` - Journal Entry "
            f"`{result['je_doc']}`. The pallet's revenue is lowered to match.", ephemeral=True)


class EbayAccountChargesButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ebay_account_confirm:(?P<month>\d{4}-\d{2})"):
    def __init__(self, month: str):
        super().__init__(discord.ui.Button(label="Confirm & log charges", style=discord.ButtonStyle.success, emoji="🧾",
                                           custom_id=f"ebay_account_confirm:{month}"))
        self.month = month

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["month"])

    async def callback(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await ebay_books.confirm_account_charges(self.month, actor_id=interaction.user.id)
        if not result["ok"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return
        await _finish_book_card(interaction, result["pallet_ids"])
        total = result["breakdown"]["totals"]["total"]
        await finance_utils.post_to_finance_audit_log(
            interaction.client,
            f"🧾 **eBay account charges** for {self.month} - {_money(total)} (Journal Entry `{result['je_doc']}`; "
            "credited Undeposited Funds)")
        await interaction.followup.send(
            f"✅ Logged {_money(total)} of eBay account charges for {self.month} - Journal Entry `{result['je_doc']}`.",
            ephemeral=True)


class EbayPayoutConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ebay_payout_confirm:(?P<payout_id>.+)"):
    def __init__(self, payout_id: str):
        super().__init__(discord.ui.Button(label="Confirm & record deposit", style=discord.ButtonStyle.success, emoji="🏦",
                                           custom_id=f"ebay_payout_confirm:{payout_id}"))
        self.payout_id = payout_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["payout_id"])

    async def callback(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await ebay_books.confirm_payout(self.payout_id, actor_id=interaction.user.id)
        if not result["ok"]:
            await interaction.followup.send(result["message"][:1900], ephemeral=True)
            return
        await _finish_book_card(interaction, [], embed=build_payout_card_embed(ebay_books.payout_breakdown(self.payout_id)))
        b = result["breakdown"]
        kind = "Bank Deposit" if result["txn_type"] == "Deposit" else "Journal Entry"
        await finance_utils.post_to_finance_audit_log(
            interaction.client,
            f"🏦 **eBay payout** `{self.payout_id}` - {_money(b['amount'])} recorded into the bank account "
            f"({kind} `{result['doc']}`; {len(b['sales'])} sale(s))")
        note = ""
        if result["fallback"]:
            note = (NL + NL + "QuickBooks wouldn't take a linked Bank Deposit, so this was recorded as a Journal Entry "
                    "(bank up, Undeposited Funds down - same balances). The Sales Receipts will still be listed on "
                    "QuickBooks' Bank Deposit screen - **don't deposit them again** there.")
        await interaction.followup.send(
            f"✅ Recorded the {_money(b['amount'])} eBay payout as {kind} `{result['doc']}`. In QuickBooks → Banking, "
            "**Match** the bank-feed deposit to it." + note, ephemeral=True)


async def _finish_book_card(interaction: discord.Interaction, pallet_ids: list, embed: discord.Embed = None):
    try:
        if embed is not None:
            await interaction.message.edit(embed=embed, view=None)
        else:
            await interaction.message.edit(view=None)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        pass
    for pallet_id in pallet_ids:
        await finance_utils.refresh_finance_message(interaction.client, pallet_id)


async def _post_book_card(bot: discord.Client, embed: discord.Embed, button) -> bool:
    channel_id = db.get_shared_channel_id("ebay-orders")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        log.warning("eBay %s card not posted - #ebay-orders isn't set up.", embed.title)
        return False
    view = None
    if button is not None:
        view = discord.ui.View(timeout=None)
        view.add_item(button)
    try:
        await channel.send(embed=embed, view=view)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        log.exception("Couldn't post eBay card %s - will retry next run", embed.title)
        return False
    return True


async def _auto_book(bot: discord.Client, kind: str, key: str, amount, confirm, audit):
    """Shared by the refund / account / payout cards: books it by itself once
    if allowed. Returns (embed note, manual reason)."""
    if not config.EBAY_AUTO_BOOK:
        return None, None
    if not _auto_allowed(amount):
        return None, f"it's over the ${config.EBAY_AUTO_BOOK_MAX:,.0f} automatic limit"
    if not db.claim_auto_book(kind, key):
        return None, "the bot already tried this one - check it by hand"
    result = await confirm()
    db.finish_auto_book(kind, key, result.get("message") or "booked")
    if not result["ok"]:
        return None, result["message"]
    for pallet_id in result.get("pallet_ids") or []:
        await finance_utils.refresh_finance_message(bot, pallet_id)
    note = audit(result)
    await finance_utils.post_to_finance_audit_log(bot, note[1])
    return note[0], None


async def post_refund_card(bot: discord.Client, b: dict) -> bool:
    embed = build_refund_card_embed(b)
    auto_note, reason = await _auto_book(
        bot, "refund", b["order_id"], b["total"],
        lambda: ebay_books.confirm_order_refund(b["order_id"], actor_id=AUTO_ACTOR),
        lambda r: (f"Journal Entry {r['je_doc']}; the pallet's revenue is lowered to match.",
                   f"↩️ **eBay refund** for order `{b['order_id']}` - {_money(b['total'])} booked automatically "
                   f"(Journal Entry `{r['je_doc']}`)"))
    if auto_note:
        _mark_auto(embed, auto_note)
    elif reason:
        _mark_manual(embed, reason)
    if await _post_book_card(bot, embed, None if auto_note else EbayRefundConfirmButton(b["order_id"])):
        db.mark_ebay_book_carded("refund", ebay_books.refund_card_keys(b))
        return True
    return False


async def post_account_card(bot: discord.Client, b: dict) -> bool:
    embed = build_account_card_embed(b)
    auto_note, reason = await _auto_book(
        bot, "account", b["month"], b["totals"]["total"],
        lambda: ebay_books.confirm_account_charges(b["month"], actor_id=AUTO_ACTOR),
        lambda r: (f"Journal Entry {r['je_doc']}.",
                   f"🧾 **eBay account charges** for {b['month']} - {_money(b['totals']['total'])} booked "
                   f"automatically (Journal Entry `{r['je_doc']}`)"))
    if auto_note:
        _mark_auto(embed, auto_note)
    elif reason:
        _mark_manual(embed, reason)
    if await _post_book_card(bot, embed, None if auto_note else EbayAccountChargesButton(b["month"])):
        db.mark_ebay_book_carded("account", ebay_books.account_card_keys(b))
        return True
    return False


def _payout_note(r: dict) -> tuple:
    kind = "Bank Deposit" if r["txn_type"] == "Deposit" else "Journal Entry"
    extra = (" QuickBooks wouldn't take a linked deposit, so it's a Journal Entry - don't deposit these Sales "
             "Receipts again on QuickBooks' Bank Deposit screen." if r["fallback"] else "")
    return (f"{kind} {r['doc']} into the bank account - Match it to the bank-feed deposit in QuickBooks.{extra}",
            f"🏦 **eBay payout** `{r['breakdown']['payout_id']}` - {_money(r['breakdown']['amount'])} recorded "
            f"automatically ({kind} `{r['doc']}`)")


async def post_payout_card(bot: discord.Client, b: dict) -> bool:
    auto_note = reason = None
    if not b["deposit"] and b["ready"]:
        auto_note, reason = await _auto_book(
            bot, "payout", b["payout_id"], b["amount"],
            lambda: ebay_books.confirm_payout(b["payout_id"], actor_id=AUTO_ACTOR), _payout_note)
        if auto_note:
            b = ebay_books.payout_breakdown(b["payout_id"])
    embed = build_payout_card_embed(b)
    if auto_note:
        _mark_auto(embed, auto_note)
    elif reason:
        _mark_manual(embed, reason)
    button = None if (b["deposit"] or auto_note) else EbayPayoutConfirmButton(b["payout_id"])
    if await _post_book_card(bot, embed, button):
        db.mark_ebay_book_carded("payout", [b["payout_id"]])
        return True
    return False


async def _run_book_cards(bot: discord.Client, since_date: str = None) -> dict:
    """Refund, account-charge and payout cards that are due. Expects the
    eBay data to be fresh (_run_fee_cards syncs it first)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since_date = since_date or config.EBAY_FEE_CARDS_START_DATE or db.get_ebay_fee_card_start_date(today)
    posted = {"refund": 0, "account": 0, "payout": 0}
    for b in ebay_books.orders_needing_refund_cards(since_date):
        posted["refund"] += await post_refund_card(bot, b)
    # A month's account card waits for the month to end, so it's one card.
    for month in ebay_books.account_charge_months(before_month=today[:7]):
        if month < since_date[:7]:
            continue
        b = ebay_books.account_breakdown(month)
        if b["charges"] and any(not db.is_ebay_book_carded("account", k) for k in ebay_books.account_card_keys(b)):
            posted["account"] += await post_account_card(bot, b)
    if config.EBAY_PAYOUT_CARDS:
        for payout in ebay_books.payouts_needing_cards(since_date):
            posted["payout"] += await post_payout_card(bot, ebay_books.payout_breakdown(payout["payout_id"]))
    return posted


class Ebay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not config.PRESERVE_DISCORD_HISTORY:
            self.order_poll_loop.start()
            self.fee_card_loop.start()
            self.price_sync_loop.start()
            self.price_card_loop.start()
            self.catalog_snapshot_loop.start()

    async def cog_load(self):
        # Registers the (order_id -> button) template once so every
        # #ebay-orders card's Confirm button keeps working across bot
        # restarts, not just the ones posted during this process's
        # lifetime - see EbayOrderConfirmButton. Same for the fee cards'
        # Confirm & log fees button.
        if not config.PRESERVE_DISCORD_HISTORY:
            self.bot.add_dynamic_items(EbayOrderConfirmButton, EbayFeeConfirmButton, EbayRefundConfirmButton,
                                       EbayAccountChargesButton, EbayPayoutConfirmButton)

    def cog_unload(self):
        self.order_poll_loop.cancel()
        self.fee_card_loop.cancel()
        self.price_sync_loop.cancel()
        self.price_card_loop.cancel()
        self.catalog_snapshot_loop.cancel()

    @tasks.loop(minutes=config.EBAY_PRICE_SYNC_MINUTES)
    async def price_sync_loop(self):
        """Keeps the bot's prices in step with eBay (see price_sync.py)."""
        if config.PRESERVE_DISCORD_HISTORY or not (config.EBAY_ENABLED and config.EBAY_PRICE_SYNC):
            return
        try:
            await price_sync.sync_prices(self.bot)
        except Exception:
            log.exception("eBay price sync failed - will retry next run")

    @price_sync_loop.before_loop
    async def _before_price_sync_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def price_card_loop(self):
        """Shows synced prices on the #listed cards a few at a time (see
        price_sync.update_queued_cards) - nothing to do most runs."""
        if config.PRESERVE_DISCORD_HISTORY:
            return
        try:
            await price_sync.update_queued_cards(self.bot)
        except Exception:
            log.exception("Updating card prices failed - will retry next run")

    @tasks.loop(minutes=config.EBAY_CATALOG_SYNC_MINUTES)
    async def catalog_snapshot_loop(self):
        """Keeps the website catalog's view of live eBay listings fresh (see
        ebay_catalog.py) - one GetMyeBaySelling pass per 200 listings."""
        if config.PRESERVE_DISCORD_HISTORY or not config.EBAY_ENABLED:
            return
        try:
            await ebay_catalog.refresh_snapshot(reuse_seconds=60)
        except ebay_catalog.CatalogUnavailable:
            pass  # already logged; the catalog answers 503 once the last pass is too old
        except Exception:
            log.exception("eBay catalog snapshot failed - will retry next run")

    @catalog_snapshot_loop.before_loop
    async def _before_catalog_snapshot_loop(self):
        await self.bot.wait_until_ready()

    @price_card_loop.before_loop
    async def _before_price_card_loop(self):
        await self.bot.wait_until_ready()
        # Re-check cards a rate-limited or interrupted earlier pass may have
        # left showing the old price (unchanged cards aren't edited).
        try:
            db.queue_recently_synced_cards()
        except Exception:
            log.exception("Couldn't queue recently synced card prices")

    @tasks.loop(minutes=config.EBAY_FEE_SYNC_MINUTES)
    async def fee_card_loop(self):
        """Refreshes eBay's fee data and posts any due "💸 eBay fees" cards
        (see _run_fee_cards / ebay_fee_expenses.py). Needs the sell.finances
        scope - without it each run just logs the error and tries again."""
        if config.PRESERVE_DISCORD_HISTORY or not (config.EBAY_ENABLED and config.EBAY_FEE_CARDS):
            return
        await _run_fee_cards(self.bot)
        try:
            await _run_book_cards(self.bot)
        except Exception:
            log.exception("Posting eBay refund/account/payout cards failed - will retry next run")

    @fee_card_loop.before_loop
    async def _before_fee_card_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=config.EBAY_ORDER_POLL_MINUTES)
    async def order_poll_loop(self):
        """
        Checks eBay for new orders every EBAY_ORDER_POLL_MINUTES and posts
        a confirm card (to #ebay-orders) for any not already seen
        (database.ebay_seen_orders is the dedup ledger, same reasoning as
        cogs/finance.py's credit_card_poll_loop and
        quickbooks_seen_charges - a backdated/edited order shouldn't slip
        past a narrow date filter). No-ops entirely until EBAY_ENABLED -
        and needs EBAY_REFRESH_TOKEN to actually carry the sell.fulfillment
        scope (a wider re-consent than listing creation needs), which
        get_orders() surfaces as an EbayAPIError if it doesn't, same as any
        other live eBay failure here: caught, logged, retried next poll.

        An order with no SKU-matching line items at all is marked seen
        without posting a card - there's nothing to confirm, and no point
        re-checking the same unmatched order on every future poll either.
        """
        if config.PRESERVE_DISCORD_HISTORY:
            return
        if not config.EBAY_ENABLED:
            return
        try:
            since = datetime.now(timezone.utc) - timedelta(days=3)
            orders = await ebay_api.get_orders(since)
        except ebay_api.EbayAPIError:
            log.exception("eBay order poll failed")
            orders = []

        for order in orders:
            if db.has_seen_ebay_order(order["order_id"]):
                continue
            matched_items = _match_order_line_items(order)
            if not matched_items:
                db.mark_ebay_order_seen(order["order_id"])
                continue
            try:
                await _handle_new_order(self.bot, order, matched_items)
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                log.exception("Failed to post eBay order %s - will retry next poll", order["order_id"])
                continue
            db.mark_ebay_order_seen(order["order_id"])

        if config.EBAY_ORDER_AUTOMATION:
            await _run_order_automation(self.bot)

    @order_poll_loop.before_loop
    async def _before_order_poll_loop(self):
        await self.bot.wait_until_ready()

    ebay_group = app_commands.Group(name="ebay", description="eBay tools - listing lookups, status, fees")

    @ebay_group.command(
        name="category-search",
        description="Search eBay's real category list by keyword (for Edit Details' category).",
    )
    @app_commands.describe(query="A few words describing the item type, e.g. 'cordless drill'")
    async def category_search(self, interaction: discord.Interaction, query: str):
        if not await _require_admin(interaction):
            return
        # search_categories() is a live eBay API call - defer first so a
        # slow lookup (or a timeout-and-fallback) doesn't show a failed
        # interaction before the real response lands.
        await interaction.response.defer(ephemeral=True)
        matches = await ebay_api.search_categories(query, limit=10)
        if not matches:
            await interaction.followup.send(f"No eBay categories matched `{query}`.", ephemeral=True)
            return
        lines = "\n".join(f"`{category_id}` - {path}" for category_id, path in matches)
        await interaction.followup.send(f"Categories matching `{query}`:\n{lines}", ephemeral=True)

    @ebay_group.command(
        name="order-card",
        description="Post the Confirm & log sale card for one eBay order (if its card never appeared).",
    )
    @app_commands.describe(order_id="The eBay order number, e.g. 11-15276-35371")
    async def order_card(self, interaction: discord.Interaction, order_id: str):
        """Posts the same #ebay-orders card the order sync would have, for
        an order whose card never appeared. Nothing is logged until someone
        clicks the card. Refuses if any matched item's sale is already
        logged, so it can't lead to logging a sale twice."""
        if not await _require_finance_role(interaction):
            return
        if not config.EBAY_ENABLED:
            await interaction.response.send_message("The eBay API isn't configured.", ephemeral=True)
            return
        order_id = order_id.strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            order = await ebay_api.get_order(order_id)
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(f"Couldn't get eBay order `{order_id}`: {str(e)[:1500]}", ephemeral=True)
            return
        matched = _match_order_line_items(order)
        if not matched:
            await interaction.followup.send(
                f"None of eBay order `{order_id}`'s items match a tracked item (by SKU or eBay listing ID).",
                ephemeral=True,
            )
            return
        logged = [i for i in matched if db.get_item_sale(i["id"])]
        if logged:
            names = ", ".join(_item_label(i) for i in logged)
            await interaction.followup.send(
                f"{names} already has a logged sale - no card posted. See `/finance sales` "
                f"(and `/finance reverse-sale` if that logged sale is wrong).",
                ephemeral=True,
            )
            return
        channel_id = db.get_shared_channel_id("ebay-orders")
        if not channel_id or self.bot.get_channel(channel_id) is None:
            await interaction.followup.send("#ebay-orders isn't set up - run `/setup shared-channels`.", ephemeral=True)
            return
        await _post_ebay_order_match(self.bot, order, matched)
        db.mark_ebay_order_seen(order_id)
        await interaction.followup.send(
            f"Posted the Confirm & log sale card for order `{order_id}` "
            f"({', '.join(_item_label(i) for i in matched)}, ${order['total_price']:.2f}) in <#{channel_id}>.",
            ephemeral=True,
        )

    @ebay_group.command(
        name="fee-cards",
        description="Post 'eBay fees to log' cards for older eBay orders (from a date). Books nothing itself.",
    )
    @app_commands.describe(since="Orders placed on/after this date, as YYYY-MM-DD (run /ebay sync-finances first)")
    async def fee_cards(self, interaction: discord.Interaction, since: str):
        if not await _require_admin(interaction):
            return
        try:
            datetime.strptime(since.strip(), "%Y-%m-%d")
        except ValueError:
            await interaction.response.send_message("`since` must be a date like `2026-01-31`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = await _run_fee_cards(self.bot, since_date=since.strip(), sync=False)
        await interaction.followup.send(
            f"Posted **{posted}** eBay fee card(s) to #ebay-orders for orders since {since.strip()}. "
            "Only orders whose sale is already logged get one; nothing is booked until someone confirms. "
            "(Uses the last `/ebay sync-finances` data - run that first for older orders.)",
            ephemeral=True,
        )

    @ebay_group.command(
        name="sync-prices",
        description="Copy each Listed item's current eBay price into the bot (Discord, website, dashboard) now.",
    )
    async def sync_prices_cmd(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await price_sync.sync_prices(self.bot)
        lines = []
        linked = result.get("linked")
        if linked:
            newly = len(linked["linked"]) + len(linked["created"])
            lines.append(f"Matched {linked['listings']} active eBay listing(s) to items by Custom label - "
                         f"{newly} newly linked.")
            if linked["not_listed"]:
                lines.append(f"⚠️ {len(linked['not_listed'])} are live on eBay but not **Listed** in the bot: " +
                             ", ".join(f"#{i['item_number']} ({i['status']})" for i, _ in linked["not_listed"][:10]))
            if linked["conflicts"]:
                lines.append(f"⚠️ {len(linked['conflicts'])} have a different eBay listing ID on file (left alone): " +
                             ", ".join(f"#{i['item_number']}" for i, _ in linked["conflicts"][:10]))
            if linked["unmatched"]:
                lines.append(f"{len(linked['unmatched'])} eBay listing(s) have no matching item (no/unknown Custom label).")
        elif linked is None:
            lines.append("⚠️ Couldn't fetch the eBay listing list - only already-linked items were checked.")
        lines.append(f"Checked {result['checked']} listed item(s) against eBay - "
                     f"{len(result['changed'])} price(s) updated.")
        for item, old, new in result["changed"][:15]:
            pallet = (db.get_pallet(item["pallet_id"]) or {}).get("name", "?")
            lines.append(f"• {pallet} #{item['item_number']}: {_money(old) if old is not None else '-'} → {_money(new)}")
        if len(result["changed"]) > 15:
            lines.append(f"… and {len(result['changed']) - 15} more")
        if result["errors"]:
            lines.append(f"⚠️ {len(result['errors'])} couldn't be checked: " + "; ".join(result["errors"][:3]))
        if result.get("cards_pending"):
            lines.append(f"The bot's prices are already updated; {result['cards_pending']} Discord card(s) will "
                         "show the new price over the next while (Discord limits how fast old messages can be edited).")
        await interaction.followup.send(NL.join(lines)[:1900], ephemeral=True)

    @ebay_group.command(
        name="refund-cards",
        description="Post refund cards now for eBay refunds/adjustments on logged sales.",
    )
    @app_commands.describe(since="Only orders placed on/after this date, YYYY-MM-DD (default: the fee-card start date)")
    async def refund_cards(self, interaction: discord.Interaction, since: str = None):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await ebay_finances.sync(30)
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(f"⚠️ Couldn't refresh eBay data: {e}", ephemeral=True)
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        since = since or config.EBAY_FEE_CARDS_START_DATE or db.get_ebay_fee_card_start_date(today)
        posted = 0
        for b in ebay_books.orders_needing_refund_cards(since):
            posted += await post_refund_card(self.bot, b)
        await interaction.followup.send(f"Posted {posted} eBay refund card(s) to #ebay-orders.", ephemeral=True)

    @ebay_group.command(
        name="account-charges",
        description="Post the eBay account-charges card (store subscription, listing fees) for a month.",
    )
    @app_commands.describe(month="YYYY-MM (default: last month). The current month works too, for charges so far.")
    async def account_charges_cmd(self, interaction: discord.Interaction, month: str = None):
        if not await _require_finance_role(interaction):
            return
        now = datetime.now(timezone.utc)
        if month is None:
            first = now.replace(day=1)
            month = (first - timedelta(days=1)).strftime("%Y-%m")
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            await interaction.response.send_message("Month must look like `2026-09`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await ebay_finances.sync(max(31, (now - datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)).days + 1))
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(f"⚠️ Couldn't refresh eBay data: {e}", ephemeral=True)
            return
        b = ebay_books.account_breakdown(month)
        if not b["charges"]:
            await interaction.followup.send(f"No unlogged eBay account charges for {month}.", ephemeral=True)
            return
        ok = await post_account_card(self.bot, b)
        await interaction.followup.send(
            f"Posted the {month} account-charges card ({_money(b['totals']['total'])}) to #ebay-orders." if ok
            else "Couldn't post the card - is #ebay-orders set up?", ephemeral=True)

    @ebay_group.command(
        name="payouts",
        description="Recent eBay payouts and whether each is recorded in QuickBooks.",
    )
    async def payouts_cmd(self, interaction: discord.Interaction):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await ebay_finances.sync(60)
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(f"⚠️ Couldn't refresh eBay data: {e}", ephemeral=True)
            return
        rows = []
        for p in db.list_ebay_payouts(limit=15):
            b = ebay_books.payout_breakdown(p["payout_id"])
            if b["deposit"]:
                state = "✅ in QuickBooks"
            elif p.get("payout_status") != "SUCCEEDED":
                state = f"⏳ {(p.get('payout_status') or 'pending').lower()}"
            elif b["ready"]:
                state = "🟢 ready - `/ebay payout-card`"
            else:
                state = f"⚠️ waiting on {len(b['problems'])} thing(s)"
            rows.append(f"`{p['payout_id']}` {(p.get('payout_date') or '')[:10]} · {_money(p.get('amount') or 0)} · {state}")
        await interaction.followup.send(
            ("**eBay payouts (newest first):**" + NL + NL.join(rows))[:1900] if rows else "No eBay payouts synced yet.",
            ephemeral=True)

    @ebay_group.command(
        name="payout-card",
        description="Post (or re-post) the deposit card for one eBay payout.",
    )
    @app_commands.describe(payout_id="The payout ID from /ebay payouts")
    async def payout_card_cmd(self, interaction: discord.Interaction, payout_id: str):
        if not await _require_finance_role(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await ebay_finances.sync(60)
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(f"⚠️ Couldn't refresh eBay data: {e}", ephemeral=True)
            return
        b = ebay_books.payout_breakdown(payout_id.strip())
        if b["payout"] is None:
            await interaction.followup.send(f"No eBay payout `{payout_id}` found - check `/ebay payouts`.", ephemeral=True)
            return
        ok = await post_payout_card(self.bot, b)
        await interaction.followup.send("Posted the payout card to #ebay-orders." if ok
                                        else "Couldn't post the card - is #ebay-orders set up?", ephemeral=True)

    @ebay_group.command(
        name="status",
        description="Check every eBay permission and QuickBooks setting the bot needs. Read-only.",
    )
    async def status(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ebay_results = await status_checks.ebay_checks()
        qb_results = await status_checks.quickbooks_checks()
        problems = sum(1 for c in ebay_results + qb_results if c.state is False)
        embed = discord.Embed(
            title="🩺 Bot connection status",
            description="All good ✅" if not problems else f"**{problems} problem(s)** - see ❌ below.",
            color=discord.Color.green() if not problems else discord.Color.red(),
        )
        embed.add_field(name="eBay", value=status_checks.format_checks(ebay_results)[:1024], inline=False)
        embed.add_field(name="QuickBooks", value=status_checks.format_checks(qb_results)[:1024], inline=False)
        embed.set_footer(text="Read-only check - nothing was created or changed.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @ebay_group.command(
        name="category-aspects",
        description="Show the item specifics eBay requires/recommends for a category (from eBay's own list).",
    )
    @app_commands.describe(category_id="An eBay leaf category ID (see /ebay category-search)")
    async def category_aspects(self, interaction: discord.Interaction, category_id: str):
        if not await _require_admin(interaction):
            return
        if not config.EBAY_ENABLED:
            await interaction.response.send_message("The eBay API isn't configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        aspects = await ebay_aspects.get_category_aspects(category_id.strip())
        if not aspects:
            await interaction.followup.send(
                f"Couldn't get item specifics for category `{category_id}` from eBay - check the ID "
                "(`/ebay category-search`) and try again.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(build_category_aspects_text(category_id.strip(), aspects), ephemeral=True)

    @ebay_group.command(
        name="sync-finances",
        description="Pull eBay's fees/shipping/refunds/payouts (read-only - changes no recorded numbers).",
    )
    @app_commands.describe(days_back="How far back to pull, in days (default 1825 = eBay's 5-year maximum)")
    async def sync_finances(self, interaction: discord.Interaction, days_back: int = 1825):
        """
        Fills the read-only eBay mirror tables (see ebay_finances.sync) -
        never writes to any existing sale/cost/item table, card, or
        QuickBooks. Safe to re-run: rows are keyed on eBay's own IDs.
        """
        if not await _require_admin(interaction):
            return
        if not config.EBAY_ENABLED:
            await interaction.response.send_message(
                "The eBay API isn't configured (`EBAY_APP_ID`/`EBAY_CERT_ID`/`EBAY_REFRESH_TOKEN` in `.env`).",
                ephemeral=True,
            )
            return
        if days_back < 1:
            await interaction.response.send_message("`days_back` must be at least 1.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await ebay_finances.sync(days_back, actor_id=interaction.user.id)
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(
                f"⚠️ eBay sync stopped: {str(e)[:1500]}\n"
                "Anything already pulled before the error is kept. No recorded numbers were changed.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ Synced **{result['transactions']}** eBay money transaction(s) since "
            f"{result['transactions_from']:%Y-%m-%d} and **{result['orders']}** order breakdown(s) since "
            f"{result['orders_from']:%Y-%m-%d} (eBay keeps order detail for 2 years).\n"
            "No recorded sale, cost, card, or QuickBooks entry was changed. Run `/ebay net-report` to compare.",
            ephemeral=True,
        )

    @ebay_group.command(
        name="net-report",
        description="Compare recorded eBay revenue with eBay's real fees and payout. Changes nothing.",
    )
    @app_commands.describe(pallet_name="Pallet name (omit for every pallet; inside a pallet's channels, that pallet)")
    async def net_report(self, interaction: discord.Interaction, pallet_name: str = None):
        if not await _require_finance_role(interaction):
            return

        pallet = None
        if pallet_name:
            pallet = db.get_pallet_by_name(pallet_name.strip())
            if not pallet:
                await interaction.response.send_message(f"No pallet named `{pallet_name}` found.", ephemeral=True)
                return
        elif getattr(interaction.channel, "category_id", None):
            pallet = db.get_pallet_by_category(interaction.channel.category_id)

        await interaction.response.defer(ephemeral=True, thinking=True)
        last_sync = db.get_last_successful_ebay_finance_sync()
        if pallet:
            report = ebay_finances.build_pallet_report(pallet["id"])
            embed = build_pallet_net_embed(report, last_sync)
            reports = [report]
            filename = f"ebay-net-{pallet['name']}.csv"
        else:
            overview = ebay_finances.build_overview()
            embed = build_overview_net_embed(overview)
            reports = overview["pallets"]
            filename = "ebay-net-all-pallets.csv"
        csv_file = discord.File(io.BytesIO(build_net_report_csv(reports)), filename=filename)
        await interaction.followup.send(embed=embed, file=csv_file, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ebay(bot))
