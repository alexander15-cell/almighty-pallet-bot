"""
Admin commands for the eBay CSV batch fallback (see ebay_csv.py) - the path
used while there's no live eBay API integration:

/ebay export-batch  - hands the accumulated CSV to whoever manages eBay as a
                       Discord attachment, archives + clears it so the next
                       "Add to eBay Batch" click starts fresh, and records a
                       durable, numbered ebay_batches snapshot of exactly
                       which items went out in it (see database.
                       create_ebay_batch) - items.ebay_batch_id points back
                       to it. This is eBay's classic File Exchange "Add"
                       template (see ebay_csv.py) - ready to upload as-is,
                       built entirely from data Queue Review already
                       captured.
/ebay fill-recommendations - a separate, optional path: upload eBay's own
                       AI-prefill tool's returned recommendations file (see
                       ebay_recommendations.py) and get back the same file
                       with price/quantity/condition/format filled in from
                       what Queue Review already captured. Not part of the
                       normal export-batch flow above - only useful if
                       someone chooses to use eBay's separate Prefill
                       Listing tool by hand instead.
/ebay batches        - lists recent batches with how many items in each are
                       still waiting on a result.
/ebay batch          - shows one batch's still-pending items.
/ebay import-results - reconciles a batch against a results CSV downloaded
                       from Seller Hub (see ebay_results.py): rows it can
                       confidently match as succeeded move that item to
                       Listed and record the real eBay item ID; anything
                       else (no listing ID, an explicit error, an
                       unrecognized SKU) is reported back unresolved rather
                       than guessed at.
/ebay confirm-listed - the fully-manual fallback: once a CSV has actually
                       been uploaded and processed by eBay, moves the
                       item(s) that were in it from pending_ebay_upload to
                       Listed by hand, for anyone who'd rather just check
                       Seller Hub directly than download/upload a results
                       CSV.
/ebay category-search - looks up real eBay leaf category IDs by keyword
                       against eBay's own official taxonomy (see
                       ebay_taxonomy.py) - mainly for finding an ID to pass
                       to /ebay retry-item's category_id correction.
/ebay sync-finances  - pulls eBay's own fee/shipping/refund/payout records
                       into read-only mirror tables (see ebay_finances.py).
                       Never changes any existing sale, cost, card, or
                       QuickBooks entry.
/ebay net-report     - side-by-side: what's recorded vs. what eBay says
                       (fees, buyer-paid shipping, labels, refunds, ad
                       fees, net payout), per pallet or business-wide, with
                       a per-item CSV. Finance Management or Pallet Admin.

All of these require the Pallet Admin role, same as admin_tools.py (except
/ebay net-report, which Finance Management can also run). The
actual message/status moving is delegated to the ItemFlow cog (via
get_cog, same cross-cog pattern QueueReviewView/AwaitingListingView use) so
that logic stays in one place alongside the rest of the pipeline.

This file also owns order_poll_loop - not a command, a background
tasks.loop (same pattern as cogs/finance.py's credit_card_poll_loop) that
checks the direct eBay API for new orders and posts a #ebay-orders confirm
card for any matched to a tracked item by SKU (see
_match_order_line_items, ebay_csv.parse_custom_label). Needs EBAY_ENABLED
AND a refresh token carrying the sell.fulfillment scope - a wider
re-consent than the sell.inventory scope the listing-creation path needs
(see README's eBay API section). EbayOrderConfirmButton's click reuses
finance_utils.post_sale_to_quickbooks/split_evenly, the same functions
/finance log-sale uses, so a sale logged either way is handled identically.
"""
import asyncio
import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database as db
import discord_resilience
import ebay_api
import ebay_csv
import ebay_finances
import ebay_recommendations
import ebay_results
import finance_utils
import runtime_settings

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


def _parse_specifics(text: str) -> dict:
    """
    Parses "Key=Value, Key2=Value2" (as typed into /ebay retry-item's
    specifics option) into a dict. Deliberately simple (splits on commas,
    then the first "=" in each piece) - doesn't support a value containing
    a literal comma, which is an acceptable limit for a quick one-off fix,
    not a replacement for the full Queue Review specifics field.
    """
    result = {}
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"{piece!r} isn't in 'Key=Value' form")
        key, value = piece.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            raise ValueError(f"{piece!r} has no key before '='")
        result[key] = value
    if not result:
        raise ValueError("no 'Key=Value' pairs found")
    return result


def _parse_dimensions(text: str) -> tuple:
    """
    Parses "L x W x H" (as typed into /ebay retry-item's dimensions option)
    into three positive inch values, or raises ValueError with the exact
    message to show. Same "L x W x H" format/separator as EbayListingModal
    (item_flow.py's Queue Review approval step) so a reviewer only ever has
    to remember one format.
    """
    parts = [p.strip() for p in re.split(r"[xX×]", text) if p.strip()]
    if len(parts) != 3:
        raise ValueError("must be three numbers separated by 'x', e.g. `12 x 8 x 4`")
    try:
        length_in, width_in, height_in = (float(p) for p in parts)
    except ValueError:
        raise ValueError("must all be numbers, e.g. `12 x 8 x 4`")
    if any(d <= 0 for d in (length_in, width_in, height_in)):
        raise ValueError("must all be numbers greater than 0, e.g. `12 x 8 x 4`")
    return length_in, width_in, height_in


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
    Resolves an eBay order's line items back to tracked items by SKU
    (ebay_csv.parse_custom_label) - a line item with an unparseable SKU
    (not this bot's format) or no matching item (deleted, or from before
    this bot tracked it) is silently skipped, not an error: an order can
    still be worth confirming for whichever of its items DO match. Used by
    both the poll loop (to decide whether to post a card at all) and
    EbayOrderConfirmButton (to re-resolve fresh on click).
    """
    matched = []
    for line_item in order["line_items"]:
        parsed = ebay_csv.parse_custom_label(line_item["sku"])
        if not parsed:
            continue
        item = db.get_item_by_pallet_and_number(*parsed)
        if item:
            matched.append(item)
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

      - paid, not cancelled, item Listed/pending upload -> Sold
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
                        and item["status"] in (db.STATUS_LISTED, db.STATUS_PENDING_EBAY_UPLOAD) \
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
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                log.exception("eBay order automation failed for %s (order %s) - will retry next poll", label, order_id)
    return counts


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

    async def cog_load(self):
        # Registers the (order_id -> button) template once so every
        # #ebay-orders card's Confirm button keeps working across bot
        # restarts, not just the ones posted during this process's
        # lifetime - see EbayOrderConfirmButton.
        if not config.PRESERVE_DISCORD_HISTORY:
            self.bot.add_dynamic_items(EbayOrderConfirmButton)

    def cog_unload(self):
        self.order_poll_loop.cancel()

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

    ebay_group = app_commands.Group(name="ebay", description="Admin eBay CSV batch management")

    @ebay_group.command(name="export-batch", description="Download the accumulated eBay CSV batch and start a fresh one.")
    async def export_batch(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        pending_items = db.get_unbatched_pending_items()

        path = ebay_csv.export_and_archive()
        if path is None:
            await interaction.response.send_message("The eBay batch is empty - nothing to export.", ephemeral=True)
            return

        batch_id = db.create_ebay_batch(
            csv_filename=path.name, exported_by=interaction.user.id, item_ids=[i["id"] for i in pending_items],
        )

        pending_channel_id = db.get_shared_channel_id("pending-ebay-upload")
        pending_channel_mention = f"<#{pending_channel_id}>" if pending_channel_id else "#pending-ebay-upload"
        photo_url_note = (
            "PicURL is filled in from R2-hosted photo URLs where available."
            if config.R2_ENABLED else
            "PicURL is blank (R2 photo hosting isn't configured), so add photos yourself "
            "before or after uploading."
        )
        await interaction.response.send_message(
            f"📄 eBay batch **#{batch_id}** CSV attached ({len(pending_items)} item(s)). This is eBay's classic "
            f"File Exchange \"Add\" template, ready to upload as-is via the Upload tab in Seller Hub - review it "
            f"first (especially category, since that's what most often needs a manual correction), then upload. "
            f"{photo_url_note} The live batch has been cleared - the next **Add to eBay Batch** click starts a "
            f"new one. Once eBay processes it, either run `/ebay import-results batch_id:{batch_id}` with the "
            f"results CSV Seller Hub gives you, or `/ebay confirm-listed` by hand after checking Seller Hub, to "
            f"move these out of {pending_channel_mention}.",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

    @ebay_group.command(
        name="fill-recommendations",
        description="Upload eBay's returned recommendations file - fills in price/condition/format from Queue Review.",
    )
    @app_commands.describe(recommendations_file="The .xlsm file Seller Hub gave back after processing your batch upload")
    async def fill_recommendations(self, interaction: discord.Interaction, recommendations_file: discord.Attachment):
        if not await _require_admin(interaction):
            return
        if not recommendations_file.filename.lower().endswith((".xlsm", ".xlsx")):
            await interaction.response.send_message(
                "That doesn't look like an Excel file (.xlsm/.xlsx) - upload the file eBay gave you back "
                "after processing your batch, not the one you uploaded to it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_bytes = await recommendations_file.read()

        try:
            filled_path, summary = await asyncio.to_thread(ebay_recommendations.fill_recommendations_file, raw_bytes)
        except ebay_recommendations.RecommendationsFileError as e:
            await interaction.followup.send(f"⚠️ Couldn't process that file: {e}", ephemeral=True)
            return

        try:
            lines = [
                f"✅ Filled in {summary.filled_count} item(s) across {summary.sheet_count} category sheet(s) "
                f"with price/quantity/condition/format from Queue Review - never overwrote anything already there.",
            ]
            if summary.unmatched_skus:
                shown = ", ".join(summary.unmatched_skus[:10])
                lines.append(
                    f"⚠️ {len(summary.unmatched_skus)} row(s) had a SKU this bot doesn't recognize "
                    f"(not one of this bot's items, or already deleted): {shown}"
                )
            if not config.EBAY_ITEM_LOCATION:
                lines.append("ℹ️ `EBAY_ITEM_LOCATION` isn't set in `.env` - Location was left blank, fill it in by hand.")
            if not config.EBAY_SHIPPING_SERVICE:
                lines.append(
                    "ℹ️ `EBAY_SHIPPING_SERVICE` isn't set in `.env` - Shipping service was left blank, fill it in by hand."
                )
            lines.append(
                "Review before re-uploading to Seller Hub - dropdown pick-lists in the file may not survive "
                "re-saving through this tool, but the underlying data is unaffected; just type a value directly "
                "if a cell looks off."
            )
            await interaction.followup.send(
                "\n".join(lines),
                file=discord.File(filled_path, filename=recommendations_file.filename),
                ephemeral=True,
            )
        finally:
            filled_path.unlink(missing_ok=True)

    @ebay_group.command(name="batches", description="List recent eBay CSV batches and how many items in each are still pending.")
    async def batches(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        batches = db.list_ebay_batches()
        if not batches:
            await interaction.response.send_message("No eBay batches have been exported yet.", ephemeral=True)
            return

        lines = []
        for b in batches:
            still_pending = len(db.get_ebay_batch_items(b["id"]))
            lines.append(f"**#{b['id']}** - `{b['csv_filename']}` - {still_pending}/{b['item_count']} still pending")
        embed = discord.Embed(title="eBay Batches", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ebay_group.command(name="batch", description="Show one eBay batch's still-pending items.")
    @app_commands.describe(batch_id="The batch number shown by /ebay batches or /ebay export-batch")
    async def batch(self, interaction: discord.Interaction, batch_id: int):
        if not await _require_admin(interaction):
            return

        b = db.get_ebay_batch(batch_id)
        if not b:
            await interaction.response.send_message(f"No batch #{batch_id} found.", ephemeral=True)
            return

        pending_items = db.get_ebay_batch_items(batch_id)
        if not pending_items:
            await interaction.response.send_message(
                f"Batch #{batch_id} (`{b['csv_filename']}`, {b['item_count']} item(s)) - none still pending, "
                "everything from it has already been confirmed or moved on.",
                ephemeral=True,
            )
            return

        lines = [f"#{item['item_number']} (pallet {item['pallet_id']})" for item in pending_items[:25]]
        await interaction.response.send_message(
            f"Batch #{batch_id} (`{b['csv_filename']}`) - {len(pending_items)}/{b['item_count']} still pending: "
            + ", ".join(lines),
            ephemeral=True,
        )

    @ebay_group.command(name="import-results", description="Reconcile a batch against a results CSV downloaded from Seller Hub.")
    @app_commands.describe(
        batch_id="The batch number shown by /ebay batches or /ebay export-batch",
        results_csv="The results/report CSV Seller Hub gives you after processing the upload",
    )
    async def import_results(self, interaction: discord.Interaction, batch_id: int, results_csv: discord.Attachment):
        if not await _require_admin(interaction):
            return

        b = db.get_ebay_batch(batch_id)
        if not b:
            await interaction.response.send_message(f"No batch #{batch_id} found.", ephemeral=True)
            return

        pending_items = db.get_ebay_batch_items(batch_id)
        if not pending_items:
            await interaction.response.send_message(f"Batch #{batch_id} has no items still pending a result.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        csv_bytes = await results_csv.read()
        parsed = ebay_results.parse_results(csv_bytes)

        by_label = {ebay_csv.custom_label(item): item for item in pending_items}
        cog = self.bot.get_cog("ItemFlow")

        confirmed, confirmed_pallet_ids, unmatched_success, unmatched_failed = [], set(), [], []
        for row in parsed["succeeded"]:
            item = by_label.get(row["custom_label"])
            if not item:
                unmatched_success.append(row["custom_label"])
                continue
            if await cog.confirm_ebay_pending_item(item, actor_id=interaction.user.id, ebay_item_id=row["ebay_item_id"]):
                confirmed.append(f"#{item['item_number']} (pallet {item['pallet_id']})")
                confirmed_pallet_ids.add(item["pallet_id"])

        for row in parsed["failed"]:
            if row["custom_label"] not in by_label:
                unmatched_failed.append(row["custom_label"])

        for pallet_id in confirmed_pallet_ids:
            await finance_utils.refresh_finance_message(self.bot, pallet_id)

        lines = [f"✅ Confirmed {len(confirmed)} item(s) live: {', '.join(confirmed) or 'none'}."]
        if parsed["failed"]:
            failed_labels = ", ".join(r["custom_label"] for r in parsed["failed"][:10])
            lines.append(f"❌ {len(parsed['failed'])} row(s) reported failed/no listing ID: {failed_labels}")
        if unmatched_success or unmatched_failed:
            lines.append(
                f"⚠️ {len(unmatched_success) + len(unmatched_failed)} row(s) had a SKU not found in this "
                f"batch's still-pending items - check they belong to batch #{batch_id} and weren't already resolved."
            )
        if parsed["unparsed_rows"]:
            lines.append(f"⚠️ {parsed['unparsed_rows']} row(s) had no readable SKU column and were skipped.")
        still_pending_after = len(db.get_ebay_batch_items(batch_id))
        if still_pending_after:
            lines.append(f"{still_pending_after} item(s) in this batch are still unresolved - use `/ebay batch batch_id:{batch_id}` to see them.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @ebay_group.command(
        name="confirm-listed",
        description="Confirm eBay batch item(s) are live, moving them to Listed. Run inside that pallet's category.",
    )
    @app_commands.describe(item_number="A single item's number to confirm (omit to confirm every pending item in this pallet)")
    async def confirm_listed(self, interaction: discord.Interaction, item_number: int = None):
        if not await _require_admin(interaction):
            return

        pallet = db.get_pallet_by_category(interaction.channel.category_id) if interaction.channel.category_id else None
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return

        cog = self.bot.get_cog("ItemFlow")

        if item_number is not None:
            item = db.get_item_by_pallet_and_number(pallet["id"], item_number)
            if not item:
                await interaction.response.send_message(
                    f"No item #{item_number} found in **{pallet['name']}**.", ephemeral=True
                )
                return
            if item["status"] != db.STATUS_PENDING_EBAY_UPLOAD:
                await interaction.response.send_message(
                    f"Item #{item_number} isn't waiting on an eBay batch upload "
                    f"(current status: {item['status']}).",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            moved = await cog.confirm_ebay_pending_item(item, actor_id=interaction.user.id)
            await finance_utils.refresh_finance_message(self.bot, pallet["id"])
            await interaction.followup.send(
                f"✅ Confirmed item #{item_number} live on eBay. Moved to Listed." if moved
                else f"Item #{item_number} couldn't be confirmed (status changed under us - try again).",
                ephemeral=True,
            )
            return

        pending_items = db.get_items_by_status_for_pallet(pallet["id"], db.STATUS_PENDING_EBAY_UPLOAD)
        if not pending_items:
            await interaction.response.send_message(
                f"No items in **{pallet['name']}** are waiting on an eBay batch upload.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmed = 0
        for item in pending_items:
            if await cog.confirm_ebay_pending_item(item, actor_id=interaction.user.id):
                confirmed += 1
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
            f"✅ Confirmed {confirmed} item(s) in **{pallet['name']}** live on eBay. Moved to Listed.",
            ephemeral=True,
        )

    @ebay_group.command(
        name="category-search",
        description="Search eBay's real category list by keyword - IDs for /ebay retry-item's category_id.",
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
        name="retry-item",
        description="Re-queue a failed batch item, fixing whatever caused the failure. Run in its pallet's category.",
    )
    @app_commands.describe(
        item_number="The item's number shown on its card (e.g. 3)",
        category_id="Optional - a corrected eBay leaf category ID (see /ebay category-search)",
        condition_id="Optional - a corrected condition ID (error: 'condition id is invalid for the selected category')",
        specifics="Optional - item specifics to add/fix, as 'Key=Value, Key2=Value2' - merged into what's already saved",
        weight_lb="Optional - a corrected weight in pounds, e.g. 2.5",
        dimensions="Optional - corrected dimensions as 'L x W x H' in inches, e.g. '12 x 8 x 4'",
    )
    async def retry_item(
        self, interaction: discord.Interaction, item_number: int,
        category_id: str = None, condition_id: str = None, specifics: str = None,
        weight_lb: float = None, dimensions: str = None,
    ):
        if not await _require_admin(interaction):
            return

        pallet = db.get_pallet_by_category(interaction.channel.category_id) if interaction.channel.category_id else None
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return

        item = db.get_item_by_pallet_and_number(pallet["id"], item_number)
        if not item:
            await interaction.response.send_message(f"No item #{item_number} found in **{pallet['name']}**.", ephemeral=True)
            return
        if item["status"] != db.STATUS_PENDING_EBAY_UPLOAD:
            await interaction.response.send_message(
                f"Item #{item_number} isn't sitting in a pending-upload state (current status: "
                f"{item['status']}) - there's nothing to retry.",
                ephemeral=True,
            )
            return

        listing = db.get_ebay_listing_data(item["id"])
        if not listing:
            await interaction.response.send_message(
                "No eBay listing data found for this item - can't rebuild its row.", ephemeral=True
            )
            return

        corrections = []
        if category_id:
            listing["category_id"] = category_id.strip()
            corrections.append(f"category → `{listing['category_id']}`")
        if condition_id:
            listing["condition_id"] = condition_id.strip()
            corrections.append(f"condition → `{listing['condition_id']}`")
        if specifics:
            try:
                added = _parse_specifics(specifics)
            except ValueError as e:
                await interaction.response.send_message(f"Couldn't parse `specifics`: {e}", ephemeral=True)
                return
            listing["item_specifics"].update(added)
            corrections.append(f"specifics +{', '.join(added)}")
        if weight_lb is not None:
            if weight_lb <= 0:
                await interaction.response.send_message("`weight_lb` must be greater than 0.", ephemeral=True)
                return
            listing["weight_lb"] = weight_lb
            corrections.append(f"weight → `{weight_lb:g} lb`")
        if dimensions:
            try:
                length_in, width_in, height_in = _parse_dimensions(dimensions)
            except ValueError as e:
                await interaction.response.send_message(f"`dimensions` {e}.", ephemeral=True)
                return
            listing["length_in"], listing["width_in"], listing["height_in"] = length_in, width_in, height_in
            corrections.append(f"dimensions → `{length_in:g}x{width_in:g}x{height_in:g} in`")

        if corrections:
            # Always thread weight/dims through, whether or not THIS retry
            # touched them - save_ebay_listing_data is a full upsert, so
            # omitting them here would silently null out a previously-saved
            # weight/dimensions on every category/condition/specifics-only
            # correction, not just leave them unchanged.
            db.save_ebay_listing_data(
                item["id"], listing["ebay_title"], listing["category_id"], listing["condition_id"],
                listing["price"], listing["item_specifics"], listing_format=listing["listing_format"],
                auction_duration=listing["auction_duration"], weight_lb=listing.get("weight_lb"),
                length_in=listing.get("length_in"), width_in=listing.get("width_in"),
                height_in=listing.get("height_in"), actor_id=interaction.user.id,
            )

        db.clear_ebay_batch_id(item["id"])
        ebay_csv.append_item_to_batch(item, listing)
        correction_note = f" ({'; '.join(corrections)})" if corrections else ""
        await interaction.response.send_message(
            f"🔁 Re-queued item #{item_number} into the current (live) eBay CSV batch{correction_note} - "
            f"it'll go out in the next `/ebay export-batch`.",
            ephemeral=True,
        )

    @ebay_group.command(
        name="requeue-pending",
        description="Bulk /ebay retry-item: re-queue every item pending an eBay upload into a fresh batch.",
    )
    async def requeue_pending(self, interaction: discord.Interaction):
        """
        The bulk version of /ebay retry-item above, for when EVERY currently-
        pending item (not just one) needs a fresh CSV row - e.g. after
        correcting stored data that already-exported items' rows were built
        from (a wrong R2_PUBLIC_URL_BASE baked stale photo URLs into the
        original CSV, which fixing the database alone doesn't retroactively
        fix - that file's already downloaded/possibly already uploaded).
        Doesn't touch category/condition/specifics/weight/dimensions like
        retry-item can - just rebuilds each row from whatever's currently in
        the database, same as a plain re-run of retry-item with no
        corrections would, for every pending item at once instead of one at
        a time.
        """
        if not await _require_admin(interaction):
            return

        pending_items = db.get_all_items_pending_ebay_upload()
        if not pending_items:
            await interaction.response.send_message(
                "No items are currently pending an eBay batch upload.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        requeued_numbers = []
        skipped_numbers = []
        for item in pending_items:
            listing = db.get_ebay_listing_data(item["id"])
            if not listing:
                skipped_numbers.append(item["item_number"])
                continue
            db.clear_ebay_batch_id(item["id"])
            ebay_csv.append_item_to_batch(item, listing)
            requeued_numbers.append(item["item_number"])

        lines = [
            f"🔁 Re-queued {len(requeued_numbers)} item(s) into the current (live) eBay CSV batch - "
            f"they'll go out in the next `/ebay export-batch`."
        ]
        if skipped_numbers:
            shown = ", ".join(f"#{n}" for n in skipped_numbers[:10])
            more = f", and {len(skipped_numbers) - 10} more" if len(skipped_numbers) > 10 else ""
            lines.append(f"⚠️ Skipped {len(skipped_numbers)} item(s) with no eBay listing data on file: {shown}{more}.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

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
