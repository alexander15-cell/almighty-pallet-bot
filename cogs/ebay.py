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
import ebay_fee_expenses
import ebay_finances
import ebay_listing_rules
import finance_utils
import runtime_settings
import status_checks

log = logging.getLogger(__name__)


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

        try:
            order = await ebay_api.get_order(self.order_id)
        except ebay_api.EbayAPIError as e:
            await interaction.followup.send(f"Couldn't re-fetch this order from eBay: {e}", ephemeral=True)
            return

        matched_items = _match_order_line_items(order)
        if not matched_items:
            await interaction.followup.send(
                "None of this order's items match a tracked item anymore - nothing was submitted.",
                ephemeral=True,
            )
            return

        # Re-checked here, not just when the poll loop first matched this
        # order - the card can sit for a while, and another sale could
        # have claimed one of these items in the meantime, same reasoning
        # as /finance log-sale's own confirmation step.
        already_sold = [item for item in matched_items if db.get_item_sale(item["id"])]
        if already_sold:
            names = ", ".join(f"#{item['item_number']}" for item in already_sold)
            await interaction.followup.send(
                f"Item(s) {names} were already logged in another sale - nothing was submitted.",
                ephemeral=True,
            )
            return

        try:
            await interaction.message.edit(view=None)
        except discord_resilience.TRANSIENT_DISCORD_ERRORS:
            pass

        n = len(matched_items)
        allocated_shares = finance_utils.split_evenly(order["total_price"], n)
        sale_id = db.create_sale("eBay", order["sale_date"], order["total_price"], False, created_by=interaction.user.id)

        affected_pallets = set()
        for i, item in enumerate(matched_items):
            db.add_sale_item(sale_id, item["id"], allocated_shares[i])
            db.record_item_sale(item["id"], allocated_shares[i], "eBay", actor_id=interaction.user.id)
            affected_pallets.add(item["pallet_id"])

        for pallet_id in affected_pallets:
            await finance_utils.refresh_finance_message(interaction.client, pallet_id)

        result = await finance_utils.post_sale_to_quickbooks(
            sale_id, "eBay", order["total_price"], False, order["sale_date"],
        )
        await finance_utils.update_accounting_entries(
            interaction.client, sale_id, receipt_doc=result.get("sales_receipt_doc"), error=result.get("error"),
        )
        item_list = ", ".join(
            f"{db.get_pallet(item['pallet_id'])['name']}#{item['item_number']}" for item in matched_items
        )

        if result["ok"]:
            message = (
                f"✅ Logged sale #{sale_id} ({item_list}) via **eBay** (order `{self.order_id}`).\n"
                f"Sales Receipt: `{result['sales_receipt_doc']}`"
            )
            await finance_utils.post_to_finance_audit_log(
                interaction.client,
                f"💰 **Sale #{sale_id}** ({item_list}) via **eBay** - ${order['total_price']:.2f} "
                f"(Sales Receipt `{result['sales_receipt_doc']}`) - via eBay order auto-sync",
            )
        else:
            message = (
                f"⚠️ Sale #{sale_id} saved, but QuickBooks failed: {result['error']}\n"
                f"Run `/finance retry-sale sale_id:{sale_id}` once it's fixed - nothing was lost."
            )
        await interaction.followup.send(message, ephemeral=True)


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


async def _post_ebay_order_match(bot: discord.Client, order: dict, matched_items: list):
    """Posts one newly-seen eBay order to #ebay-orders with its Confirm &
    log sale button - see order_poll_loop that calls this."""
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
    embed.set_footer(text="Nothing is sent to QuickBooks until you confirm.")
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


async def _post_fee_card(bot: discord.Client, breakdown: dict) -> bool:
    channel_id = db.get_shared_channel_id("ebay-orders")
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        log.warning("eBay fee card for order %s not posted - #ebay-orders isn't set up.", breakdown["order_id"])
        return False
    view = discord.ui.View(timeout=None)
    view.add_item(EbayFeeConfirmButton(breakdown["order_id"]))
    try:
        await channel.send(embed=build_fee_card_embed(breakdown), view=view)
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
        await _post_ebay_order_match(bot, order, matched)
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


class Ebay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not config.PRESERVE_DISCORD_HISTORY:
            self.order_poll_loop.start()
            self.fee_card_loop.start()

    async def cog_load(self):
        # Registers the (order_id -> button) template once so every
        # #ebay-orders card's Confirm button keeps working across bot
        # restarts, not just the ones posted during this process's
        # lifetime - see EbayOrderConfirmButton. Same for the fee cards'
        # Confirm & log fees button.
        if not config.PRESERVE_DISCORD_HISTORY:
            self.bot.add_dynamic_items(EbayOrderConfirmButton, EbayFeeConfirmButton)

    def cog_unload(self):
        self.order_poll_loop.cancel()
        self.fee_card_loop.cancel()

    @tasks.loop(minutes=config.EBAY_FEE_SYNC_MINUTES)
    async def fee_card_loop(self):
        """Refreshes eBay's fee data and posts any due "💸 eBay fees" cards
        (see _run_fee_cards / ebay_fee_expenses.py). Needs the sell.finances
        scope - without it each run just logs the error and tries again."""
        if config.PRESERVE_DISCORD_HISTORY or not (config.EBAY_ENABLED and config.EBAY_FEE_CARDS):
            return
        await _run_fee_cards(self.bot)

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
                await _post_ebay_order_match(self.bot, order, matched_items)
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
