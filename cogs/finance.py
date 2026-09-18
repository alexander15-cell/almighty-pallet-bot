"""
Purchase Management and Finance Management commands.

/setprice - Purchase Management, Finance Management, or Pallet Admin can set
            a pallet's total cost. Usable more than once (later calls just
            overwrite it), so Purchase can also use it to correct a mistake
            without needing Finance's help for that specific case.

/finance record-sale       - Finance Management or Admin. Records (or
                              corrects) an item's actual sale price and
                              platform. Deliberately independent of the
                              operational "Mark as Sold" button, so
                              Listing Management never has to stop and
                              type a price - Finance does this at their
                              own pace, whenever the numbers are known.
/finance override-count    - Finance Management or Admin. Manually sets
                              the pallet's "items received" figure, for
                              when the physical/accounting count needs to
                              differ from the number of Data Entry
                              submissions (e.g. junk that was never logged).
/finance clear-count-override - reverts to the automatic, live count.
/finance summary            - posts a fresh (non-pinned) copy of the same
                              numbers shown on the pinned card, for a
                              record in chat or to check a pallet from
                              somewhere else.

Every command that changes a number refreshes that pallet's pinned live
status card in #pallet-discussion via finance_utils.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import finance_utils


def _has_role(interaction: discord.Interaction, role_name: str) -> bool:
    role = discord.utils.get(interaction.guild.roles, name=role_name)
    return bool(role and role in interaction.user.roles)


def _is_admin(interaction: discord.Interaction) -> bool:
    return _has_role(interaction, config.ROLE_ADMIN)


async def _require_any_role(interaction: discord.Interaction, role_names: list[str]) -> bool:
    if _is_admin(interaction) or any(_has_role(interaction, r) for r in role_names):
        return True
    names = " or ".join(f"**{r}**" for r in role_names)
    await interaction.response.send_message(f"You need {names} to do that.", ephemeral=True)
    return False


def _get_pallet_or_none(interaction: discord.Interaction):
    return db.get_pallet_by_category(interaction.channel.category_id) if interaction.channel.category_id else None


class Finance(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    finance_group = app_commands.Group(name="finance", description="Finance Management commands")

    @app_commands.command(name="setprice", description="Set this pallet's total cost. Run inside that pallet's category.")
    @app_commands.describe(cost="Total amount paid for the pallet")
    async def setprice(self, interaction: discord.Interaction, cost: float):
        if not await _require_any_role(interaction, [config.ROLE_PURCHASE_MGMT, config.ROLE_FINANCE_MGMT]):
            return
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels (e.g. #pallet-discussion), not somewhere else.",
                ephemeral=True,
            )
            return
        if cost < 0:
            await interaction.response.send_message("Cost can't be negative.", ephemeral=True)
            return

        db.set_pallet_cost(pallet["id"], cost, actor_id=interaction.user.id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.response.send_message(
            f"💵 Set **{pallet['name']}** cost to ${cost:.2f}. Live card updated.", ephemeral=True
        )

    @finance_group.command(name="record-sale", description="Record (or correct) an item's actual sale price. Run inside that pallet's category.")
    @app_commands.describe(
        item_number="The item's number shown on its card (e.g. 3)",
        price="Actual sale price",
        platform="Where it sold (eBay, Facebook Marketplace, Website, Other)",
    )
    async def record_sale(self, interaction: discord.Interaction, item_number: int, price: float, platform: str):
        if not await _require_any_role(interaction, [config.ROLE_FINANCE_MGMT]):
            return
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return
        item = db.get_item_by_pallet_and_number(pallet["id"], item_number)
        if not item:
            await interaction.response.send_message(f"No item #{item_number} found in **{pallet['name']}**.", ephemeral=True)
            return
        if price < 0:
            await interaction.response.send_message("Price can't be negative.", ephemeral=True)
            return

        was_already_priced = item["sale_price"] is not None
        db.record_item_sale(item["id"], price, platform.strip(), actor_id=interaction.user.id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])

        verb = "Corrected" if was_already_priced else "Recorded"
        await interaction.response.send_message(
            f"💰 {verb} sale price for **{pallet['name']}** item #{item_number}: "
            f"${price:.2f} on {platform.strip()}. Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(name="override-count", description="Manually set the pallet's 'items received' count. Run inside that pallet's category.")
    @app_commands.describe(count="The correct number of items received for this pallet")
    async def override_count(self, interaction: discord.Interaction, count: int):
        if not await _require_any_role(interaction, [config.ROLE_FINANCE_MGMT]):
            return
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return
        if count < 0:
            await interaction.response.send_message("Count can't be negative.", ephemeral=True)
            return

        db.set_items_received_override(pallet["id"], count)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.response.send_message(
            f"📦 **{pallet['name']}** items received manually set to {count}. Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(name="clear-count-override", description="Revert to the automatic (live) items-received count. Run inside that pallet's category.")
    async def clear_count_override(self, interaction: discord.Interaction):
        if not await _require_any_role(interaction, [config.ROLE_FINANCE_MGMT]):
            return
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return

        db.clear_items_received_override(pallet["id"])
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.response.send_message(
            f"📦 **{pallet['name']}** items received count reverted to automatic. Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(name="summary", description="Post a fresh copy of this pallet's financial/status numbers. Run inside that pallet's category.")
    async def summary(self, interaction: discord.Interaction):
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return
        fin = db.get_pallet_financials(pallet["id"])
        embed = finance_utils.build_finance_embed(pallet, fin)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Finance(bot))
