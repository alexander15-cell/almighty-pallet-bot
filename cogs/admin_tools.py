"""
Admin-only management commands: deleting a single item, archiving a finished
pallet (removes its Discord channels but keeps all data), listing pallets,
and a full database wipe.

Every command here requires the "Pallet Admin" role. /db-wipe additionally
requires the caller to hold real Discord Administrator permission on the
server, as a second, harder-to-grant safety layer on top of the bot's own
role system, since wiping the database is irreversible.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
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


class ConfirmArchiveView(discord.ui.View):
    """One-click confirmation before deleting real Discord channels."""

    def __init__(self, pallet_id: int, pallet_name: str):
        super().__init__(timeout=60)
        self.pallet_id = pallet_id
        self.pallet_name = pallet_name
        self.confirmed = False

    @discord.ui.button(label="Yes, archive it", style=discord.ButtonStyle.red, emoji="🗄️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.grey)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.send_message("Cancelled - nothing was archived.", ephemeral=True)


class ConfirmWipeModal(discord.ui.Modal, title="⚠️ Confirm Full Database Wipe"):
    confirmation = discord.ui.TextInput(
        label=f'Type exactly: {config.DB_WIPE_CONFIRMATION_PHRASE}',
        placeholder=config.DB_WIPE_CONFIRMATION_PHRASE,
        max_length=40,
    )

    def __init__(self, cog: "AdminTools"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirmation.value.strip() != config.DB_WIPE_CONFIRMATION_PHRASE:
            await interaction.response.send_message(
                "Text didn't match exactly. Nothing was deleted. Run `/db-wipe` again if you're sure.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        category_ids = db.get_all_category_ids()
        deleted_categories = 0
        for cat_id in category_ids:
            category = interaction.guild.get_channel(cat_id)
            if category is None:
                continue
            try:
                for channel in list(category.channels):
                    await channel.delete(reason="Database wipe")
                await category.delete(reason="Database wipe")
                deleted_categories += 1
            except discord.HTTPException as e:
                print(f"[admin_tools] Failed to delete category {cat_id} during wipe: {e}")

        # The shared pipeline channels are reusable infrastructure and are
        # NOT deleted - but any item cards sitting in them belong to pallets
        # that are about to stop existing, so purge messages there too for a
        # genuine "start from nothing" reset. purge() only reliably bulk-
        # deletes messages under 14 days old; anything older is skipped
        # rather than erroring, since Discord's API doesn't allow bulk-
        # deleting old messages.
        purged_channels = 0
        for stage, channel_id in db.get_all_shared_channels().items():
            channel = interaction.guild.get_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.purge(limit=1000, bulk=True)
                purged_channels += 1
            except discord.HTTPException as e:
                print(f"[admin_tools] Failed to purge shared channel {stage}: {e}")

        db.wipe_database()

        try:
            await interaction.followup.send(
                f"☠️ Database wiped. Removed {deleted_categories} pallet categor"
                f"{'y' if deleted_categories == 1 else 'ies'} and cleared {purged_channels} "
                f"shared pipeline channel(s) (messages older than 14 days couldn't be bulk-"
                f"deleted - remove those manually if needed). "
                f"Starting from nothing - use **Start New Pallet** in your hub channel to begin again.",
                ephemeral=True,
            )
        except discord.HTTPException:
            # If this command was run from inside a pallet channel, that
            # channel (and the webhook this followup needs) may have just
            # been deleted as part of the wipe - nothing more to do here.
            pass


class AdminTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    item_group = app_commands.Group(name="item", description="Admin item management")
    pallet_group = app_commands.Group(name="pallet", description="Admin pallet management")

    @item_group.command(name="delete", description="Delete a single item by its number. Run inside that pallet's category.")
    @app_commands.describe(item_number="The item's number shown on its card (e.g. 3)")
    async def item_delete(self, interaction: discord.Interaction, item_number: int):
        if not await _require_admin(interaction):
            return

        pallet = db.get_pallet_by_category(interaction.channel.category_id)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.",
                ephemeral=True,
            )
            return

        item = db.get_item_by_pallet_and_number(pallet["id"], item_number)
        if not item:
            await interaction.response.send_message(
                f"No item #{item_number} found in **{pallet['name']}**.", ephemeral=True
            )
            return
        if item["status"] == db.STATUS_DELETED:
            await interaction.response.send_message(f"Item #{item_number} was already deleted.", ephemeral=True)
            return

        # Best-effort: find and delete whatever message currently represents
        # this item, wherever it currently lives. Most stages are shared
        # channels now (one channel serves every pallet), so resolve_channel_id
        # is used instead of a per-pallet lookup.
        deleted_message = False
        if item["current_message_id"]:
            for stage in config.STAGE_CHANNELS:
                channel_id = db.resolve_channel_id(pallet["id"], stage)
                channel = self.bot.get_channel(channel_id) if channel_id else None
                if not channel:
                    continue
                try:
                    msg = await channel.fetch_message(item["current_message_id"])
                    await msg.delete()
                    deleted_message = True
                    break
                except discord.NotFound:
                    continue
                except discord.HTTPException:
                    continue

        db.soft_delete_item(item["id"], actor_id=interaction.user.id)
        await interaction.response.send_message(
            f"🗑️ Deleted item #{item_number} from **{pallet['name']}**"
            + ("." if deleted_message else " (its card was already gone from Discord, but it's now removed from tracking)."),
            ephemeral=True,
        )
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])

    @pallet_group.command(name="archive", description="Close out a finished pallet: deletes its Discord channels, keeps all data.")
    async def pallet_archive(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        pallet = db.get_pallet_by_category(interaction.channel.category_id)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.",
                ephemeral=True,
            )
            return
        if pallet["archived"]:
            await interaction.response.send_message(f"**{pallet['name']}** is already archived.", ephemeral=True)
            return

        summary = db.get_pallet_summary(pallet["id"])
        summary_line = ", ".join(f"{v} {k}" for k, v in summary.items()) or "no items"
        open_statuses = (db.STATUS_DATA_ENTRY, db.STATUS_AUTOMATED_REVIEW, db.STATUS_QUEUE_REVIEW,
                          db.STATUS_AWAITING_LISTING, db.STATUS_LISTED)
        open_count = sum(v for k, v in summary.items() if k in open_statuses)
        open_warning = (
            f"\n⚠️ **{open_count} item(s) are still in progress** and live in the *shared* pipeline "
            f"channels - archiving only removes this pallet's own channels (#pallet-discussion, "
            f"#data-entry). Their cards will remain visible in the shared channels with no home "
            f"category, still taggable by pallet name in the embed."
            if open_count else ""
        )

        view = ConfirmArchiveView(pallet["id"], pallet["name"])
        await interaction.response.send_message(
            f"⚠️ This deletes **all Discord channels** for **{pallet['name']}** "
            f"({summary_line}). All item data stays in the database permanently - "
            f"this only removes the Discord side.{open_warning} Are you sure?",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            return

        category = interaction.guild.get_channel(interaction.channel.category_id)
        if category:
            try:
                for channel in list(category.channels):
                    await channel.delete(reason=f"Pallet {pallet['name']} archived")
                await category.delete(reason=f"Pallet {pallet['name']} archived")
            except discord.HTTPException as e:
                await interaction.followup.send(f"Partially failed to delete channels: {e}", ephemeral=True)

        db.archive_pallet(pallet["id"])
        # interaction.channel no longer exists at this point, so this can only
        # be seen via Discord's toast/notification, not a normal message.
        try:
            await interaction.followup.send(f"✅ **{pallet['name']}** archived.", ephemeral=True)
        except discord.HTTPException:
            pass

    @pallet_group.command(name="list", description="List all pallets and their item counts (admin only).")
    @app_commands.describe(include_archived="Include archived pallets too (default: yes)")
    async def pallet_list(self, interaction: discord.Interaction, include_archived: bool = True):
        if not await _require_admin(interaction):
            return

        pallets = db.get_all_pallets(include_archived=include_archived)
        if not pallets:
            await interaction.response.send_message("No pallets yet.", ephemeral=True)
            return

        embed = discord.Embed(title="📦 All Pallets", color=discord.Color.blurple())
        for p in pallets[:25]:  # embed field limit
            summary = db.get_pallet_summary(p["id"])
            counts_line = ", ".join(f"{v} {k}" for k, v in summary.items()) or "no items"
            status_flag = "🗄️ archived" if p["archived"] else "🟢 active"
            embed.add_field(
                name=f"{p['name']} ({status_flag})",
                value=counts_line,
                inline=False,
            )
        if len(pallets) > 25:
            embed.set_footer(text=f"Showing 25 of {len(pallets)} pallets.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="db-wipe", description="⚠️ DANGER: permanently erase ALL pallet/item data and delete every pallet channel.")
    @app_commands.checks.has_permissions(administrator=True)
    async def db_wipe(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        # app_commands.checks.has_permissions already gated on Discord's own
        # Administrator permission before this handler even runs - this is
        # deliberately a second, independent lock (the bot's own Pallet Admin
        # role) on top of Discord's built-in one.
        await interaction.response.send_modal(ConfirmWipeModal(self))

    @db_wipe.error
    async def db_wipe_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "This requires real Discord **Administrator** permission on this server, "
                "not just the Pallet Admin role - that's intentional, since this command "
                "is irreversible.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminTools(bot))
