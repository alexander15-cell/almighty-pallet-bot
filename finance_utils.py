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
    except discord.HTTPException as e:
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
    except (discord.NotFound, discord.HTTPException):
        return

    fin = db.get_pallet_financials(pallet_id)
    embed = build_finance_embed(pallet, fin)
    try:
        await msg.edit(embed=embed)
    except discord.HTTPException as e:
        print(f"[finance_utils] Could not refresh status card for pallet {pallet_id}: {e}")
