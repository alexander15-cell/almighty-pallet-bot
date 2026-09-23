"""
Admin command for the Pirate Ship CSV export (see pirate_ship_csv.py) -
shipping labels for "Other"-platform sales (FB Marketplace, website, etc.).

eBay sales never need this: Pirate Ship pulls those directly via its own
native eBay integration. This command only covers items sold somewhere
Pirate Ship can't see on its own.

/pirate-ship export-batch - exports every STATUS_SOLD item with a non-eBay
                             sale_platform that hasn't been exported yet, as
                             a Discord attachment, then marks them exported
                             so a re-run doesn't duplicate them. Requires the
                             Pallet Admin role, same as admin_tools.py.
/pirate-ship purge-buyer-data - previews (default) or, with confirm:True,
                             clears recipient name/address from shipped
                             items past config.BUYER_DATA_RETENTION_DAYS,
                             and redacts matching rows in already-exported
                             CSV archives. Inventory identity and financial
                             records are never touched. Requires Pallet
                             Admin; always previews before it changes
                             anything.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import pirate_ship_csv
import runtime_settings


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


class PirateShip(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    pirate_ship_group = app_commands.Group(name="pirate-ship", description="Admin Pirate Ship shipping export")

    @pirate_ship_group.command(
        name="export-batch",
        description="Export pending non-eBay sold items as a Pirate Ship shipping CSV.",
    )
    async def export_batch(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        items = db.get_unexported_other_platform_sales()
        if not items:
            await interaction.response.send_message(
                "No non-eBay sold items are waiting on a Pirate Ship export.", ephemeral=True
            )
            return

        missing_address = [
            item for item in items
            if not item.get("address_line1") and not item.get("shipping_address")
        ]
        path = pirate_ship_csv.export_pending(items)
        db.mark_pirate_ship_exported([item["id"] for item in items])

        warning = ""
        if missing_address:
            numbers = ", ".join(f"#{item['item_number']}" for item in missing_address)
            warning = (
                f"\n⚠️ {len(missing_address)} item(s) have no shipping address on file "
                f"(use `/finance set-shipping-info`): {numbers}. They're still included - "
                "fill in the address before uploading, or they'll need a manual label."
            )

        await interaction.response.send_message(
            f"📦 Pirate Ship export attached ({len(items)} item(s)). Upload it in Pirate Ship's "
            "batch/spreadsheet order import - weight and package dimensions aren't tracked here, "
            f"so fill those in before creating labels.{warning}",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

    @pirate_ship_group.command(
        name="purge-buyer-data",
        description="Preview, or (confirm:True) clear, buyer names/addresses past the retention window.",
    )
    @app_commands.describe(
        days=f"Days after shipment before buyer data is eligible (default {config.BUYER_DATA_RETENTION_DAYS})",
        confirm="Set True to actually clear the data - default is preview-only",
    )
    async def purge_buyer_data(self, interaction: discord.Interaction, days: int = None, confirm: bool = False):
        if not await _require_admin(interaction):
            return

        days = config.BUYER_DATA_RETENTION_DAYS if days is None else days
        if days < 0:
            await interaction.response.send_message("Days can't be negative.", ephemeral=True)
            return

        candidates = db.get_buyer_data_purge_candidates(days)
        if not candidates:
            await interaction.response.send_message(
                f"No shipped items have buyer data eligible for removal (shipped more than {days} day(s) ago).",
                ephemeral=True,
            )
            return

        numbers = ", ".join(f"#{item['item_number']}" for item in candidates[:25])
        if len(candidates) > 25:
            numbers += f", and {len(candidates) - 25} more"

        if not confirm:
            await interaction.response.send_message(
                f"**Preview only** - {len(candidates)} item(s) shipped more than {days} day(s) ago still "
                f"have buyer data on file: {numbers}.\nInventory identity, sale price, and audit history "
                f"are never touched - only recipient name/address. Run again with `confirm:True` to "
                f"actually clear it (also redacts matching rows in already-exported Pirate Ship CSVs).",
                ephemeral=True,
            )
            return

        db.purge_buyer_data([item["id"] for item in candidates], actor_id=interaction.user.id)
        order_numbers = {pirate_ship_csv.order_number(item) for item in candidates}
        redacted_rows = pirate_ship_csv.redact_archived_buyer_data(order_numbers)

        await interaction.response.send_message(
            f"🗑️ Cleared buyer data for {len(candidates)} item(s): {numbers}.\n"
            f"Also redacted {redacted_rows} matching row(s) in already-exported Pirate Ship CSV "
            f"archives. Files/Discord attachments downloaded before this point may still contain "
            f"the old data - those need separate manual removal.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PirateShip(bot))
