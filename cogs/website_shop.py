"""
Website-publishing "#website_shop" approval flow, integrated directly into
this bot - same process, same database, same Discord token as every other
cog here. See config.WEBSITE_SHOP_ENABLED's own comment for why this exists
alongside combined_bot.py (a separate, isolated deployment of the same
underlying modules) rather than replacing it.

Off by default: bot.py only adds this cog's module to its COGS list when
config.WEBSITE_SHOP_ENABLED is true, so nothing here runs, nothing here is
even imported, unless that's explicitly turned on.

Reuses combined_intake.py (the "which items are eligible, in what state"
adapter) and combined_delivery.py (the durable approval -> website delivery
bridge) completely unchanged from the separate combined_bot.py deployment -
see those modules' own docstrings for the safety invariants they enforce
(only items at/above config.WEBSITE_SHOP_ITEM_ID_FLOOR are ever considered,
so nothing here can adopt or touch a pre-existing item). shop_discord.py's
views/embeds and shop_approval.py's durable store are likewise reused as-is.
"""
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database as db
import runtime_settings
from combined_delivery import DeliveryService
from combined_intake import IntakeAdapter
from publisher.journal import Journal
from publisher.transport import Transport
from shop_approval import ShopReviewStore

log = logging.getLogger(__name__)


class _AdapterSettings:
    """The handful of fields combined_intake.IntakeAdapter and
    combined_delivery.DeliveryService actually read - built from this bot's
    real config/database, not a separate deployment's setup.json. Deliberately
    has no "data-entry" key: this bot has one such channel per pallet, not
    one for the whole server, and combined_intake.py's channel-identity check
    for that key is skipped entirely when it's absent - see that module."""

    def __init__(self, guild_id, channels, application_id):
        self.guild_id = guild_id
        self.channels = channels
        self.application_id = application_id
        self.item_id_floor = config.WEBSITE_SHOP_ITEM_ID_FLOOR
        self.sku_prefix = config.WEBSITE_SHOP_SKU_PREFIX
        self.photo_directory = Path(config.PHOTO_DIR)


async def _require_role(interaction: discord.Interaction, role_name: str) -> bool:
    admin_role = runtime_settings.resolve_role(interaction.guild, config.ROLE_ADMIN)
    target_role = runtime_settings.resolve_role(interaction.guild, role_name)
    member_roles = interaction.user.roles
    if (target_role and target_role in member_roles) or (admin_role and admin_role in member_roles):
        return True
    await interaction.response.send_message(
        f"You need the **{role_name}** role to do that.", ephemeral=True
    )
    return False


class WebsiteShop(commands.Cog):
    website_group = app_commands.Group(name="website", description="Website approval and safe item controls")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.store = self.adapter = self.journal = self.delivery = None

    async def cog_load(self):
        db.ensure_items_autoincrement_floor(config.WEBSITE_SHOP_ITEM_ID_FLOOR)

        guild_id = str(config.GUILD_ID)
        shop_channel_id = str(config.WEBSITE_SHOP_CHANNEL_ID)
        listed_channel_id = str(db.get_shared_channel_id("listed"))
        sold_channel_id = str(db.get_shared_channel_id("sold"))
        application_id = str(self.bot.user.id)

        self.store = ShopReviewStore(
            config.WEBSITE_SHOP_APPROVAL_DB_PATH,
            guild_id=guild_id, shop_channel_id=shop_channel_id,
            operator_ids=config.WEBSITE_SHOP_OPERATOR_IDS,
        )
        settings = _AdapterSettings(
            guild_id, {"website_shop": shop_channel_id, "listed": listed_channel_id, "sold": sold_channel_id},
            application_id,
        )
        self.adapter = IntakeAdapter(self.bot, settings, self.store)
        self.journal = Journal(
            config.WEBSITE_SHOP_JOURNAL_DB_PATH, config.WEBSITE_SOURCE_ID, guild_id,
            config.WEBSITE_URL, application_id, listed_channel_id, sold_channel_id,
        )
        transport = Transport(config.WEBSITE_URL, config.WEBSITE_SECRET) if config.WEBSITE_PUBLISH_ENABLED else None
        self.delivery = DeliveryService(
            self.store, self.journal, transport, self.adapter.resolve_content, self.adapter.lifecycle,
            enabled=config.WEBSITE_PUBLISH_ENABLED, max_deliveries=1,
        )
        await self.adapter.restore_views()
        if not self.poll_loop.is_running():
            self.poll_loop.start()
        log.info(
            "Website shop ready in #%s. Publishing: %s.",
            shop_channel_id, config.WEBSITE_PUBLISH_ENABLED,
        )

    def cog_unload(self):
        self.poll_loop.cancel()
        if self.adapter is not None:
            self.adapter.close()
        if self.store is not None:
            self.store.close()
        if self.journal is not None:
            self.journal.close()

    @tasks.loop(seconds=config.WEBSITE_SHOP_POLL_SECONDS)
    async def poll_loop(self):
        try:
            await self.adapter.reconcile()
            await self.delivery.tick()
        except Exception:
            log.exception("Website shop poll failed; retained work is checked again next cycle.")

    @poll_loop.before_loop
    async def _before_poll_loop(self):
        await self.bot.wait_until_ready()

    @website_group.command(name="status", description="Check website approval and delivery status")
    async def status(self, interaction: discord.Interaction):
        if not await _require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        counts = self.journal.counts()
        mode = "Live publishing" if config.WEBSITE_PUBLISH_ENABLED else "Preview only - no website sends"
        await interaction.response.send_message(f"{mode}. Delivery counts: {counts}", ephemeral=True)

    @website_group.command(name="review", description="Show or recover the latest website reviews")
    async def review(self, interaction: discord.Interaction):
        if not await _require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        await interaction.response.defer(ephemeral=True)
        await self.adapter.reconcile()
        await interaction.followup.send(
            "Checked website reviews. Use #website_shop to enter price and eBay link, then approve.", ephemeral=True
        )

    @website_group.command(name="hold", description="Put a new-inventory item on hold and queue website removal")
    @app_commands.describe(item_id="The new-inventory item ID shown on its #website_shop review card")
    async def hold(self, interaction: discord.Interaction, item_id: str):
        if not await _require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        if not item_id.isascii() or not item_id.isdigit() or not config.WEBSITE_SHOP_ITEM_ID_FLOOR <= int(item_id) <= 2147483647:
            await interaction.response.send_message(
                "Use the new-inventory item ID from #website_shop. Historical listings are not adopted.", ephemeral=True
            )
            return
        item = db.get_item(int(item_id))
        if not item or item["status"] in {db.STATUS_SOLD, db.STATUS_SHIPPED, db.STATUS_DELETED, db.STATUS_ON_HOLD}:
            await interaction.response.send_message("That item cannot be put on hold. Check its latest card.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.bot.get_cog("ItemFlow").place_item_on_hold(item, db.HOLD_REASON_MANUAL_LISTING, "", interaction.user.id)
        await interaction.followup.send(
            "Placed on hold. Website removal is queued, not yet confirmed." if result else "Hold could not finish. Try again.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(WebsiteShop(bot))
