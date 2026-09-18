"""
Handles the full item lifecycle after a pallet's channels exist:

  data-entry (per-pallet)  --(auto)-->  automated-review (SHARED)
    --(AI)--> queue-review (SHARED)
  queue-review --(approve)--> awaiting-listing (SHARED)
  queue-review --(reject)--> back to that pallet's own data-entry
  awaiting-listing --(button)--> listed (SHARED)
  listed --(button)--> sold (SHARED)
  sold --(button)--> shipped (stays in the Sold channel, just checked off)

Every stage from automated-review onward is ONE channel shared by every
pallet (see config.SHARED_STAGE_CHANNELS) rather than a per-pallet copy -
this is what keeps total channel count from scaling with pallet count.
Because items from many pallets now sit side by side in the same channel,
every card's embed leads with a "Pallet" field so it's never ambiguous
which pallet an item belongs to.

Photos are saved to local disk the moment they're submitted, so every
later repost (to automated-review, queue-review, etc.) re-uploads from
disk rather than depending on Discord's CDN links surviving after the
originating message is deleted.

Any change that affects the numbers on a pallet's live finance/status card
(item received, item moved stage) triggers finance_utils.refresh_finance_message
so that card never goes stale.
"""
import json
from pathlib import Path

import discord
from discord.ext import commands, tasks

import config
import database as db
import ai_review
import finance_utils

PHOTO_DIR = Path("data/photos")


def photo_dir_for(item_id: int) -> Path:
    d = PHOTO_DIR / str(item_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


async def send_item_card(channel: discord.TextChannel, item: dict, view: discord.ui.View = None,
                          extra_text: str = "") -> discord.Message:
    """
    Reposts an item's photos + current description as a fresh message in
    `channel`. Always leads with a Pallet field, since shared channels mix
    items from every pallet together.

    Discord's embed API only lets a single embed carry ONE "main" image via
    set_image() - any additional files sent alongside it show up as bare,
    disconnected attachments rendered above the embed. The fix: send one
    embed per photo, all in the same message - Discord visually stacks
    multiple embeds on one message together as a single grouped block.
    """
    photo_paths = json.loads(item["photo_urls"])
    pallet = db.get_pallet(item["pallet_id"])
    pallet_name = pallet["name"] if pallet else f"(pallet #{item['pallet_id']})"

    files = []
    for p in photo_paths:
        path = Path(p)
        if path.exists():
            files.append(discord.File(path, filename=path.name))

    title = item.get("ai_title") or f"Item #{item['item_number']}"
    description = item.get("ai_description") or item.get("raw_description") or "(no description yet)"
    flags = item.get("ai_flags")
    color = discord.Color.orange()

    main_embed = discord.Embed(title=title, description=description, color=color)
    main_embed.add_field(name="📦 Pallet", value=pallet_name, inline=True)
    main_embed.add_field(name="Item #", value=str(item["item_number"]), inline=True)
    main_embed.add_field(name="Status", value=item["status"], inline=True)
    if flags and flags not in ("[]", None, ""):
        main_embed.add_field(name="⚠️ Flags", value=str(flags), inline=False)
    if extra_text:
        main_embed.set_footer(text=extra_text)

    embeds = [main_embed]
    if files:
        main_embed.set_image(url=f"attachment://{files[0].filename}")
        for extra_file in files[1:]:
            gallery_embed = discord.Embed(color=color)
            gallery_embed.set_image(url=f"attachment://{extra_file.filename}")
            embeds.append(gallery_embed)

    msg = await channel.send(embeds=embeds, files=files, view=view)
    return msg


class EditDescriptionModal(discord.ui.Modal, title="Edit Listing Description"):
    def __init__(self, item_id: int):
        super().__init__()
        self.item_id = item_id
        item = db.get_item(item_id)
        self.new_description = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=(item.get("ai_description") or item.get("raw_description") or "")[:4000],
            max_length=4000,
        )
        self.add_item(self.new_description)

    async def on_submit(self, interaction: discord.Interaction):
        db.update_description(self.item_id, self.new_description.value)
        await interaction.response.send_message("Description updated. Re-approve when ready.", ephemeral=True)
        all_embeds = list(interaction.message.embeds)
        all_embeds[0].description = self.new_description.value
        await interaction.message.edit(embeds=all_embeds)


class QueueReviewView(discord.ui.View):
    """Buttons shown on each item card sitting in the shared Queue Review channel."""

    def __init__(self, item_id: int):
        super().__init__(timeout=None)
        self.item_id = item_id
        self.approve.custom_id = f"pallet_bot:qr_approve:{item_id}"
        self.edit.custom_id = f"pallet_bot:qr_edit:{item_id}"
        self.reject.custom_id = f"pallet_bot:qr_reject:{item_id}"

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.move_to_awaiting_listing(interaction, self.item_id)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.blurple, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditDescriptionModal(self.item_id))

    @discord.ui.button(label="Reject / Send Back", style=discord.ButtonStyle.red, emoji="↩️")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.reject_to_data_entry(interaction, self.item_id)


class AwaitingListingView(discord.ui.View):
    def __init__(self, item_id: int):
        super().__init__(timeout=None)
        self.item_id = item_id
        self.mark_listed.custom_id = f"pallet_bot:mark_listed:{item_id}"

    @discord.ui.button(label="Mark as Listed", style=discord.ButtonStyle.green, emoji="🏷️")
    async def mark_listed(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.move_to_listed(interaction, self.item_id)


class ListedView(discord.ui.View):
    def __init__(self, item_id: int):
        super().__init__(timeout=None)
        self.item_id = item_id
        self.mark_sold.custom_id = f"pallet_bot:mark_sold:{item_id}"

    @discord.ui.button(label="Mark as Sold", style=discord.ButtonStyle.green, emoji="💰")
    async def mark_sold(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.move_to_sold(interaction, self.item_id)


class ShippedView(discord.ui.View):
    def __init__(self, item_id: int):
        super().__init__(timeout=None)
        self.item_id = item_id
        self.mark_shipped.custom_id = f"pallet_bot:mark_shipped:{item_id}"

    @discord.ui.button(label="Mark as Shipped", style=discord.ButtonStyle.blurple, emoji="🚚")
    async def mark_shipped(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.mark_shipped(interaction, self.item_id)


class ItemFlow(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._views_reconnected = False

    async def cog_load(self):
        self.stale_check_loop.start()

    def cog_unload(self):
        self.stale_check_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready can fire more than once (e.g. after a reconnect), but views
        # only need registering once per process lifetime.
        if self._views_reconnected:
            return
        self._views_reconnected = True

        view_for_status = {
            db.STATUS_QUEUE_REVIEW: QueueReviewView,
            db.STATUS_AWAITING_LISTING: AwaitingListingView,
            db.STATUS_LISTED: ListedView,
            db.STATUS_SOLD: ShippedView,
        }

        open_items = db.get_open_items_for_reconnect()
        reconnected = 0
        for item in open_items:
            view_cls = view_for_status.get(item["status"])
            if not view_cls:
                continue
            try:
                self.bot.add_view(view_cls(item["id"]), message_id=int(item["current_message_id"]))
                reconnected += 1
            except Exception as e:
                print(f"[item_flow] Could not reconnect view for item {item['id']}: {e}")

        print(f"[item_flow] Reconnected buttons on {reconnected} in-flight item(s).")

    # ---------------------------------------------------------- data entry --

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        # data-entry is the one stage still looked up via channel_map (it's
        # per-pallet, not shared), so this existing lookup is unaffected by
        # the shared-channel change.
        stage = db.get_stage_for_channel(message.channel.id)
        if stage != "data-entry":
            return
        if not message.attachments:
            await message.reply(
                "Attach at least one photo with your description so this can be logged as an item.",
                delete_after=15,
            )
            return

        pallet_id = db.get_pallet_id_for_channel(message.channel.id)

        # If this message is a reply to a REJECTED item's "sent back for
        # redo" card, treat it as a correction of that SAME item rather than
        # a brand new one - otherwise every resubmission double-counted (a
        # new item row was created AND the old rejected card was left
        # sitting in Data Entry forever with nothing to clean it up).
        resubmit_item = None
        old_card_channel = None
        if message.reference and message.reference.message_id:
            candidate = db.get_item_by_message(message.channel.id, message.reference.message_id)
            if candidate and candidate["pallet_id"] == pallet_id and candidate["status"] == db.STATUS_REJECTED:
                resubmit_item = candidate
                old_card_channel = message.channel  # rejected cards always land back in data-entry

        if resubmit_item:
            item_id = resubmit_item["id"]
            # Clear out the old photo files before saving new ones, so a
            # resubmission doesn't end up with a mix of old and new images.
            folder = photo_dir_for(item_id)
            for old_file in folder.glob("*"):
                old_file.unlink(missing_ok=True)
        else:
            item_id = db.create_item(
                pallet_id=pallet_id,
                raw_description=message.content or "",
                photo_urls=[],
                submitted_by=message.author.id,
            )
            folder = photo_dir_for(item_id)

        saved_paths = []
        for i, attachment in enumerate(message.attachments):
            ext = Path(attachment.filename).suffix or ".jpg"
            dest = folder / f"photo_{i}{ext}"
            await attachment.save(dest)
            saved_paths.append(str(dest))

        if resubmit_item:
            db.resubmit_item(item_id, message.content or "", saved_paths, submitted_by=message.author.id)
            # Delete the old rejected card - it's been superseded by this
            # resubmission, so leaving it around would just be a duplicate.
            try:
                old_msg = await old_card_channel.fetch_message(resubmit_item["current_message_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass
        else:
            with db.get_conn() as conn:
                conn.execute("UPDATE items SET photo_urls = ? WHERE id = ?", (json.dumps(saved_paths), item_id))

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        # Either a new item was received, or a rejected one just got fixed -
        # either way the pallet's stage counts changed, so refresh its card.
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

        automated_review_channel = self.bot.get_channel(
            db.resolve_channel_id(pallet_id, "automated-review")
        )
        if automated_review_channel is None:
            await message.channel.send(
                "⚠️ The shared Automated Review channel isn't set up yet - ask a Pallet Admin "
                "to run `/setup-shared-channels`. This item was logged but won't move further "
                "until that's done.",
            )
            return

        pallet = db.get_pallet(pallet_id)
        item_num = db.get_item(item_id)["item_number"]
        resubmit_note = " (resubmitted)" if resubmit_item else ""

        if not config.AI_ENABLED:
            await automated_review_channel.send(
                f"⏭️ AI review not configured - **{pallet['name']}** item #{item_num}{resubmit_note} "
                f"passed through to Queue Review as-is."
            )
            await self.send_to_queue_review(item_id)
            return

        placeholder = await automated_review_channel.send(
            f"🔄 Reviewing **{pallet['name']}** item #{item_num}{resubmit_note}..."
        )
        await self.run_ai_review(item_id, placeholder)

    async def send_to_queue_review(self, item_id: int):
        """Moves an item into Queue Review without an AI pass - used both when
        AI_ENABLED is False and as the shared final step after a real AI review."""
        db.update_status(item_id, db.STATUS_QUEUE_REVIEW, note="Sent to queue review")
        item = db.get_item(item_id)
        pallet_id = item["pallet_id"]
        queue_channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "queue-review"))
        view = QueueReviewView(item_id)
        extra = "" if config.AI_ENABLED else "AI review: not configured (raw submission)"
        msg = await send_item_card(queue_channel, item, view=view, extra_text=extra)
        db.update_status(item_id, db.STATUS_QUEUE_REVIEW, new_message_id=msg.id)
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def run_ai_review(self, item_id: int, placeholder_message: discord.Message):
        item = db.get_item(item_id)
        photo_paths = json.loads(item["photo_urls"])

        result = await ai_review.review_item(photo_paths, item["raw_description"])

        db.save_ai_review(
            item_id,
            title=result.get("suggested_title", ""),
            description=result.get("suggested_description", ""),
            flags=", ".join(result.get("flags", [])) if result.get("flags") else "",
        )

        try:
            await placeholder_message.delete()
        except discord.HTTPException:
            pass

        confidence = result.get("confidence", "unknown")
        db.update_status(item_id, db.STATUS_QUEUE_REVIEW, note="AI review complete")
        item = db.get_item(item_id)
        pallet_id = item["pallet_id"]
        queue_channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "queue-review"))
        view = QueueReviewView(item_id)
        msg = await send_item_card(
            queue_channel, item, view=view, extra_text=f"AI confidence: {confidence}"
        )
        db.update_status(item_id, db.STATUS_QUEUE_REVIEW, new_message_id=msg.id)
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    # ------------------------------------------------------------ movement --

    async def move_to_awaiting_listing(self, interaction: discord.Interaction, item_id: int):
        if not await self._require_role(interaction, config.ROLE_QUEUE_REVIEW):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_QUEUE_REVIEW:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}). "
                f"Someone likely clicked at the same time as you.", ephemeral=True
            )
            return
        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "awaiting-listing"))
        view = AwaitingListingView(item_id)
        msg = await send_item_card(channel, item, view=view)
        db.update_status(item_id, db.STATUS_AWAITING_LISTING, actor_id=interaction.user.id, new_message_id=msg.id)
        await interaction.message.delete()
        await interaction.response.send_message(
            f"Approved. Moved to <#{channel.id}> for listing.", ephemeral=True
        )
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def reject_to_data_entry(self, interaction: discord.Interaction, item_id: int):
        if not await self._require_role(interaction, config.ROLE_QUEUE_REVIEW):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_QUEUE_REVIEW:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}).", ephemeral=True
            )
            return
        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "data-entry"))
        msg = await send_item_card(
            channel, item,
            extra_text="⬅️ Sent back for redo - REPLY to this message with corrected photo(s)/note to resubmit "
                       "(don't post a fresh message, or it'll be logged as a separate item).",
        )
        db.update_status(item_id, db.STATUS_REJECTED, actor_id=interaction.user.id, new_message_id=msg.id)
        await interaction.message.delete()
        await interaction.response.send_message("Sent back to Data Entry.", ephemeral=True)
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def move_to_listed(self, interaction: discord.Interaction, item_id: int):
        if not await self._require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_AWAITING_LISTING:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}).", ephemeral=True
            )
            return
        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "listed"))
        view = ListedView(item_id)
        msg = await send_item_card(channel, item, view=view)
        db.update_status(item_id, db.STATUS_LISTED, actor_id=interaction.user.id, new_message_id=msg.id)
        await interaction.message.delete()
        await interaction.response.send_message(f"Marked listed. See <#{channel.id}>.", ephemeral=True)
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def move_to_sold(self, interaction: discord.Interaction, item_id: int):
        if not await self._require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_LISTED:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}).", ephemeral=True
            )
            return
        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "sold"))
        view = ShippedView(item_id)
        msg = await send_item_card(channel, item, view=view)
        db.update_status(item_id, db.STATUS_SOLD, actor_id=interaction.user.id, new_message_id=msg.id)
        await interaction.message.delete()
        await interaction.response.send_message(
            f"Marked sold. 🎉 See <#{channel.id}>. Finance Management can record the sale price "
            f"with `/finance record-sale`.",
            ephemeral=True,
        )
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def mark_shipped(self, interaction: discord.Interaction, item_id: int):
        if not await self._require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_SOLD:
            await interaction.response.send_message(
                f"This item isn't in Sold-awaiting-shipment (current status: {item['status']}).",
                ephemeral=True,
            )
            return
        db.update_status(item_id, db.STATUS_SHIPPED, actor_id=interaction.user.id)
        try:
            all_embeds = list(interaction.message.embeds)
            footer_text = (all_embeds[0].footer.text or "") if all_embeds[0].footer else ""
            all_embeds[0].set_footer(text=footer_text + " — Shipped ✅")
            await interaction.message.edit(embeds=all_embeds, view=None)
        except (discord.HTTPException, IndexError):
            pass
        await interaction.response.send_message("Marked shipped.", ephemeral=True)
        await finance_utils.refresh_finance_message(self.bot, item["pallet_id"])

    async def _require_role(self, interaction: discord.Interaction, role_name: str) -> bool:
        admin_role = discord.utils.get(interaction.guild.roles, name=config.ROLE_ADMIN)
        target_role = discord.utils.get(interaction.guild.roles, name=role_name)
        member_roles = interaction.user.roles
        if (target_role and target_role in member_roles) or (admin_role and admin_role in member_roles):
            return True
        await interaction.response.send_message(
            f"You need the **{role_name}** role to do that.", ephemeral=True
        )
        return False

    # ------------------------------------------------------- stale listing --

    @tasks.loop(hours=config.STALE_CHECK_INTERVAL_HOURS)
    async def stale_check_loop(self):
        # get_stale_listed_items already looks across ALL pallets - the only
        # change here is that the alert now goes to the one shared 10-day-alerts
        # channel (not a per-pallet copy of it), so every ping names the pallet.
        stale_items = db.get_stale_listed_items(config.DAYS_BEFORE_STALE_ALERT)
        alert_channel_id = db.get_shared_channel_id("10-day-alerts")
        channel = self.bot.get_channel(alert_channel_id) if alert_channel_id else None
        for item in stale_items:
            if channel:
                pallet = db.get_pallet(item["pallet_id"])
                pallet_name = pallet["name"] if pallet else f"pallet #{item['pallet_id']}"
                title = item.get("ai_title") or f"Item #{item['item_number']}"
                await channel.send(
                    f"⏰ **{pallet_name}** - **{title}** (item #{item['item_number']}) has been "
                    f"listed for {config.DAYS_BEFORE_STALE_ALERT}+ days. Consider a price drop "
                    f"or refreshed photos."
                )
            db.mark_stale_alert_sent(item["id"])

    @stale_check_loop.before_loop
    async def before_stale_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ItemFlow(bot))
