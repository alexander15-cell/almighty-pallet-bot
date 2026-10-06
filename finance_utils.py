"""
Builds the live financial + pipeline-status embed that gets posted (and
pinned) as the first message in every pallet's #pallet-discussion channel,
and refreshes it in place whenever something relevant changes: cost is set,
an item is received, an item moves stage, a sale price is recorded, or the
received-count override is changed.

This lives outside any single cog because pallet_setup.py (creates the
message), item_flow.py (item counts/stages change it), and finance.py
(cost/price changes it) all need to call the same update logic - putting it
here avoids three cogs importing each other.
"""
import discord

import config
import database as db
import discord_resilience
import quickbooks

# (label, config.py attribute holding the account ID) pairs shown on the
# #finance-dashboard live balance snapshot - see build_dashboard_embed.
# The credit card account is handled separately below since it only makes
# sense to show once QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID is actually set.
# "Cost of Goods Sold" (not "Inventory") since every pallet purchase is
# expensed immediately on a cash basis - there's no Inventory asset this
# bot maintains anymore (see config.py's QUICKBOOKS_COGS_ACCOUNT_ID comment).
DASHBOARD_ACCOUNTS = [
    ("Cash", "QUICKBOOKS_BANK_ACCOUNT_ID"),
    ("Undeposited Funds", "QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID"),
    ("Cost of Goods Sold", "QUICKBOOKS_COGS_ACCOUNT_ID"),
]


def build_finance_embed(pallet: dict, fin: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 {pallet['name']} - Live Status",
        color=discord.Color.gold(),
    )

    if fin["cost"] is not None:
        embed.add_field(name="Pallet Cost", value=f"${fin['cost']:.2f}", inline=True)
    else:
        embed.add_field(name="Pallet Cost", value="Not set - use /finance setprice", inline=True)

    received_label = "Items Received"
    received_value = str(fin["items_received"])
    if fin["items_received_is_override"]:
        received_value += " (manual override)"
    embed.add_field(name=received_label, value=received_value, inline=True)

    if fin["cost_per_item"] is not None:
        embed.add_field(name="Cost / Item", value=f"${fin['cost_per_item']:.2f}", inline=True)
    else:
        embed.add_field(name="Cost / Item", value="—", inline=True)

    embed.add_field(name="Revenue So Far", value=f"${fin['revenue_so_far']:.2f}", inline=True)
    embed.add_field(
        name="Items Priced",
        value=f"{fin['items_priced']} (avg ${fin['avg_sale_price']:.2f})" if fin["items_priced"] else "0",
        inline=True,
    )
    if fin["items_pending_sale"]:
        embed.add_field(
            name="Pending Sale Value",
            value=f"${fin['pending_sale_value']:.2f} ({fin['items_pending_sale']} priced, not yet sold)",
            inline=True,
        )

    # Refunds/expenses only show once at least one has been recorded, so a
    # pallet that never uses /finance refund or /finance expense keeps the
    # simpler card it always had.
    if fin["refunds_total"] or fin["expenses_total"]:
        embed.add_field(name="Refunds", value=f"-${fin['refunds_total']:.2f}", inline=True)
        embed.add_field(name="Expenses", value=f"-${fin['expenses_total']:.2f}", inline=True)
        embed.add_field(name="Net Revenue", value=f"${fin['net_revenue']:.2f}", inline=True)

    if fin["profit_so_far"] is not None:
        pl_word = "Profit" if fin["profit_so_far"] >= 0 else "Loss"
        label = f"{pl_word} vs. Cost" + (" (net)" if (fin["refunds_total"] or fin["expenses_total"]) else "")
        embed.add_field(
            name=label,
            value=f"${abs(fin['profit_so_far']):.2f}",
            inline=True,
        )
    else:
        embed.add_field(name="Profit / Loss", value="Set a cost with /finance setprice to see this", inline=True)

    if fin["cost_recovery_pct"] is not None:
        bar_filled = min(int(fin["cost_recovery_pct"] // 10), 10)
        bar = "🟩" * bar_filled + "⬜" * (10 - bar_filled)
        status_word = "✅ Broke even" if fin["broke_even"] else "still recovering cost"
        embed.add_field(
            name="Cost Recovery",
            value=f"{bar} {fin['cost_recovery_pct']:.1f}% ({status_word})",
            inline=False,
        )

    counts = fin["status_counts"]
    stage_line = "\n".join(
        f"**{stage.replace('_', ' ').title()}:** {counts.get(stage, 0)}"
        for stage in config.STAGE_ORDER_FOR_STATUS
        if counts.get(stage, 0) > 0 or stage not in ("rejected", "deleted")
    )
    embed.add_field(name="Items by Stage", value=stage_line or "No items yet", inline=False)

    embed.set_footer(text="Updates automatically as items move and prices are recorded.")
    return embed


async def post_initial_finance_message(bot: discord.Client, pallet_id: int, discussion_channel: discord.TextChannel):
    """Called once, right after a pallet's channels are created. Posts and
    pins the card, and stores its message_id for future edits."""
    pallet = db.get_pallet(pallet_id)
    fin = db.get_pallet_financials(pallet_id)
    embed = build_finance_embed(pallet, fin)
    msg = await discussion_channel.send(embed=embed)
    try:
        await msg.pin(reason="Live pallet status card")
    except discord_resilience.TRANSIENT_DISCORD_ERRORS as e:
        print(f"[finance_utils] Could not pin status card for pallet {pallet_id}: {e}")
    db.set_finance_message(pallet_id, msg.id)


async def refresh_finance_message(bot: discord.Client, pallet_id: int):
    """
    Call this after ANY change that affects the numbers shown on the card:
    cost set, item created, item status changed, sale price recorded,
    received-count override changed. Silently does nothing if the pallet has
    no discussion channel or finance message yet (e.g. mid-creation), or if
    the message/channel has since been deleted (e.g. an archived pallet) -
    a missing card is not worth crashing whatever action triggered this.
    """
    if config.PRESERVE_DISCORD_HISTORY:
        # Incomplete recovered totals must not replace historical finance cards.
        return
    pallet = db.get_pallet(pallet_id)
    if not pallet or not pallet.get("finance_message_id"):
        return

    discussion_channel_id = db.get_stage_channel_id(pallet_id, "discussion")
    if not discussion_channel_id:
        return

    channel = bot.get_channel(discussion_channel_id)
    if not channel:
        return

    try:
        msg = await channel.fetch_message(pallet["finance_message_id"])
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        return

    fin = db.get_pallet_financials(pallet_id)
    embed = build_finance_embed(pallet, fin)
    try:
        await msg.edit(embed=embed)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS as e:
        print(f"[finance_utils] Could not refresh status card for pallet {pallet_id}: {e}")
        return

    await _post_pallet_warnings(channel, pallet, fin)
    await refresh_dashboard_message(bot)


async def build_dashboard_embed() -> discord.Embed:
    """
    The business-wide "position" snapshot kept in #finance-dashboard - a
    handful of live QuickBooks account balances (Cash, Undeposited Funds,
    Cost of Goods Sold, and the configured credit card if any) plus the
    same month-to-date revenue/spend and pallet-progress figures /finance
    overview already computes. Refreshed automatically by
    refresh_dashboard_message every time refresh_finance_message runs for
    any pallet - i.e. after any cost/sale/expense change anywhere, not
    just here.
    """
    embed = discord.Embed(title="📈 Business Position", color=discord.Color.blurple())

    if quickbooks.is_connected():
        for label, config_attr in DASHBOARD_ACCOUNTS:
            account_id = getattr(config, config_attr, None)
            if not account_id:
                continue
            try:
                balance = await quickbooks.get_account_balance(account_id)
                embed.add_field(name=label, value=f"${balance['balance']:.2f}", inline=True)
            except quickbooks.QuickBooksError:
                embed.add_field(name=label, value="⚠️ Couldn't fetch", inline=True)
        if config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID:
            try:
                balance = await quickbooks.get_account_balance(config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID)
                embed.add_field(name="Credit Card Balance", value=f"${balance['balance']:.2f}", inline=True)
            except quickbooks.QuickBooksError:
                embed.add_field(name="Credit Card Balance", value="⚠️ Couldn't fetch", inline=True)
    else:
        embed.add_field(
            name="QuickBooks",
            value="Not connected - run `/finance connect-quickbooks`" if quickbooks.is_configured() else "Not configured",
            inline=False,
        )

    mtd = db.get_month_to_date_financials()
    embed.add_field(name="Month-to-Date Revenue", value=f"${mtd['revenue']:.2f}", inline=True)
    embed.add_field(name="Month-to-Date Spend", value=f"${mtd['spend']:.2f}", inline=True)

    progress = db.get_pallet_progress_counts()
    embed.add_field(
        name="Pallets",
        value=f"{progress['in_progress']} in progress, {progress['sold_out']} fully sold out",
        inline=True,
    )

    embed.set_footer(text="Updates automatically whenever a sale, expense, or cost changes.")
    return embed


async def post_initial_dashboard_message(bot: discord.Client, channel: discord.TextChannel):
    """Called once, right after #finance-dashboard is created. Posts and
    pins the first snapshot, and stores its message_id for future edits."""
    embed = await build_dashboard_embed()
    msg = await channel.send(embed=embed)
    try:
        await msg.pin(reason="Live business dashboard")
    except discord_resilience.TRANSIENT_DISCORD_ERRORS as e:
        print(f"[finance_utils] Could not pin dashboard message: {e}")
    db.set_dashboard_message_id(msg.id)


async def refresh_dashboard_message(bot: discord.Client):
    """
    Best-effort, same shape as refresh_finance_message - silently does
    nothing if #finance-dashboard hasn't been set up yet (run
    /setup shared-channels) or the message/channel has since been deleted,
    since a missing dashboard is never worth crashing whatever action
    triggered this refresh.
    """
    if config.PRESERVE_DISCORD_HISTORY:
        return
    message_id = db.get_dashboard_message_id()
    if not message_id:
        return
    channel_id = db.get_shared_channel_id("finance-dashboard")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    try:
        msg = await channel.fetch_message(message_id)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        return
    embed = await build_dashboard_embed()
    try:
        await msg.edit(embed=embed)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS as e:
        print(f"[finance_utils] Could not refresh dashboard message: {e}")


async def post_to_finance_audit_log(bot: discord.Client, text: str):
    """
    Best-effort, permanent (non-ephemeral) record in #finance-audit-log of
    a completed QuickBooks transaction - a logged sale or an allocated
    credit-card charge. Never raises: a missing/misconfigured channel must
    never block the action that triggered this, since the transaction
    itself already went through by the time this is called.
    """
    channel_id = db.get_shared_channel_id("finance-audit-log")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    try:
        await channel.send(text)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS as e:
        print(f"[finance_utils] Could not post to finance-audit-log: {e}")


async def _post_pallet_warnings(channel: discord.TextChannel, pallet: dict, fin: dict):
    """
    Simple early-warning checks (see config.QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT)
    run every time the live card refreshes - i.e. reactively after any
    cost/sale mutation, since every one of those call sites already calls
    refresh_finance_message(). Not a full analytics system, just two flags
    worth surfacing without someone having to go look:
      - running cost basis has crept past the pallet's manifest retail
        value estimate (sum of every item's AI-suggested price - a rough
        guess, but the only "what should this pallet be worth" figure the
        bot has without a human entering one by hand)
      - once every item is sold/shipped, the realized margin came in below
        the configured threshold
    Posted as a plain (non-pinned) message in the pallet's own discussion
    channel, right after its live status card. Not deduplicated across
    calls - the mutations that trigger this happen rarely enough that a
    fresh nudge each time is useful, not spam.
    """
    warnings = []

    manifest_estimate = db.get_pallet_manifest_value_estimate(pallet["id"])
    if fin["cost"] is not None and manifest_estimate > 0 and fin["cost"] > manifest_estimate:
        warnings.append(
            f"⚠️ **Cost basis (${fin['cost']:.2f}) has exceeded this pallet's manifest retail "
            f"value estimate (${manifest_estimate:.2f})** - based on summed AI-suggested prices, "
            f"so treat this as a rough heads-up, not a precise number."
        )

    status_counts = fin["status_counts"]
    real_counts = {status: n for status, n in status_counts.items() if status != db.STATUS_DELETED and n}
    fully_sold_out = bool(real_counts) and all(status in (db.STATUS_SOLD, db.STATUS_SHIPPED) for status in real_counts)
    if fully_sold_out and fin["cost"] is not None and fin["revenue_so_far"] > 0 and fin["profit_so_far"] is not None:
        margin_pct = (fin["profit_so_far"] / fin["revenue_so_far"]) * 100
        if margin_pct < config.QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT:
            warnings.append(
                f"⚠️ **{pallet['name']} is fully sold out with a {margin_pct:.1f}% margin** - "
                f"below the {config.QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT:.0f}% early-warning threshold."
            )

    if not warnings:
        return
    try:
        await channel.send("\n".join(warnings))
    except discord_resilience.TRANSIENT_DISCORD_ERRORS as e:
        print(f"[finance_utils] Could not post warning(s) for pallet {pallet['id']}: {e}")
