"""
Admin commands for the Facebook Marketplace CSV batch (see
fb_marketplace_csv.py) - the path used since there's no Facebook Marketplace
API integration, only its own bulk CSV upload tool.

/fb-marketplace export-batch  - hands the accumulated CSV to whoever manages
                       Facebook Marketplace as a Discord attachment, and
                       archives + clears it so the next "Add to FB
                       Marketplace Batch" click starts fresh. Unlike the
                       eBay batch, there's no numbered-batch/import-results
                       reconciliation here - Facebook doesn't give back a
                       results file the way eBay's Seller Hub does.
/fb-marketplace confirm-listed - the (only) way to move an item out of
                       pending_fb_marketplace_upload: once the uploaded CSV
                       is actually live on Facebook, moves the item(s) that
                       were in it to Listed by hand.

Requires the Pallet Admin role, same as cogs/ebay.py and cogs/pirate_ship.py.
The actual message/status moving is delegated to the ItemFlow cog (via
get_cog, same cross-cog pattern used throughout this bot) so that logic
stays in one place alongside the rest of the pipeline.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import fb_marketplace_csv
import finance_utils
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


class FbMarketplace(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    fb_marketplace_group = app_commands.Group(
        name="fb-marketplace", description="Admin Facebook Marketplace CSV batch management"
    )

    @fb_marketplace_group.command(
        name="export-batch",
        description="Download the accumulated FB Marketplace CSV batch and start a fresh one.",
    )
    async def export_batch(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        pending_items = db.get_items_pending_fb_marketplace_upload()

        path = fb_marketplace_csv.export_and_archive()
        if path is None:
            await interaction.response.send_message(
                "The FB Marketplace batch is empty - nothing to export.", ephemeral=True
            )
            return

        pending_channel_id = db.get_shared_channel_id("pending-fb-marketplace-upload")
        pending_channel_mention = f"<#{pending_channel_id}>" if pending_channel_id else "#pending-fb-marketplace-upload"
        photo_url_note = (
            "Photo URL is filled in from R2-hosted photo URLs where available (one per item)."
            if config.R2_ENABLED else
            "Photo URL is blank (R2 photo hosting isn't configured), so add a photo to each "
            "listing yourself after uploading."
        )
        await interaction.response.send_message(
            f"📘 FB Marketplace batch CSV attached ({len(pending_items)} item(s)). Upload it via "
            f"Facebook's \"Use a spreadsheet for multiple listings\" bulk tool - it predicts each "
            f"listing's category from the title/description, so review those once it's live. "
            f"{photo_url_note} The live batch has been cleared - the next **Add to FB Marketplace "
            f"Batch** click starts a new one. Once it's actually live, run `/fb-marketplace "
            f"confirm-listed` to move these out of {pending_channel_mention}.",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

    @fb_marketplace_group.command(
        name="confirm-listed",
        description="Confirm FB Marketplace item(s) are live, moving to Listed. Run inside that pallet's category.",
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
            if item["status"] != db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD:
                await interaction.response.send_message(
                    f"Item #{item_number} isn't waiting on an FB Marketplace batch upload "
                    f"(current status: {item['status']}).",
                    ephemeral=True,
                )
                return
            moved = await cog.confirm_fb_marketplace_pending_item(item, actor_id=interaction.user.id)
            await finance_utils.refresh_finance_message(self.bot, pallet["id"])
            await interaction.response.send_message(
                f"✅ Confirmed item #{item_number} live on FB Marketplace. Moved to Listed." if moved
                else f"Item #{item_number} couldn't be confirmed (status changed under us - try again).",
                ephemeral=True,
            )
            return

        pending_items = db.get_items_by_status_for_pallet(pallet["id"], db.STATUS_PENDING_FB_MARKETPLACE_UPLOAD)
        if not pending_items:
            await interaction.response.send_message(
                f"No items in **{pallet['name']}** are waiting on an FB Marketplace batch upload.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        confirmed = 0
        for item in pending_items:
            if await cog.confirm_fb_marketplace_pending_item(item, actor_id=interaction.user.id):
                confirmed += 1
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
            f"✅ Confirmed {confirmed} item(s) in **{pallet['name']}** live on FB Marketplace. Moved to Listed.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(FbMarketplace(bot))
