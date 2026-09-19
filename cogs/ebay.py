"""
Admin commands for the eBay CSV batch fallback (see ebay_csv.py) - the path
used while there's no live eBay API integration:

/ebay export-batch  - hands the accumulated CSV to whoever manages eBay as a
                       Discord attachment, then archives + clears it so the
                       next "Add to eBay Batch" click starts fresh.
/ebay confirm-listed - once that CSV has actually been uploaded and
                       processed by eBay, moves the item(s) that were in it
                       from pending_ebay_upload to Listed. There's no live
                       API to detect this automatically, so an admin confirms
                       it by hand after checking Seller Hub.

Both require the Pallet Admin role, same as admin_tools.py. The actual
message/status moving for confirm-listed is delegated to the ItemFlow cog
(via get_cog, same cross-cog pattern QueueReviewView/AwaitingListingView use)
so that logic stays in one place alongside the rest of the pipeline.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import ebay_csv
import finance_utils


def _is_pallet_admin(interaction: discord.Interaction) -> bool:
    admin_role = discord.utils.get(interaction.guild.roles, name=config.ROLE_ADMIN)
    return bool(admin_role and admin_role in interaction.user.roles)


async def _require_admin(interaction: discord.Interaction) -> bool:
    if _is_pallet_admin(interaction):
        return True
    await interaction.response.send_message(
        f"You need the **{config.ROLE_ADMIN}** role to do that.", ephemeral=True
    )
    return False


class Ebay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ebay_group = app_commands.Group(name="ebay", description="Admin eBay CSV batch management")

    @ebay_group.command(name="export-batch", description="Download the accumulated eBay CSV batch and start a fresh one.")
    async def export_batch(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        path = ebay_csv.export_and_archive()
        if path is None:
            await interaction.response.send_message("The eBay batch is empty - nothing to export.", ephemeral=True)
            return

        pending_channel_id = db.get_shared_channel_id("pending-ebay-upload")
        pending_channel_mention = f"<#{pending_channel_id}>" if pending_channel_id else "#pending-ebay-upload"
        await interaction.response.send_message(
            "📄 eBay batch CSV attached. Upload it in Seller Hub's bulk upload / File Exchange "
            "tool - PicURL is blank since photos are only saved locally, so add photos in Seller "
            "Hub or fill PicURL in yourself before uploading. The batch has been cleared - the "
            "next **Add to eBay Batch** click starts a new one. Once eBay actually shows these "
            f"listings live, run `/ebay confirm-listed` to move them out of {pending_channel_mention}.",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

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
            moved = await cog.confirm_ebay_pending_item(item, actor_id=interaction.user.id)
            await finance_utils.refresh_finance_message(self.bot, pallet["id"])
            await interaction.response.send_message(
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Ebay(bot))
