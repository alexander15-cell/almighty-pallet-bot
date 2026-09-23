"""
Admin-only management commands: deleting a single item, archiving a finished
pallet (removes its Discord channels but keeps all data), listing pallets,
a full database wipe, and local backups (see backup.py).

Every command here requires the "Pallet Admin" role. /db-wipe additionally
requires the caller to hold real Discord Administrator permission on the
server, as a second, harder-to-grant safety layer on top of the bot's own
role system, since wiping the database is irreversible.

/backup-now and /backups only ever create or list backups - restoring one
is deliberately CLI-only (`python backup.py restore ...`, run with the bot
stopped), not a Discord command, since it's the one operation here that can
put stale data back in place of current data.

/bind-role, /unbind-role, /role-bindings - optional role-ID bindings (see
runtime_settings.py). Every permission check normally matches a role by
NAME (e.g. "Pallet Admin"), which breaks if that role gets renamed in
Discord; binding it to its actual role ID here makes that check survive a
rename. Purely optional and editable at runtime - an unbound role just
keeps matching by name like today.
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import backup
import config
import database as db
import finance_utils
import runtime_settings

log = logging.getLogger(__name__)

_BINDABLE_ROLES = [
    config.ROLE_DATA_ENTRY, config.ROLE_QUEUE_REVIEW, config.ROLE_LISTING_MGMT,
    config.ROLE_PURCHASE_MGMT, config.ROLE_FINANCE_MGMT, config.ROLE_ADMIN,
]
_ROLE_NAME_CHOICES = [app_commands.Choice(name=name, value=name) for name in _BINDABLE_ROLES]


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
        self.backup_loop.start()

    def cog_unload(self):
        self.backup_loop.cancel()

    item_group = app_commands.Group(name="item", description="Admin item management")
    pallet_group = app_commands.Group(name="pallet", description="Admin pallet management")

    @tasks.loop(hours=config.BACKUP_INTERVAL_HOURS)
    async def backup_loop(self):
        """Scheduled local backup (see backup.py) - runs every
        BACKUP_INTERVAL_HOURS while the bot is online. A failure here is
        logged, not raised, so one bad backup attempt doesn't crash the
        whole bot or stop future scheduled attempts."""
        try:
            path = await asyncio.to_thread(backup.create_backup)
            log.info(f"Scheduled backup created: {path}")
        except Exception:
            log.exception("Scheduled backup failed")

    @backup_loop.before_loop
    async def _before_backup_loop(self):
        await self.bot.wait_until_ready()

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

    @app_commands.command(name="backup-now", description="Create a verified local backup of the database, photos, and CSV archives right now.")
    async def backup_now(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            path = await asyncio.to_thread(backup.create_backup)
        except Exception as e:
            log.exception("Manual backup failed")
            await interaction.followup.send(f"Backup failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Backup created and verified: `{path.name}`", ephemeral=True)

    @app_commands.command(name="backups", description="List recent local backups.")
    async def backups(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        backup_dir = Path(config.BACKUP_DIR)
        files = sorted(backup_dir.glob("backup_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if backup_dir.exists() else []
        if not files:
            await interaction.response.send_message(
                "No local backups yet - one is created automatically every "
                f"{config.BACKUP_INTERVAL_HOURS}h, or run `/admin backup-now`.",
                ephemeral=True,
            )
            return
        lines = []
        for path in files[:15]:
            size_mb = path.stat().st_size / (1024 * 1024)
            age = datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            lines.append(f"`{path.name}` - {size_mb:.1f} MB - {age.days}d {age.seconds // 3600}h ago")
        embed = discord.Embed(
            title="Local Backups",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Showing {min(len(files), 15)} of {len(files)}. Kept up to {config.BACKUP_KEEP_COUNT} snapshots / {config.BACKUP_MAX_AGE_DAYS} days.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bind-role", description="Bind one of this bot's roles to a specific Discord role, so a future rename doesn't break it.")
    @app_commands.describe(role_name="Which of this bot's roles to bind", role="The actual Discord role to bind it to")
    @app_commands.choices(role_name=_ROLE_NAME_CHOICES)
    async def bind_role(self, interaction: discord.Interaction, role_name: app_commands.Choice[str], role: discord.Role):
        if not await _require_admin(interaction):
            return
        runtime_settings.set_role_id(role_name.value, role.id)
        await interaction.response.send_message(
            f"🔗 Bound **{role_name.value}** to {role.mention} (id `{role.id}`). Renaming that role in Discord "
            f"won't break this bot's permission checks anymore. Use `/admin unbind-role` to revert to "
            f"matching by name.",
            ephemeral=True,
        )

    @app_commands.command(name="unbind-role", description="Remove a role-ID binding, reverting to matching that role by name.")
    @app_commands.describe(role_name="Which of this bot's roles to unbind")
    @app_commands.choices(role_name=_ROLE_NAME_CHOICES)
    async def unbind_role(self, interaction: discord.Interaction, role_name: app_commands.Choice[str]):
        if not await _require_admin(interaction):
            return
        runtime_settings.clear_role_id(role_name.value)
        await interaction.response.send_message(
            f"Unbound **{role_name.value}** - it'll match by name (a role literally called "
            f"\"{role_name.value}\") again.",
            ephemeral=True,
        )

    @app_commands.command(name="role-bindings", description="List which of this bot's roles are bound to a specific Discord role ID.")
    async def role_bindings(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return
        bindings = runtime_settings.get_bindings()
        lines = []
        for role_name in _BINDABLE_ROLES:
            role_id = bindings.get(role_name)
            if role_id:
                role = interaction.guild.get_role(role_id)
                status = role.mention if role else f"id `{role_id}` (role no longer exists - falling back to name match)"
                lines.append(f"**{role_name}** → {status}")
            else:
                lines.append(f"**{role_name}** → matching by name (not bound)")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminTools(bot))
