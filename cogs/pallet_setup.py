"""
Handles two things:

1. /setup-shared-channels (admin, run ONCE ever): creates the shared pipeline
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
sets cost afterward with /setprice (see cogs/finance.py).
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import finance_utils


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
                "to run `/setup-shared-channels` once, first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(NewPalletModal())


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


class PalletSetup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Register the persistent view so the button keeps working across restarts
        self.bot.add_view(NewPalletView())

    @app_commands.command(
        name="setup-hub",
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

    @app_commands.command(
        name="setup-shared-channels",
        description="One-time setup: creates the shared pipeline channels used by every pallet.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_shared_channels(self, interaction: discord.Interaction):
        if db.is_shared_channels_setup():
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
        for stage in config.SHARED_STAGE_CHANNELS:
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
                    except discord.HTTPException:
                        pass
            db.set_shared_channel(stage, channel.id)
            created.append(channel.name)

        await interaction.followup.send(
            f"✅ Shared pipeline channels ready under **{category.name}**: "
            + ", ".join(f"#{c}" for c in created),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(PalletSetup(bot))
