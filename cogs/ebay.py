"""
Admin commands for the eBay CSV batch fallback (see ebay_csv.py) - the path
used while there's no live eBay API integration:

/ebay export-batch  - hands the accumulated CSV to whoever manages eBay as a
                       Discord attachment, archives + clears it so the next
                       "Add to eBay Batch" click starts fresh, and records a
                       durable, numbered ebay_batches snapshot of exactly
                       which items went out in it (see database.
                       create_ebay_batch) - items.ebay_batch_id points back
                       to it.
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

All of these require the Pallet Admin role, same as admin_tools.py. The
actual message/status moving is delegated to the ItemFlow cog (via
get_cog, same cross-cog pattern QueueReviewView/AwaitingListingView use) so
that logic stays in one place alongside the rest of the pipeline.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import ebay_csv
import ebay_results
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


class Ebay(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    ebay_group = app_commands.Group(name="ebay", description="Admin eBay CSV batch management")

    @ebay_group.command(name="export-batch", description="Download the accumulated eBay CSV batch and start a fresh one.")
    async def export_batch(self, interaction: discord.Interaction):
        if not await _require_admin(interaction):
            return

        if not config.EBAY_ITEM_LOCATION:
            await interaction.response.send_message(
                "⚠️ `EBAY_ITEM_LOCATION` isn't set in `.env` - eBay rejects **every** row in a batch "
                "without it (`No <Item.Location> exists`). Set it to your ship-from city/state or ZIP "
                "(e.g. `Columbus, OH`), restart the bot, then try exporting again.",
                ephemeral=True,
            )
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
        pic_url_note = (
            "PicURL is filled in from R2-hosted photo URLs where available."
            if config.R2_ENABLED else
            "PicURL is blank (R2 photo hosting isn't configured), so add photos in Seller Hub "
            "or fill PicURL in yourself before uploading."
        )
        await interaction.response.send_message(
            f"📄 eBay batch **#{batch_id}** CSV attached ({len(pending_items)} item(s)). Upload it in Seller "
            f"Hub's bulk upload / File Exchange tool - {pic_url_note} The live batch has been cleared - the "
            "next **Add to eBay Batch** click starts a new one. Once eBay processes it, either run "
            f"`/ebay import-results batch_id:{batch_id}` with the results CSV Seller Hub gives you, or "
            f"`/ebay confirm-listed` by hand after checking Seller Hub, to move these out of {pending_channel_mention}.",
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

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
        name="retry-item",
        description="Re-queue a failed batch item, optionally correcting its category. Run in its pallet's category.",
    )
    @app_commands.describe(
        item_number="The item's number shown on its card (e.g. 3)",
        category_id="Optional - a corrected eBay leaf category ID, if the failure was 'not a leaf category' (error 87)",
    )
    async def retry_item(self, interaction: discord.Interaction, item_number: int, category_id: str = None):
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

        category_note = ""
        if category_id:
            listing["category_id"] = category_id.strip()
            db.save_ebay_listing_data(
                item["id"], listing["ebay_title"], listing["category_id"], listing["condition_id"],
                listing["price"], listing["item_specifics"], listing_format=listing["listing_format"],
                auction_duration=listing["auction_duration"], actor_id=interaction.user.id,
            )
            category_note = f" with category corrected to `{listing['category_id']}`"

        db.clear_ebay_batch_id(item["id"])
        ebay_csv.append_item_to_batch(item, listing)
        await interaction.response.send_message(
            f"🔁 Re-queued item #{item_number} into the current (live) eBay CSV batch{category_note}, "
            f"picking up any config changes since its last export (e.g. `EBAY_ITEM_LOCATION`) - it'll "
            f"go out in the next `/ebay export-batch`.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Ebay(bot))
