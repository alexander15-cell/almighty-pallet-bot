"""
Handles two things:

1. /setup shared-channels (admin, run ONCE ever): creates the shared pipeline
   category and its 6 channels (automated-review, queue-review, etc.), used
   by every pallet. Each channel gets its topic set and an explanatory
   message pinned, so anyone new can open a channel and understand it
   without asking.

2. The "New Pallet Tracking" hub button: creates a new category per pallet
   containing only #pallet-discussion and #data-entry - the two channels
   that genuinely benefit from being pallet-specific. Everything else routes
   into the shared channels from step 1. This is what keeps total channel
   count from scaling with the number of pallets.

No cost/price fields live in the pallet-creation modal - Purchase Management
sets cost afterward with /finance setprice (see cogs/finance.py).
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import discord_resilience
import finance_utils
import information_content
import quickbooks

log = logging.getLogger(__name__)


def role_overwrites(guild: discord.Guild, allowed_role_names: list[str]) -> dict:
    """
    Build a permission overwrite dict: @everyone denied, admin role allowed,
    and whichever roles are specified for this particular channel allowed.
    """
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }

    admin_role = discord.utils.get(guild.roles, name=config.ROLE_ADMIN)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_messages=True
        )

    for role_name in allowed_role_names:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        else:
            print(f"[pallet_setup] WARNING: role '{role_name}' not found in guild - "
                  f"create it in Discord or it won't get channel access.")

    return overwrites


class NewPalletView(discord.ui.View):
    """Persistent view (survives bot restarts) holding the 'Start New Pallet' button."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Start New Pallet",
        style=discord.ButtonStyle.green,
        emoji="📦",
        custom_id="pallet_bot:start_new_pallet",  # fixed custom_id required for persistence
    )
    async def start_new_pallet(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not db.is_shared_channels_setup():
            await interaction.response.send_message(
                "The shared pipeline channels haven't been set up yet. Ask a Pallet Admin "
                "to run `/setup shared-channels` once, first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(NewPalletModal())


async def _claim_awaiting_charges(interaction: discord.Interaction, pallet_id: int, pallet_name: str, charge_ids: list):
    """
    Moves each selected awaiting_pallet_charges row onto the just-created
    pallet (into pallet_costs) and pushes a matching QuickBooks expense
    tagged with the pallet's name/id. Pulled out of
    AwaitingChargesSelect.callback so it can be exercised directly in
    tests without fighting discord.py's Select/Interaction internals - see
    cogs/finance.py's _handle_allocation_choice for the same pattern.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not charge_ids:
        await interaction.followup.send("No charges attached.", ephemeral=True)
        return

    awaiting_channel_id = db.get_shared_channel_id("awaiting-pallet-charges")
    awaiting_channel = interaction.client.get_channel(awaiting_channel_id) if awaiting_channel_id else None

    claimed_total = 0.0
    for value in charge_ids:
        charge = db.claim_awaiting_pallet_charge(int(value), pallet_id, interaction.user.id)
        if not charge or charge["claimed"]:
            continue  # already claimed by someone else in the meantime
        claimed_total += charge["amount"]

        try:
            await quickbooks.create_expense(
                config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID, charge["amount"], charge["txn_date"],
                memo=f"{pallet_name} (Pallet #{pallet_id}) - {charge['merchant']}",
            )
        except quickbooks.QuickBooksError:
            log.exception(
                "Claimed awaiting charge %s locally but failed to push the matching QuickBooks expense",
                charge["quickbooks_txn_id"],
            )

        if awaiting_channel and charge.get("message_id"):
            try:
                msg = await awaiting_channel.fetch_message(charge["message_id"])
                await msg.delete()
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                pass

    await finance_utils.refresh_finance_message(interaction.client, pallet_id)
    await interaction.followup.send(
        f"🧾 Attached {len(charge_ids)} charge(s) totaling ${claimed_total:.2f} to **{pallet_name}**.",
        ephemeral=True,
    )


class AwaitingChargesSelect(discord.ui.Select):
    """
    Shown right after a new pallet is created, only if there are any
    QuickBooks credit-card charges sitting in #awaiting-pallet-charges
    (allocated to "New Pallet (not arrived yet)" before this one existed).
    Multi-select since a pallet often has more than one charge to claim at
    once - e.g. the purchase price plus a separate deposit/fee charge.
    """

    def __init__(self, pallet_id: int, pallet_name: str, charges: list):
        options = [
            discord.SelectOption(
                label=f"{c['merchant'] or 'Unknown merchant'} - ${c['amount']:.2f}"[:100],
                description=(c["txn_date"] or "")[:100],
                value=str(c["id"]),
            )
            for c in charges
        ]
        super().__init__(
            placeholder="Attach any of these charges to this pallet (optional)...",
            options=options, min_values=0, max_values=len(options),
        )
        self.pallet_id = pallet_id
        self.pallet_name = pallet_name

    async def callback(self, interaction: discord.Interaction):
        await _claim_awaiting_charges(interaction, self.pallet_id, self.pallet_name, self.values)


class AwaitingChargesView(discord.ui.View):
    def __init__(self, pallet_id: int, pallet_name: str, charges: list):
        super().__init__(timeout=600)
        self.add_item(AwaitingChargesSelect(pallet_id, pallet_name, charges))


class NewPalletModal(discord.ui.Modal, title="New Pallet"):
    pallet_name = discord.ui.TextInput(
        label="Pallet name / ID",
        placeholder="e.g. Pallet-2026-014",
        max_length=80,
    )
    notes = discord.ui.TextInput(
        label="Notes (optional)",
        placeholder="e.g. source/supplier, anything worth remembering",
        required=False,
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        name = self.pallet_name.value.strip()

        # Discord category names have a 100 char limit and are case-insensitive
        # in the UI but not enforced unique by Discord - we enforce uniqueness ourselves.
        existing = discord.utils.get(guild.categories, name=name)
        if existing:
            await interaction.followup.send(
                f"A category named `{name}` already exists. Pick a unique pallet name.",
                ephemeral=True,
            )
            return

        category = await guild.create_category(name=name)

        pallet_id = db.create_pallet(
            name=name, category_id=category.id, created_by=interaction.user.id,
            notes=self.notes.value.strip() or None,
        )

        # Discussion channel first, so it lands at the top of the category.
        discussion_overwrites = role_overwrites(guild, config.DISCUSSION_CHANNEL_ROLES)
        discussion_channel = await guild.create_text_channel(
            name=config.DISCUSSION_CHANNEL_NAME, category=category, overwrites=discussion_overwrites,
            topic=config.CHANNEL_INFO.get(config.DISCUSSION_CHANNEL_NAME, ""),
        )
        db.map_channel(pallet_id, "discussion", discussion_channel.id)
        await discussion_channel.send(f"ℹ️ {config.CHANNEL_INFO.get(config.DISCUSSION_CHANNEL_NAME, '')}")

        # Data entry - the one pipeline stage that stays per-pallet, since
        # people physically unboxing one pallet at a time benefit from a
        # dedicated space rather than a shared firehose channel.
        de_overwrites = role_overwrites(guild, config.CHANNEL_ROLE_PERMISSIONS.get("data-entry", []))
        data_entry_channel = await guild.create_text_channel(
            name="data-entry", category=category, overwrites=de_overwrites,
            topic=config.CHANNEL_INFO.get("data-entry", ""),
        )
        db.map_channel(pallet_id, "data-entry", data_entry_channel.id)
        await data_entry_channel.send(f"ℹ️ {config.CHANNEL_INFO.get('data-entry', '')}")

        # Post and pin the live finance/status card as the FIRST real message
        # in the discussion channel (after the info blurb above it - Discord
        # pins float above chat regardless of send order, so this is fine).
        await finance_utils.post_initial_finance_message(interaction.client, pallet_id, discussion_channel)

        await interaction.followup.send(
            f"✅ Created pallet **{name}**. Data entry can start in <#{data_entry_channel.id}>. "
            f"Everything from Automated Review onward happens in the shared pipeline channels.",
            ephemeral=True,
        )

        # Any QuickBooks credit-card charges filed under "New Pallet (not
        # arrived yet)" before this pallet existed (see cogs/finance.py's
        # Allocate button) - offer to attach them now that a real pallet id
        # exists. Silently skipped if there are none, so this is a no-op
        # for anyone not using the QuickBooks integration.
        unclaimed = db.get_unclaimed_pallet_charges()
        if unclaimed:
            view = AwaitingChargesView(pallet_id, name, unclaimed[:25])
            await interaction.followup.send(
                f"📥 There {'is' if len(unclaimed) == 1 else 'are'} {len(unclaimed)} unclaimed QuickBooks "
                f"charge(s) waiting in #awaiting-pallet-charges - attach any that belong to **{name}**:",
                view=view, ephemeral=True,
            )


class PalletSetup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Register the persistent view so the button keeps working across restarts
        self.bot.add_view(NewPalletView())

    setup_group = app_commands.Group(name="setup", description="One-time server setup commands")

    @setup_group.command(
        name="hub",
        description="Post the 'Start New Pallet' button in this channel (run once, in #new-pallet-tracking).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_hub(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📦 New Pallet Tracking",
            description=(
                "Click the button below whenever a new pallet arrives.\n\n"
                "This creates a category with **#pallet-discussion** and **#data-entry** "
                "for this pallet. Everything past Data Entry (Automated Review, Queue "
                "Review, Awaiting Listing, Listed, Sold, 10-Day Alerts) happens in the "
                "shared pipeline channels used by every pallet - check each item's "
                "embed to see which pallet it belongs to."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed, view=NewPalletView())
        await interaction.response.send_message("Hub message posted.", ephemeral=True)

    @setup_group.command(
        name="shared-channels",
        description="One-time setup: creates the shared pipeline channels used by every pallet.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_shared_channels(self, interaction: discord.Interaction):
        # FINANCE_SHARED_CHANNELS (QuickBooks credit-card allocation) is
        # checked and created alongside SHARED_STAGE_CHANNELS here since
        # both are stored the same way (db.set_shared_channel), but kept a
        # separate config list - re-running this command on an install that
        # already has the pipeline channels but not the finance ones (added
        # later) fills in just what's missing rather than doing nothing.
        all_stages = list(config.SHARED_STAGE_CHANNELS) + list(config.FINANCE_SHARED_CHANNELS)
        existing_channels = db.get_all_shared_channels()
        missing = [s for s in all_stages if s not in existing_channels]
        if not missing:
            await interaction.response.send_message(
                "Shared pipeline channels already exist. Nothing to do. "
                "(If you need to move/recreate them, update the channel IDs manually in the database.)",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild

        category = discord.utils.get(guild.categories, name=config.SHARED_PIPELINE_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(name=config.SHARED_PIPELINE_CATEGORY_NAME)

        created = []
        for stage in missing:
            existing_channel = discord.utils.get(category.channels, name=stage)
            if existing_channel:
                channel = existing_channel
            else:
                overwrites = role_overwrites(guild, config.CHANNEL_ROLE_PERMISSIONS.get(stage, []))
                channel = await guild.create_text_channel(
                    name=stage, category=category, overwrites=overwrites,
                    topic=config.CHANNEL_INFO.get(stage, ""),
                )
                info_text = config.CHANNEL_INFO.get(stage, "")
                if info_text:
                    info_msg = await channel.send(f"ℹ️ {info_text}")
                    try:
                        await info_msg.pin(reason="Channel usage info")
                    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                        pass
            db.set_shared_channel(stage, channel.id)
            created.append(channel.name)

        await interaction.followup.send(
            f"✅ Shared pipeline channels ready under **{category.name}**: "
            + ", ".join(f"#{c}" for c in created),
            ephemeral=True,
        )

    @setup_group.command(
        name="info-channel",
        description="Post (or refresh) a guide to how this bot works in #information.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_info_channel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild

        category = discord.utils.get(guild.categories, name=config.SHARED_PIPELINE_CATEGORY_NAME)
        if category is None:
            category = await guild.create_category(name=config.SHARED_PIPELINE_CATEGORY_NAME)

        channel = discord.utils.get(category.channels, name=config.INFORMATION_CHANNEL_NAME)
        if channel is None:
            # Visible to everyone regardless of role (it's meant to be read
            # before asking), but read-only - only Pallet Admin can post,
            # so the guide can't be buried under chat.
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            }
            admin_role = discord.utils.get(guild.roles, name=config.ROLE_ADMIN)
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True
                )
            channel = await guild.create_text_channel(
                name=config.INFORMATION_CHANNEL_NAME, category=category, overwrites=overwrites,
                topic="How Almighty Pallet Bot works and how to use it.",
            )
        else:
            # Re-running this command refreshes the guide rather than piling
            # up duplicates - clear this bot's own previous messages first
            # (anything anyone else posted is left alone, though the channel
            # is read-only so that should be rare).
            async for msg in channel.history(limit=200):
                if msg.author == interaction.client.user:
                    try:
                        await msg.delete()
                    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                        pass

        for embed in information_content.build_embeds():
            await channel.send(embed=embed)

        await interaction.followup.send(f"✅ Posted the bot guide in {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PalletSetup(bot))
