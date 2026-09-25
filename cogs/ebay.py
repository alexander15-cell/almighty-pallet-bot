"""
Admin commands for the eBay CSV batch fallback (see ebay_csv.py) - the path
used while there's no live eBay API integration:

/ebay export-batch  - hands the accumulated CSV to whoever manages eBay as a
                       Discord attachment, archives + clears it so the next
                       "Add to eBay Batch" click starts fresh, and records a
                       durable, numbered ebay_batches snapshot of exactly
                       which items went out in it (see database.
                       create_ebay_batch) - items.ebay_batch_id points back
                       to it. This is eBay's classic File Exchange "Add"
                       template (see ebay_csv.py) - ready to upload as-is,
                       built entirely from data Queue Review already
                       captured.
/ebay fill-recommendations - a separate, optional path: upload eBay's own
                       AI-prefill tool's returned recommendations file (see
                       ebay_recommendations.py) and get back the same file
                       with price/quantity/condition/format filled in from
                       what Queue Review already captured. Not part of the
                       normal export-batch flow above - only useful if
                       someone chooses to use eBay's separate Prefill
                       Listing tool by hand instead.
/ebay batches        - lists recent batches with how many items in each are
                       still waiting on a result.
/ebay batch          - shows one batch's still-pending items.
/ebay import-results - reconciles a batch against a results CSV downloaded
                       from Seller Hub (see ebay_results.py): rows it can
                       confidently match as succeeded move that item to
                       Listed and record the real eBay item ID; anything
                       else (no listing ID, an explicit error, an
                       unrecognized SKU) is reported back unresolved rather
                       than guessed at.
/ebay confirm-listed - the fully-manual fallback: once a CSV has actually
                       been uploaded and processed by eBay, moves the
                       item(s) that were in it from pending_ebay_upload to
                       Listed by hand, for anyone who'd rather just check
                       Seller Hub directly than download/upload a results
                       CSV.
/ebay category-search - looks up real eBay leaf category IDs by keyword
                       against eBay's own official taxonomy (see
                       ebay_taxonomy.py) - mainly for finding an ID to pass
                       to /ebay retry-item's category_id correction.

All of these require the Pallet Admin role, same as admin_tools.py. The
actual message/status moving is delegated to the ItemFlow cog (via
get_cog, same cross-cog pattern QueueReviewView/AwaitingListingView use) so
that logic stays in one place alongside the rest of the pipeline.
"""
import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import ebay_csv
import ebay_recommendations
import ebay_results
import ebay_taxonomy
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


def _parse_specifics(text: str) -> dict:
    """
    Parses "Key=Value, Key2=Value2" (as typed into /ebay retry-item's
    specifics option) into a dict. Deliberately simple (splits on commas,
    then the first "=" in each piece) - doesn't support a value containing
    a literal comma, which is an acceptable limit for a quick one-off fix,
    not a replacement for the full Queue Review specifics field.
    """
    result = {}
    for piece in text.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" not in piece:
            raise ValueError(f"{piece!r} isn't in 'Key=Value' form")
        key, value = piece.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            raise ValueError(f"{piece!r} has no key before '='")
        result[key] = value
    if not result:
        raise ValueError("no 'Key=Value' pairs found")
    return result


def _parse_dimensions(text: str) -> tuple:
    """
    Parses "L x W x H" (as typed into /ebay retry-item's dimensions option)
    into three positive inch values, or raises ValueError with the exact
    message to show. Same "L x W x H" format/separator as EbayListingModal
    (item_flow.py's Queue Review approval step) so a reviewer only ever has
    to remember one format.
    """
    parts = [p.strip() for p in re.split(r"[xX×]", text) if p.strip()]
    if len(parts) != 3:
        raise ValueError("must be three numbers separated by 'x', e.g. `12 x 8 x 4`")
    try:
        length_in, width_in, height_in = (float(p) for p in parts)
    except ValueError:
        raise ValueError("must all be numbers, e.g. `12 x 8 x 4`")
    if any(d <= 0 for d in (length_in, width_in, height_in)):
        raise ValueError("must all be numbers greater than 0, e.g. `12 x 8 x 4`")
    return length_in, width_in, height_in


class Ebay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ebay_group = app_commands.Group(name="ebay", description="Admin eBay CSV batch management")

    @ebay_group.command(name="export-batch", description="Download the accumulated eBay CSV batch and start a fresh one.")
    async def export_batch(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        pending_items = db.get_unbatched_pending_items()

        path = ebay_csv.export_and_archive()
        if path is None:
            await interaction.response.send_message("The eBay batch is empty - nothing to export.", ephemeral=True)
            return

        batch_id = db.create_ebay_batch(
            csv_filename=path.name, exported_by=interaction.user.id, item_ids=[i["id"] for i in pending_items],
        )

        pending_channel_id = db.get_shared_channel_id("pending-ebay-upload")
        pending_channel_mention = f"<#{pending_channel_id}>" if pending_channel_id else "#pending-ebay-upload"
        photo_url_note = (
            "PicURL is filled in from R2-hosted photo URLs where available."
            if config.R2_ENABLED else
            "PicURL is blank (R2 photo hosting isn't configured), so add photos yourself "
            "before or after uploading."
        )
        await interaction.response.send_message(
            f"📄 eBay batch **#{batch_id}** CSV attached ({len(pending_items)} item(s)). This is eBay's classic "
            f"File Exchange \"Add\" template, ready to upload as-is via the Upload tab in Seller Hub - review it "
            f"first (especially category, since that's what most often needs a manual correction), then upload. "
            f"{photo_url_note} The live batch has been cleared - the next **Add to eBay Batch** click starts a "
            f"new one. Once eBay processes it, either run `/ebay import-results batch_id:{batch_id}` with the "
            f"results CSV Seller Hub gives you, or `/ebay confirm-listed` by hand after checking Seller Hub, to "
            f"move these out of {pending_channel_mention}.",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

    @ebay_group.command(
        name="fill-recommendations",
        description="Upload eBay's returned recommendations file - fills in price/condition/format from Queue Review.",
    )
    @app_commands.describe(recommendations_file="The .xlsm file Seller Hub gave back after processing your batch upload")
    async def fill_recommendations(self, interaction: discord.Interaction, recommendations_file: discord.Attachment):
        if not await _require_admin(interaction):
            return
        if not recommendations_file.filename.lower().endswith((".xlsm", ".xlsx")):
            await interaction.response.send_message(
                "That doesn't look like an Excel file (.xlsm/.xlsx) - upload the file eBay gave you back "
                "after processing your batch, not the one you uploaded to it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        raw_bytes = await recommendations_file.read()

        try:
            filled_path, summary = await asyncio.to_thread(ebay_recommendations.fill_recommendations_file, raw_bytes)
        except ebay_recommendations.RecommendationsFileError as e:
            await interaction.followup.send(f"⚠️ Couldn't process that file: {e}", ephemeral=True)
            return

        try:
            lines = [
                f"✅ Filled in {summary.filled_count} item(s) across {summary.sheet_count} category sheet(s) "
                f"with price/quantity/condition/format from Queue Review - never overwrote anything already there.",
            ]
            if summary.unmatched_skus:
                shown = ", ".join(summary.unmatched_skus[:10])
                lines.append(
                    f"⚠️ {len(summary.unmatched_skus)} row(s) had a SKU this bot doesn't recognize "
                    f"(not one of this bot's items, or already deleted): {shown}"
                )
            if not config.EBAY_ITEM_LOCATION:
                lines.append("ℹ️ `EBAY_ITEM_LOCATION` isn't set in `.env` - Location was left blank, fill it in by hand.")
            if not config.EBAY_SHIPPING_SERVICE:
                lines.append(
                    "ℹ️ `EBAY_SHIPPING_SERVICE` isn't set in `.env` - Shipping service was left blank, fill it in by hand."
                )
            lines.append(
                "Review before re-uploading to Seller Hub - dropdown pick-lists in the file may not survive "
                "re-saving through this tool, but the underlying data is unaffected; just type a value directly "
                "if a cell looks off."
            )
            await interaction.followup.send(
                "\n".join(lines),
                file=discord.File(filled_path, filename=recommendations_file.filename),
                ephemeral=True,
            )
        finally:
            filled_path.unlink(missing_ok=True)

    @ebay_group.command(name="batches", description="List recent eBay CSV batches and how many items in each are still pending.")
    async def batches(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        batches = db.list_ebay_batches()
        if not batches:
            await interaction.response.send_message("No eBay batches have been exported yet.", ephemeral=True)
            return

        lines = []
        for b in batches:
            still_pending = len(db.get_ebay_batch_items(b["id"]))
            lines.append(f"**#{b['id']}** - `{b['csv_filename']}` - {still_pending}/{b['item_count']} still pending")
        embed = discord.Embed(title="eBay Batches", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ebay_group.command(name="batch", description="Show one eBay batch's still-pending items.")
    @app_commands.describe(batch_id="The batch number shown by /ebay batches or /ebay export-batch")
    async def batch(self, interaction: discord.Interaction, batch_id: int):
        if not await _require_admin(interaction):
            return

        b = db.get_ebay_batch(batch_id)
        if not b:
            await interaction.response.send_message(f"No batch #{batch_id} found.", ephemeral=True)
            return

        pending_items = db.get_ebay_batch_items(batch_id)
        if not pending_items:
            await interaction.response.send_message(
                f"Batch #{batch_id} (`{b['csv_filename']}`, {b['item_count']} item(s)) - none still pending, "
                "everything from it has already been confirmed or moved on.",
                ephemeral=True,
            )
            return

        lines = [f"#{item['item_number']} (pallet {item['pallet_id']})" for item in pending_items[:25]]
        await interaction.response.send_message(
            f"Batch #{batch_id} (`{b['csv_filename']}`) - {len(pending_items)}/{b['item_count']} still pending: "
            + ", ".join(lines),
            ephemeral=True,
        )

    @ebay_group.command(name="import-results", description="Reconcile a batch against a results CSV downloaded from Seller Hub.")
    @app_commands.describe(
        batch_id="The batch number shown by /ebay batches or /ebay export-batch",
        results_csv="The results/report CSV Seller Hub gives you after processing the upload",
    )
    async def import_results(self, interaction: discord.Interaction, batch_id: int, results_csv: discord.Attachment):
        if not await _require_admin(interaction):
            return

        b = db.get_ebay_batch(batch_id)
        if not b:
            await interaction.response.send_message(f"No batch #{batch_id} found.", ephemeral=True)
            return

        pending_items = db.get_ebay_batch_items(batch_id)
        if not pending_items:
            await interaction.response.send_message(f"Batch #{batch_id} has no items still pending a result.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        csv_bytes = await results_csv.read()
        parsed = ebay_results.parse_results(csv_bytes)

        by_label = {ebay_csv.custom_label(item): item for item in pending_items}
        cog = self.bot.get_cog("ItemFlow")

        confirmed, confirmed_pallet_ids, unmatched_success, unmatched_failed = [], set(), [], []
        for row in parsed["succeeded"]:
            item = by_label.get(row["custom_label"])
            if not item:
                unmatched_success.append(row["custom_label"])
                continue
            if await cog.confirm_ebay_pending_item(item, actor_id=interaction.user.id, ebay_item_id=row["ebay_item_id"]):
                confirmed.append(f"#{item['item_number']} (pallet {item['pallet_id']})")
                confirmed_pallet_ids.add(item["pallet_id"])

        for row in parsed["failed"]:
            if row["custom_label"] not in by_label:
                unmatched_failed.append(row["custom_label"])

        for pallet_id in confirmed_pallet_ids:
            await finance_utils.refresh_finance_message(self.bot, pallet_id)

        lines = [f"✅ Confirmed {len(confirmed)} item(s) live: {', '.join(confirmed) or 'none'}."]
        if parsed["failed"]:
            failed_labels = ", ".join(r["custom_label"] for r in parsed["failed"][:10])
            lines.append(f"❌ {len(parsed['failed'])} row(s) reported failed/no listing ID: {failed_labels}")
        if unmatched_success or unmatched_failed:
            lines.append(
                f"⚠️ {len(unmatched_success) + len(unmatched_failed)} row(s) had a SKU not found in this "
                f"batch's still-pending items - check they belong to batch #{batch_id} and weren't already resolved."
            )
        if parsed["unparsed_rows"]:
            lines.append(f"⚠️ {parsed['unparsed_rows']} row(s) had no readable SKU column and were skipped.")
        still_pending_after = len(db.get_ebay_batch_items(batch_id))
        if still_pending_after:
            lines.append(f"{still_pending_after} item(s) in this batch are still unresolved - use `/ebay batch batch_id:{batch_id}` to see them.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

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

    @ebay_group.command(
        name="category-search",
        description="Search eBay's real category list by keyword - IDs for /ebay retry-item's category_id.",
    )
    @app_commands.describe(query="A few words describing the item type, e.g. 'cordless drill'")
    async def category_search(self, interaction: discord.Interaction, query: str):
        if not await _require_admin(interaction):
            return
        matches = ebay_taxonomy.search(query, limit=10)
        if not matches:
            await interaction.response.send_message(f"No eBay categories matched `{query}`.", ephemeral=True)
            return
        lines = "\n".join(f"`{category_id}` - {path}" for category_id, path in matches)
        await interaction.response.send_message(f"Categories matching `{query}`:\n{lines}", ephemeral=True)

    @ebay_group.command(
        name="retry-item",
        description="Re-queue a failed batch item, fixing whatever caused the failure. Run in its pallet's category.",
    )
    @app_commands.describe(
        item_number="The item's number shown on its card (e.g. 3)",
        category_id="Optional - a corrected eBay leaf category ID (see /ebay category-search)",
        condition_id="Optional - a corrected condition ID (error: 'condition id is invalid for the selected category')",
        specifics="Optional - item specifics to add/fix, as 'Key=Value, Key2=Value2' - merged into what's already saved",
        weight_lb="Optional - a corrected weight in pounds, e.g. 2.5",
        dimensions="Optional - corrected dimensions as 'L x W x H' in inches, e.g. '12 x 8 x 4'",
    )
    async def retry_item(
        self, interaction: discord.Interaction, item_number: int,
        category_id: str = None, condition_id: str = None, specifics: str = None,
        weight_lb: float = None, dimensions: str = None,
    ):
        if not await _require_admin(interaction):
            return

        pallet = db.get_pallet_by_category(interaction.channel.category_id) if interaction.channel.category_id else None
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return

        item = db.get_item_by_pallet_and_number(pallet["id"], item_number)
        if not item:
            await interaction.response.send_message(f"No item #{item_number} found in **{pallet['name']}**.", ephemeral=True)
            return
        if item["status"] != db.STATUS_PENDING_EBAY_UPLOAD:
            await interaction.response.send_message(
                f"Item #{item_number} isn't sitting in a pending-upload state (current status: "
                f"{item['status']}) - there's nothing to retry.",
                ephemeral=True,
            )
            return

        listing = db.get_ebay_listing_data(item["id"])
        if not listing:
            await interaction.response.send_message(
                "No eBay listing data found for this item - can't rebuild its row.", ephemeral=True
            )
            return

        corrections = []
        if category_id:
            listing["category_id"] = category_id.strip()
            corrections.append(f"category → `{listing['category_id']}`")
        if condition_id:
            listing["condition_id"] = condition_id.strip()
            corrections.append(f"condition → `{listing['condition_id']}`")
        if specifics:
            try:
                added = _parse_specifics(specifics)
            except ValueError as e:
                await interaction.response.send_message(f"Couldn't parse `specifics`: {e}", ephemeral=True)
                return
            listing["item_specifics"].update(added)
            corrections.append(f"specifics +{', '.join(added)}")
        if weight_lb is not None:
            if weight_lb <= 0:
                await interaction.response.send_message("`weight_lb` must be greater than 0.", ephemeral=True)
                return
            listing["weight_lb"] = weight_lb
            corrections.append(f"weight → `{weight_lb:g} lb`")
        if dimensions:
            try:
                length_in, width_in, height_in = _parse_dimensions(dimensions)
            except ValueError as e:
                await interaction.response.send_message(f"`dimensions` {e}.", ephemeral=True)
                return
            listing["length_in"], listing["width_in"], listing["height_in"] = length_in, width_in, height_in
            corrections.append(f"dimensions → `{length_in:g}x{width_in:g}x{height_in:g} in`")

        if corrections:
            # Always thread weight/dims through, whether or not THIS retry
            # touched them - save_ebay_listing_data is a full upsert, so
            # omitting them here would silently null out a previously-saved
            # weight/dimensions on every category/condition/specifics-only
            # correction, not just leave them unchanged.
            db.save_ebay_listing_data(
                item["id"], listing["ebay_title"], listing["category_id"], listing["condition_id"],
                listing["price"], listing["item_specifics"], listing_format=listing["listing_format"],
                auction_duration=listing["auction_duration"], weight_lb=listing.get("weight_lb"),
                length_in=listing.get("length_in"), width_in=listing.get("width_in"),
                height_in=listing.get("height_in"), actor_id=interaction.user.id,
            )

        db.clear_ebay_batch_id(item["id"])
        ebay_csv.append_item_to_batch(item, listing)
        correction_note = f" ({'; '.join(corrections)})" if corrections else ""
        await interaction.response.send_message(
            f"🔁 Re-queued item #{item_number} into the current (live) eBay CSV batch{correction_note} - "
            f"it'll go out in the next `/ebay export-batch`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Ebay(bot))
