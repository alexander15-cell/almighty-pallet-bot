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
originating message is deleted. If config.R2_ENABLED, each photo is also
uploaded to Cloudflare R2 for a durable public URL (see r2_storage.py) -
used by the eBay CSV batch export, which can't rely on Discord's links
either.

Any change that affects the numbers on a pallet's live finance/status card
(item received, item moved stage) triggers finance_utils.refresh_finance_message
so that card never goes stale.
"""
import asyncio
import json
from pathlib import Path

import discord
from discord.ext import commands, tasks

import config
import database as db
import ai_review
import ebay_api
import ebay_csv
import finance_utils
import r2_storage

PHOTO_DIR = Path(config.PHOTO_DIR)


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


class EbayConditionSelectView(discord.ui.View):
    """
    First step of Approve: a plain button click can't collect a dropdown
    value (Discord modals only support text inputs, not selects), so
    Approve shows this ephemeral select first. Discord's mobile client
    won't fire the select callback again if you tap an option that's
    already marked `default` (the tap is silently swallowed - it looks
    selected, but nothing happens), so nothing here is pre-checked; instead
    EBAY_DEFAULT_CONDITION_ID is just sorted to the top and labeled, so
    it's still the fastest tap without being stuck if that's the one you
    want. Picking a condition moves to the category step (step 2).
    """

    def __init__(self, item_id: int):
        super().__init__(timeout=300)
        self.item_id = item_id
        ordered = sorted(
            config.EBAY_CONDITIONS,
            key=lambda pair: pair[0] != config.EBAY_DEFAULT_CONDITION_ID,
        )
        self.select = discord.ui.Select(
            placeholder="Select this item's eBay condition...",
            options=[
                discord.SelectOption(
                    label=(label + " (most common)")[:100] if condition_id == config.EBAY_DEFAULT_CONDITION_ID else label,
                    value=condition_id,
                )
                for condition_id, label in ordered
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        condition_id = self.select.values[0]
        item = db.get_item(self.item_id)
        suggested_name = item.get("ai_suggested_category")
        suggested_category_id = config.EBAY_CATEGORIES.get(suggested_name)
        if suggested_category_id:
            await interaction.response.edit_message(
                content=(
                    f"eBay category: **{suggested_name}** (AI suggested). "
                    "Tap \"Change category\" below to pick a different one, or continue to format."
                ),
                view=EbayFormatSelectView(
                    self.item_id, condition_id, suggested_category_id, show_change_category=True,
                ),
            )
        else:
            await interaction.response.edit_message(
                content="Now select this item's eBay category...",
                view=EbayCategorySelectView(self.item_id, condition_id),
            )


class EbayCategorySelectView(discord.ui.View):
    """
    Category picker: shown as step 2 when Automated Review didn't suggest a
    usable category, or when the reviewer taps "Change category" after an
    AI suggestion was auto-applied. Built from config.EBAY_CATEGORIES,
    sorted by how often each has actually been picked (database.
    get_ebay_category_counts) - most-used first, unused/new categories
    alphabetically after. Discord select menus cap out at 25 options; if
    EBAY_CATEGORIES ever grows past that, only the 25 most-used show here
    (logged to console) until pagination gets added.

    Nothing here is pre-checked (`default`) - see EbayConditionSelectView's
    docstring for why that breaks re-tapping the already-highlighted
    option on Discord's mobile client. Picking a category moves to the
    format select (step 3).
    """

    MAX_OPTIONS = 25

    def __init__(self, item_id: int, condition_id: str):
        super().__init__(timeout=300)
        self.item_id = item_id
        self.condition_id = condition_id

        def _is_fallback_only(name: str) -> bool:
            return "(top-level)" in name or "(parent/fallback)" in name

        counts = db.get_ebay_category_counts()
        ranked = sorted(
            config.EBAY_CATEGORIES.items(),
            key=lambda name_and_id: (
                -counts.get(name_and_id[1], 0),
                _is_fallback_only(name_and_id[0]),
                name_and_id[0].lower(),
            ),
        )
        truncated = len(ranked) > self.MAX_OPTIONS
        if truncated:
            print(
                f"[item_flow] config.EBAY_CATEGORIES has {len(config.EBAY_CATEGORIES)} entries, "
                f"over Discord's {self.MAX_OPTIONS}-option select limit - only the "
                f"{self.MAX_OPTIONS} most-used are shown. Needs pagination."
            )
            ranked = ranked[:self.MAX_OPTIONS]

        self.select = discord.ui.Select(
            placeholder="Select this item's eBay category..." + (" (list truncated)" if truncated else ""),
            options=[
                discord.SelectOption(label=name[:100], value=category_id)
                for name, category_id in ranked
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        category_id = self.select.values[0]
        await interaction.response.edit_message(
            content="Fixed price or auction?",
            view=EbayFormatSelectView(self.item_id, self.condition_id, category_id),
        )


class EbayFormatSelectView(discord.ui.View):
    """
    Third step of Approve: fixed-price vs. auction, decided per item rather
    than a global switch. Nothing is pre-checked (see EbayConditionSelectView's
    docstring for why) so either option is always one tap away. Fixed Price
    goes straight to EbayListingModal; Auction adds one more step
    (EbayAuctionDurationSelectView) since eBay needs an explicit *Duration
    for auctions.

    When reached right after an AI-suggested category was auto-applied
    (see EbayConditionSelectView._on_select), show_change_category adds a
    button that goes back to the manual EbayCategorySelectView in case the
    reviewer disagrees with the AI's pick.
    """

    def __init__(self, item_id: int, condition_id: str, category_id: str, show_change_category: bool = False):
        super().__init__(timeout=300)
        self.item_id = item_id
        self.condition_id = condition_id
        self.category_id = category_id
        self.select = discord.ui.Select(
            placeholder="Fixed price or auction?",
            options=[
                discord.SelectOption(label="Fixed Price", value="FixedPrice"),
                discord.SelectOption(label="Auction", value="Auction"),
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)
        if show_change_category:
            change_button = discord.ui.Button(label="Change category", style=discord.ButtonStyle.secondary)
            change_button.callback = self._on_change_category
            self.add_item(change_button)

    async def _on_select(self, interaction: discord.Interaction):
        listing_format = self.select.values[0]
        if listing_format == "Auction":
            await interaction.response.edit_message(
                content="Select this auction's duration...",
                view=EbayAuctionDurationSelectView(self.item_id, self.condition_id, self.category_id),
            )
        else:
            await interaction.response.send_modal(
                EbayListingModal(self.item_id, self.condition_id, self.category_id, "FixedPrice", None)
            )

    async def _on_change_category(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="Now select this item's eBay category...",
            view=EbayCategorySelectView(self.item_id, self.condition_id),
        )


class EbayAuctionDurationSelectView(discord.ui.View):
    """Only reached when Auction was picked in EbayFormatSelectView - eBay's
    *Duration values for auctions (config.EBAY_AUCTION_DURATIONS)."""

    def __init__(self, item_id: int, condition_id: str, category_id: str):
        super().__init__(timeout=300)
        self.item_id = item_id
        self.condition_id = condition_id
        self.category_id = category_id
        self.select = discord.ui.Select(
            placeholder="Select auction duration...",
            options=[
                discord.SelectOption(label=label, value=duration)
                for duration, label in config.EBAY_AUCTION_DURATIONS
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        duration = self.select.values[0]
        await interaction.response.send_modal(
            EbayListingModal(self.item_id, self.condition_id, self.category_id, "Auction", duration)
        )


class EbayListingModal(discord.ui.Modal, title="eBay Listing Details"):
    """
    Final step of Approve. Title/price are required (condition/category/
    format were already picked in steps 1-3, and are required too since
    they're selects, not optional text fields); item specifics are freeform
    "Key: Value" lines, one per attribute, and can be left blank. Description
    and photos are reused as-is from Data Entry - not re-entered here.

    If there's no existing eBay listing data yet, the price field pre-fills
    from items.ai_suggested_price (Automated Review's rough estimate from
    general knowledge, NOT real market data) - always editable, never
    treated as final. See ai_review.py for a note on a possible future
    improvement (real eBay "sold" comps via the Browse API).
    """

    def __init__(self, item_id: int, condition_id: str, category_id: str,
                 listing_format: str, auction_duration: str):
        super().__init__()
        self.item_id = item_id
        self.condition_id = condition_id
        self.category_id = category_id
        self.listing_format = listing_format
        self.auction_duration = auction_duration
        item = db.get_item(item_id)
        existing = db.get_ebay_listing_data(item_id)

        specifics_default = ""
        if existing and existing.get("item_specifics"):
            specifics_default = "\n".join(f"{k}: {v}" for k, v in existing["item_specifics"].items())

        if existing:
            price_default = f"{existing['price']:.2f}"
        elif item.get("ai_suggested_price") is not None:
            price_default = f"{item['ai_suggested_price']:.2f}"
        else:
            price_default = ""

        price_label = "Starting Bid (USD)" if listing_format == "Auction" else "Price (USD)"
        if not existing and item.get("ai_suggested_price") is not None:
            price_label += " - AI est., confirm"

        self.ebay_title = discord.ui.TextInput(
            label="eBay Title (max 80 chars)",
            default=(existing["ebay_title"] if existing else (item.get("ai_title") or "")[:80]),
            max_length=80,
        )
        self.price = discord.ui.TextInput(
            label=price_label[:45],  # Discord TextInput label cap
            default=price_default,
            max_length=12,
        )
        self.item_specifics = discord.ui.TextInput(
            label="Item Specifics (one per line: Key: Value)",
            style=discord.TextStyle.paragraph,
            required=False,
            default=specifics_default,
            max_length=1000,
        )
        self.add_item(self.ebay_title)
        self.add_item(self.price)
        self.add_item(self.item_specifics)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.ebay_title.value.strip()
        price_raw = self.price.value.strip()

        errors = []
        if not title:
            errors.append("Title is required.")
        price = None
        try:
            price = float(price_raw)
            if price < 0:
                errors.append("Price can't be negative.")
        except ValueError:
            errors.append("Price must be a number.")

        if errors:
            await interaction.response.send_message(
                "⚠️ Couldn't save eBay listing data:\n" + "\n".join(f"- {e}" for e in errors) +
                "\n\nClick **Approve** again to retry.",
                ephemeral=True,
            )
            return

        specifics = {}
        for line in self.item_specifics.value.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key and value:
                specifics[key] = value

        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.finalize_ebay_approval(
            interaction, self.item_id, self.condition_id, self.category_id, title, price, specifics,
            self.listing_format, self.auction_duration,
        )


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
        await cog.prompt_ebay_condition(interaction, self.item_id)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.blurple, emoji="✏️")
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EditDescriptionModal(self.item_id))

    @discord.ui.button(label="Reject / Send Back", style=discord.ButtonStyle.red, emoji="↩️")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.reject_to_data_entry(interaction, self.item_id)


class AwaitingListingView(discord.ui.View):
    """
    Three ways an approved item actually gets listed:
      - Add to eBay Batch: appends it to the CSV batch for eBay's Seller Hub
        bulk-upload tool (no API call) and moves it to pending-ebay-upload.
      - List on eBay (API): the live direct-API path, only shown at all once
        config.EBAY_ENABLED is True (real eBay dev credentials are set).
      - Mark Listed (Other): unchanged manual path for FB Marketplace/website/
        anything else - moves straight to Listed.
    """

    def __init__(self, item_id: int):
        super().__init__(timeout=None)
        self.item_id = item_id
        self.add_to_batch.custom_id = f"pallet_bot:ebay_batch:{item_id}"
        self.list_via_api.custom_id = f"pallet_bot:ebay_api_list:{item_id}"
        self.mark_listed_other.custom_id = f"pallet_bot:mark_listed:{item_id}"
        if not config.EBAY_ENABLED:
            self.remove_item(self.list_via_api)

    @discord.ui.button(label="Add to eBay Batch", style=discord.ButtonStyle.blurple, emoji="🛒")
    async def add_to_batch(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.add_to_ebay_batch(interaction, self.item_id)

    @discord.ui.button(label="List on eBay (API)", style=discord.ButtonStyle.gray, emoji="🔌")
    async def list_via_api(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "ItemFlow" = interaction.client.get_cog("ItemFlow")
        await cog.list_on_ebay_api(interaction, self.item_id)

    @discord.ui.button(label="Mark Listed (Other)", style=discord.ButtonStyle.green, emoji="🏷️")
    async def mark_listed_other(self, interaction: discord.Interaction, button: discord.ui.Button):
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

        # Local copy above is what Discord item cards render from (send_item_card
        # reads local files). This adds a second, durable copy in R2 so anything
        # that needs to stay valid long-term (the eBay CSV's PicURL, future item
        # records) doesn't depend on Discord's attachment links, which expire
        # once the originating message is gone. Best-effort: one failed upload
        # doesn't block Data Entry, it just leaves that photo's URL empty.
        # boto3 is synchronous, so each upload runs in a thread rather than
        # blocking the bot's event loop (and every other server it's in) for
        # however long the network call takes.
        if config.R2_ENABLED:
            public_urls = []
            for local_path in saved_paths:
                object_key = f"items/{item_id}/{Path(local_path).name}"
                try:
                    url = await asyncio.to_thread(r2_storage.upload_photo, local_path, object_key)
                    public_urls.append(url)
                except Exception as e:
                    print(f"[item_flow] R2 upload failed for item {item_id} ({local_path}): {e}")
                    public_urls.append(None)
            db.update_photo_public_urls(item_id, public_urls)

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

        suggested_price = result.get("suggested_price")
        try:
            suggested_price = float(suggested_price) if suggested_price is not None else None
        except (TypeError, ValueError):
            suggested_price = None  # model returned something non-numeric - just skip the pre-fill

        db.save_ai_review(
            item_id,
            title=result.get("suggested_title", ""),
            description=result.get("suggested_description", ""),
            flags=", ".join(result.get("flags", [])) if result.get("flags") else "",
            suggested_category=result.get("suggested_category") or None,
            suggested_price=suggested_price,
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

    async def prompt_ebay_condition(self, interaction: discord.Interaction, item_id: int):
        """Step 1 of Approve: role/status checks, then the condition select."""
        if not await self._require_role(interaction, config.ROLE_QUEUE_REVIEW):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_QUEUE_REVIEW:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}). "
                f"Someone likely clicked at the same time as you.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Select this item's eBay condition to continue approving - you'll pick a "
            "category and format (fixed price/auction), then enter title/price/specifics next.",
            view=EbayConditionSelectView(item_id),
            ephemeral=True,
        )

    async def finalize_ebay_approval(self, interaction: discord.Interaction, item_id: int, condition_id: str,
                                      category_id: str, title: str, price: float, specifics: dict,
                                      listing_format: str = "FixedPrice", auction_duration: str = None):
        """Final step of Approve, called from EbayListingModal.on_submit: saves
        the eBay data and actually moves the item to Awaiting Listing."""
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_QUEUE_REVIEW:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}). "
                f"Someone likely clicked at the same time as you.", ephemeral=True
            )
            return

        db.save_ebay_listing_data(
            item_id, ebay_title=title, category_id=category_id, condition_id=condition_id,
            price=price, item_specifics=specifics, listing_format=listing_format,
            auction_duration=auction_duration, actor_id=interaction.user.id,
        )
        db.record_ebay_category_use(category_id)

        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "awaiting-listing"))
        view = AwaitingListingView(item_id)
        msg = await send_item_card(channel, item, view=view)
        db.update_status(item_id, db.STATUS_AWAITING_LISTING, actor_id=interaction.user.id, new_message_id=msg.id)

        # The modal's interaction isn't attached to the original queue-review
        # card message (unlike a direct button click), so that card has to be
        # fetched and removed explicitly instead of via interaction.message.
        old_channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "queue-review"))
        if old_channel and item.get("current_message_id"):
            try:
                old_msg = await old_channel.fetch_message(item["current_message_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        condition_label = config.EBAY_CONDITION_LABELS.get(condition_id, condition_id)
        format_note = (
            f"Auction, {dict(config.EBAY_AUCTION_DURATIONS).get(auction_duration, auction_duration)}, "
            f"starting bid ${price:.2f}"
            if listing_format == "Auction" else
            f"Fixed Price, ${price:.2f}"
        )
        await interaction.response.send_message(
            f"Approved. eBay listing data saved (condition: **{condition_label}**, {format_note}). "
            f"Moved to <#{channel.id}> for listing.",
            ephemeral=True,
        )
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def add_to_ebay_batch(self, interaction: discord.Interaction, item_id: int):
        """'Add to eBay Batch' - the CSV fallback path. Never calls any eBay
        API; just appends a row and moves the item to pending-ebay-upload
        until an admin confirms via /ebay confirm-listed that eBay actually
        processed the uploaded CSV."""
        if not await self._require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_AWAITING_LISTING:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}).", ephemeral=True
            )
            return
        listing = db.get_ebay_listing_data(item_id)
        if not listing:
            await interaction.response.send_message(
                "No eBay listing data was captured for this item during Queue Review "
                "(it may predate this feature). Use **Mark Listed (Other)** instead, or "
                "have Queue Review re-approve it with the eBay details filled in.",
                ephemeral=True,
            )
            return

        ebay_csv.append_item_to_batch(item, listing)

        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "pending-ebay-upload"))
        msg = await send_item_card(
            channel, item,
            extra_text="🛒 Added to the eBay CSV batch - waiting for someone to run "
                       "/ebay export-batch, upload it in Seller Hub, then confirm with "
                       "/ebay confirm-listed.",
        )
        db.update_status(item_id, db.STATUS_PENDING_EBAY_UPLOAD, actor_id=interaction.user.id, new_message_id=msg.id)
        await interaction.message.delete()
        await interaction.response.send_message(
            f"Added to the eBay batch CSV. Moved to <#{channel.id}> pending upload confirmation.",
            ephemeral=True,
        )
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def list_on_ebay_api(self, interaction: discord.Interaction, item_id: int):
        """'List on eBay (API)' - the direct-API path, only reachable at all
        once config.EBAY_ENABLED is True (the button is removed from the view
        otherwise). See ebay_api.py - it's currently a stub pending eBay
        developer API approval."""
        if not config.EBAY_ENABLED:
            await interaction.response.send_message("Direct eBay API listing isn't enabled.", ephemeral=True)
            return
        if not await self._require_role(interaction, config.ROLE_LISTING_MGMT):
            return
        item = db.get_item(item_id)
        if item["status"] != db.STATUS_AWAITING_LISTING:
            await interaction.response.send_message(
                f"This item was already moved on (current status: {item['status']}).", ephemeral=True
            )
            return
        listing = db.get_ebay_listing_data(item_id)
        if not listing:
            await interaction.response.send_message(
                "No eBay listing data was captured for this item during Queue Review.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await ebay_api.create_listing(item, listing)
        except NotImplementedError as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"eBay API listing failed: {e}", ephemeral=True)
            return

        pallet_id = item["pallet_id"]
        channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "listed"))
        view = ListedView(item_id)
        msg = await send_item_card(
            channel, item, view=view,
            extra_text=f"✅ Listed live on eBay (item ID: {result.get('ebay_item_id', '?')}).",
        )
        db.update_status(item_id, db.STATUS_LISTED, actor_id=interaction.user.id, new_message_id=msg.id)
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"Listed live on eBay. See <#{channel.id}>.", ephemeral=True)
        await finance_utils.refresh_finance_message(self.bot, pallet_id)

    async def confirm_ebay_pending_item(self, item: dict, actor_id: int) -> bool:
        """
        Used by /ebay confirm-listed (cogs/ebay.py) once someone has checked
        that an item from the CSV batch actually went live on eBay - there's
        no live API to detect this automatically. Moves it from
        pending_ebay_upload straight to Listed. Returns False (does nothing)
        if the item isn't actually pending anymore.
        """
        if item["status"] != db.STATUS_PENDING_EBAY_UPLOAD:
            return False

        pallet_id = item["pallet_id"]
        old_channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "pending-ebay-upload"))
        if old_channel and item.get("current_message_id"):
            try:
                old_msg = await old_channel.fetch_message(item["current_message_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        listed_channel = self.bot.get_channel(db.resolve_channel_id(pallet_id, "listed"))
        view = ListedView(item["id"])
        msg = await send_item_card(
            listed_channel, item, view=view, extra_text="✅ Confirmed live on eBay from batch upload.",
        )
        db.update_status(item["id"], db.STATUS_LISTED, actor_id=actor_id, new_message_id=msg.id)
        return True

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
