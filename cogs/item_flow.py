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
import re
from pathlib import Path

import discord
from discord.ext import commands, tasks

import config
import database as db
import ai_review
import ebay_api
import ebay_csv
import ebay_taxonomy
import finance_utils
import r2_storage
import runtime_settings

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
        suggested_query = item.get("ai_suggested_category")
        matches = ebay_taxonomy.search(suggested_query, limit=1) if suggested_query else []
        if matches:
            category_id, category_path = matches[0]
            await interaction.response.edit_message(
                content=(
                    f"eBay category: **{category_path}** (AI suggested \"{suggested_query}\", "
                    "matched against eBay's real category list). Tap \"Change category\" below "
                    "to pick a different one, or continue to format."
                ),
                view=EbayFormatSelectView(
                    self.item_id, condition_id, category_id, show_change_category=True,
                ),
            )
        else:
            await interaction.response.edit_message(
                content="Now select this item's eBay category...",
                view=EbayCategoryPickView(self.item_id, condition_id),
            )


def _category_option_label(full_path: str, prefix: str = "") -> str:
    """
    Discord's SelectOption.label caps at 100 chars, and eBay's own full
    breadcrumb paths (L1 > L2 > ... > leaf) can run well past that (~28% of
    them do) - truncating the raw string from the end would risk cutting
    off the actual leaf category name (the part a reviewer actually needs
    to read) while keeping less useful top-level context. Puts the leaf
    name first instead, so it's never the part that gets cut, with as much
    ancestor context as still fits after it. `prefix` (e.g. "✅ " for a
    confirmed-working category) goes directly on the leaf name, not
    smashed into the truncated ancestor text where it'd be easy to miss.
    """
    parts = full_path.split(" > ")
    leaf = prefix + parts[-1]
    ancestors = " > ".join(parts[:-1])
    if not ancestors:
        return leaf[:100]
    label = f"{leaf} ({ancestors})"
    if len(label) <= 100:
        return label
    room = 100 - len(leaf) - 4  # " (" + ")" + the "…" itself
    if room > 10:
        return f"{leaf} ({ancestors[:room].rstrip()}…)"
    return leaf[:100]


class EbayCategoryPickView(discord.ui.View):
    """
    Manual category picker (step 2), shown when Automated Review didn't
    suggest a usable category, or the reviewer taps "Change category". eBay
    has ~18,000 real leaf categories (see ebay_taxonomy.py, built from
    eBay's own official category export) - far more than Discord's
    25-option select cap, so there's no single static dropdown that could
    ever cover every category this bot might need. Instead: a quick-pick
    dropdown of categories this team has actually used before (sorted
    CONFIRMED-live-on-eBay first via database.get_ebay_category_confirmed_counts,
    then by raw pick count - the same signal this used before eBay's real
    taxonomy data existed, still useful for "which of our proven categories
    fits again"), plus a "🔍 Search categories" button that searches the
    full official taxonomy by keyword for anything not already in the
    quick-pick list - covering every real eBay category, not a hand-curated
    subset capped at 25.

    Nothing here is pre-checked (`default`) - see EbayConditionSelectView's
    docstring for why that breaks re-tapping the already-highlighted
    option on Discord's mobile client. Picking a category (quick-pick or
    search result) moves to the format select (step 3).
    """

    MAX_QUICK_PICK = 24  # leaves room for the search button as a 25th component in the worst case

    def __init__(self, item_id: int, condition_id: str):
        super().__init__(timeout=300)
        self.item_id = item_id
        self.condition_id = condition_id

        counts = db.get_ebay_category_counts()
        confirmed_counts = db.get_ebay_category_confirmed_counts()
        ranked_ids = sorted(
            counts.keys() | confirmed_counts.keys(),
            key=lambda category_id: (-confirmed_counts.get(category_id, 0), -counts.get(category_id, 0)),
        )

        options = []
        for category_id in ranked_ids:
            path = ebay_taxonomy.get_path(category_id)
            if not path:
                # An id this team used before that no longer resolves (a
                # stale one from before a taxonomy refresh, or a manually
                # entered ID via /ebay retry-item) - skip rather than show
                # a blank/unreadable option.
                continue
            prefix = "✅ " if confirmed_counts.get(category_id) else ""
            options.append(discord.SelectOption(label=_category_option_label(path, prefix=prefix), value=category_id))
            if len(options) >= self.MAX_QUICK_PICK:
                break

        if options:
            self.select = discord.ui.Select(placeholder="Or pick a previously-used category...", options=options)
            self.select.callback = self._on_select
            self.add_item(self.select)

        search_button = discord.ui.Button(label="🔍 Search categories", style=discord.ButtonStyle.secondary)
        search_button.callback = self._on_search
        self.add_item(search_button)

    async def _on_select(self, interaction: discord.Interaction):
        category_id = self.select.values[0]
        await interaction.response.edit_message(
            content="Fixed price or auction?",
            view=EbayFormatSelectView(self.item_id, self.condition_id, category_id),
        )

    async def _on_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EbayCategorySearchModal(self.item_id, self.condition_id))


class EbayCategorySearchModal(discord.ui.Modal, title="Search eBay Categories"):
    """
    Reached via EbayCategoryPickView's "🔍 Search categories" button (or
    EbayCategorySearchResultsView's "Search again"). Looks the reviewer's
    keywords up against eBay's full official taxonomy (ebay_taxonomy.py)
    and shows up to 25 ranked matches to pick from - no static dropdown
    could ever list all ~18,000 real categories, so search replaces
    browsing for anything not already in the quick-pick list.
    """

    def __init__(self, item_id: int, condition_id: str):
        super().__init__()
        self.item_id = item_id
        self.condition_id = condition_id
        self.query = discord.ui.TextInput(
            label="Keywords (e.g. \"cordless drill\")",
            placeholder="A few words describing the item type",
            max_length=100,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        query = self.query.value.strip()
        matches = ebay_taxonomy.search(query, limit=25)
        if not matches:
            await interaction.response.edit_message(
                content=f"No eBay categories matched \"{query}\" - try different or fewer keywords.",
                view=EbayCategoryPickView(self.item_id, self.condition_id),
            )
            return
        await interaction.response.edit_message(
            content=f"Categories matching \"{query}\" - pick one, or search again:",
            view=EbayCategorySearchResultsView(self.item_id, self.condition_id, matches),
        )


class EbayCategorySearchResultsView(discord.ui.View):
    """Up to 25 ranked eBay category search results (from
    EbayCategorySearchModal) as a select, plus a "Search again" button for
    when none of them fit. matches' category IDs are always unique (each
    comes from a distinct row in ebay_taxonomy.py's index), so unlike the
    old static EBAY_CATEGORIES dict this can't produce duplicate option
    values."""

    def __init__(self, item_id: int, condition_id: str, matches: list):
        super().__init__(timeout=300)
        self.item_id = item_id
        self.condition_id = condition_id
        self.select = discord.ui.Select(
            placeholder="Pick a category...",
            options=[
                discord.SelectOption(label=_category_option_label(full_path), value=category_id)
                for category_id, full_path in matches
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        search_again = discord.ui.Button(label="🔍 Search again", style=discord.ButtonStyle.secondary)
        search_again.callback = self._on_search_again
        self.add_item(search_again)

    async def _on_select(self, interaction: discord.Interaction):
        category_id = self.select.values[0]
        await interaction.response.edit_message(
            content="Fixed price or auction?",
            view=EbayFormatSelectView(self.item_id, self.condition_id, category_id),
        )

    async def _on_search_again(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EbayCategorySearchModal(self.item_id, self.condition_id))


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
    button that goes back to the manual EbayCategoryPickView in case the
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
            view=EbayCategoryPickView(self.item_id, self.condition_id),
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


def _parse_weight_lb(weight_raw: str) -> float:
    """Parses EbayListingModal's weight field into pounds > 0, or raises
    ValueError with the exact message to show the reviewer."""
    try:
        weight_lb = float(weight_raw)
    except ValueError:
        raise ValueError("Weight must be a number (pounds), e.g. 2.5.")
    if weight_lb <= 0:
        raise ValueError("Weight must be greater than 0.")
    return weight_lb


def _parse_dimensions(dims_raw: str) -> tuple:
    """Parses EbayListingModal's "L x W x H" dimensions field into three
    positive inch values, or raises ValueError with the exact message to
    show the reviewer."""
    parts = [p.strip() for p in re.split(r"[xX×]", dims_raw) if p.strip()]
    if len(parts) != 3:
        raise ValueError("Dimensions must be three numbers separated by 'x', e.g. 12 x 8 x 4.")
    try:
        length_in, width_in, height_in = (float(p) for p in parts)
    except ValueError:
        raise ValueError("Dimensions must all be numbers, e.g. 12 x 8 x 4.")
    if any(d <= 0 for d in (length_in, width_in, height_in)):
        raise ValueError("Dimensions must all be greater than 0.")
    return length_in, width_in, height_in


class EbayListingModal(discord.ui.Modal, title="eBay Listing Details"):
    """
    Final step of Approve. Title/price/weight/dimensions are required
    (condition/category/format were already picked in steps 1-3, and are
    required too since they're selects, not optional text fields); item
    specifics are freeform "Key: Value" lines, one per attribute, and can
    be left blank. Description and photos are reused as-is from Data
    Entry - not re-entered here. That's 5 fields total, right at Discord's
    modal cap.

    Weight/dimensions are mandatory (not optional like specifics) because
    eBay's Calculated shipping (see ebay_csv.py) needs them on every row -
    without real values here, every listing in a batch would fail exactly
    like the missing Item.Location/shipping-service bugs already fixed.
    They feed the Pirate Ship CSV export too, since every approved item
    reaches this same modal regardless of which platform it eventually
    sells on.

    If there's no existing eBay listing data yet, price/weight/dimensions
    pre-fill from Automated Review's own rough estimates (items.
    ai_suggested_price / ai_suggested_weight_lb / ai_suggested_*_in) -
    general-knowledge/visual guesses, NOT real market data or actual
    measurements - always editable, never treated as final. See ai_review.py
    for more on both estimates' limits.
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

        if existing and existing.get("weight_lb") is not None:
            weight_default = f"{existing['weight_lb']:g}"
            weight_label = "Weight (lb)"
        elif item.get("ai_suggested_weight_lb") is not None:
            weight_default = f"{item['ai_suggested_weight_lb']:g}"
            weight_label = "Weight (lb) - AI est., confirm"
        else:
            weight_default = ""
            weight_label = "Weight (lb)"

        dims_existing = existing and all(existing.get(k) is not None for k in ("length_in", "width_in", "height_in"))
        dims_ai = all(item.get(k) is not None for k in ("ai_suggested_length_in", "ai_suggested_width_in", "ai_suggested_height_in"))
        if dims_existing:
            dims_default = f"{existing['length_in']:g} x {existing['width_in']:g} x {existing['height_in']:g}"
            dims_label = "Dimensions L x W x H (in)"
        elif dims_ai:
            dims_default = (
                f"{item['ai_suggested_length_in']:g} x {item['ai_suggested_width_in']:g} x "
                f"{item['ai_suggested_height_in']:g}"
            )
            dims_label = "Dimensions LxWxH (in) - AI est., confirm"
        else:
            dims_default = ""
            dims_label = "Dimensions L x W x H (in)"

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
        self.weight = discord.ui.TextInput(
            label=weight_label[:45],
            default=weight_default,
            max_length=10,
        )
        self.dimensions = discord.ui.TextInput(
            label=dims_label[:45],
            placeholder="e.g. 12 x 8 x 4",
            default=dims_default,
            max_length=30,
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
        self.add_item(self.weight)
        self.add_item(self.dimensions)
        self.add_item(self.item_specifics)

    async def on_submit(self, interaction: discord.Interaction):
        title = self.ebay_title.value.strip()
        price_raw = self.price.value.strip()
        weight_raw = self.weight.value.strip()
        dims_raw = self.dimensions.value.strip()

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

        weight_lb = None
        try:
            weight_lb = _parse_weight_lb(weight_raw)
        except ValueError as e:
            errors.append(str(e))

        length_in = width_in = height_in = None
        try:
            length_in, width_in, height_in = _parse_dimensions(dims_raw)
        except ValueError as e:
            errors.append(str(e))

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
            self.listing_format, self.auction_duration, weight_lb, length_in, width_in, height_in,
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

        def _safe_float(value):
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None  # model returned something non-numeric - just skip the pre-fill

        db.save_ai_review(
            item_id,
            title=result.get("suggested_title", ""),
            description=result.get("suggested_description", ""),
            flags=", ".join(result.get("flags", [])) if result.get("flags") else "",
            suggested_category=result.get("suggested_category") or None,
            suggested_price=_safe_float(result.get("suggested_price")),
            suggested_weight_lb=_safe_float(result.get("estimated_weight_lb")),
            suggested_length_in=_safe_float(result.get("estimated_length_in")),
            suggested_width_in=_safe_float(result.get("estimated_width_in")),
            suggested_height_in=_safe_float(result.get("estimated_height_in")),
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
                                      listing_format: str = "FixedPrice", auction_duration: str = None,
                                      weight_lb: float = None, length_in: float = None,
                                      width_in: float = None, height_in: float = None):
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
            auction_duration=auction_duration, weight_lb=weight_lb, length_in=length_in,
            width_in=width_in, height_in=height_in, actor_id=interaction.user.id,
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
            f"Approved. eBay listing data saved (condition: **{condition_label}**, {format_note}, "
            f"{weight_lb:g} lb, {length_in:g}x{width_in:g}x{height_in:g} in). "
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

    async def confirm_ebay_pending_item(self, item: dict, actor_id: int, ebay_item_id: str = None) -> bool:
        """
        Used by /ebay confirm-listed and /ebay import-results (cogs/ebay.py)
        once an item from a CSV batch is confirmed to have actually gone
        live on eBay - either by hand after checking Seller Hub, or parsed
        from a downloaded results CSV. Moves it from pending_ebay_upload
        straight to Listed, and records the real eBay listing ID when one's
        known. Returns False (does nothing) if the item isn't actually
        pending anymore.
        """
        if item["status"] != db.STATUS_PENDING_EBAY_UPLOAD:
            return False

        if ebay_item_id:
            db.set_ebay_item_id(item["id"], ebay_item_id)

        listing = db.get_ebay_listing_data(item["id"])
        if listing:
            # Only a REAL confirmed listing bumps this - not just being
            # picked during Queue Review - so the category select can trust
            # it as "this category actually works on eBay" and sort/label
            # it accordingly. This is the only such signal available
            # without eBay API access to verify categories directly.
            db.record_ebay_category_confirmed(listing["category_id"])

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
        extra = f"✅ Confirmed live on eBay from batch upload (item ID: {ebay_item_id})." if ebay_item_id \
            else "✅ Confirmed live on eBay from batch upload."
        msg = await send_item_card(listed_channel, item, view=view, extra_text=extra)
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
        admin_role = runtime_settings.resolve_role(interaction.guild, config.ROLE_ADMIN)
        target_role = runtime_settings.resolve_role(interaction.guild, role_name)
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
