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
/finance set-shipping-info  - Finance Management or Admin. Records a buyer's
                              recipient name/address for a non-eBay ("Other"
                              platform) sale, feeding /pirate-ship
                              export-batch. Deliberately independent of
                              record-sale too, since the address often isn't
                              known until after the price is agreed on.
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


class ShippingAddressModal(discord.ui.Modal, title="Shipping Address (1/2)"):
    """
    First of two modals capturing a structured (not freeform) address for a
    non-eBay sale, feeding /pirate-ship export-batch - split in two because
    Discord modals cap out at 5 text inputs, and a real address needs 7
    distinct fields (recipient, 2 address lines, city, state, postal code,
    country). Submitting shows a button that opens ShippingPostalModal for
    the rest, the same "chain through an intermediate interaction" pattern
    item_flow.py uses for its own multi-step forms.
    """

    def __init__(self, item_number: int, item_id: int):
        super().__init__()
        self.item_number = item_number
        self.item_id = item_id
        item = db.get_item(item_id)
        self.recipient_name = discord.ui.TextInput(
            label="Recipient Name", default=item.get("recipient_name") or "", max_length=100,
        )
        self.address_line1 = discord.ui.TextInput(
            label="Address Line 1", default=item.get("address_line1") or "", max_length=200,
        )
        self.address_line2 = discord.ui.TextInput(
            label="Address Line 2 (apt/suite, optional)", required=False,
            default=item.get("address_line2") or "", max_length=200,
        )
        self.city = discord.ui.TextInput(
            label="City", default=item.get("city") or "", max_length=100,
        )
        self.state = discord.ui.TextInput(
            label="State / Region", default=item.get("state") or "", max_length=100,
        )
        for field in (self.recipient_name, self.address_line1, self.address_line2, self.city, self.state):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction):
        view = discord.ui.View(timeout=300)
        continue_button = discord.ui.Button(label="Continue: postal code & country", style=discord.ButtonStyle.primary)

        async def _continue(inner_interaction: discord.Interaction):
            await inner_interaction.response.send_modal(
                ShippingPostalModal(
                    self.item_number, self.item_id,
                    recipient_name=self.recipient_name.value.strip(),
                    address_line1=self.address_line1.value.strip(),
                    address_line2=self.address_line2.value.strip(),
                    city=self.city.value.strip(),
                    state=self.state.value.strip(),
                )
            )

        continue_button.callback = _continue
        view.add_item(continue_button)
        await interaction.response.send_message(
            "Recipient, address, and city/state saved for this step - tap below to add the "
            "postal code and country and finish.",
            view=view, ephemeral=True,
        )


class ShippingPostalModal(discord.ui.Modal, title="Shipping Address (2/2)"):
    """Second step - see ShippingAddressModal. Only reachable via its
    "Continue" button, which carries the first modal's values forward."""

    def __init__(self, item_number: int, item_id: int, recipient_name: str, address_line1: str,
                 address_line2: str, city: str, state: str):
        super().__init__()
        self.item_number = item_number
        self.item_id = item_id
        self.recipient_name = recipient_name
        self.address_line1 = address_line1
        self.address_line2 = address_line2
        self.city = city
        self.state = state
        item = db.get_item(item_id)
        self.postal_code = discord.ui.TextInput(
            label="Postal / ZIP Code", default=item.get("postal_code") or "", max_length=20,
        )
        self.country = discord.ui.TextInput(
            label="Country", default=item.get("country") or "US", max_length=60,
        )
        self.add_item(self.postal_code)
        self.add_item(self.country)

    async def on_submit(self, interaction: discord.Interaction):
        db.set_shipping_info(
            self.item_id,
            recipient_name=self.recipient_name,
            address_line1=self.address_line1,
            address_line2=self.address_line2,
            city=self.city,
            state=self.state,
            postal_code=self.postal_code.value.strip(),
            country=self.country.value.strip() or "US",
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"📦 Shipping address saved for item #{self.item_number}. It'll go out in the next "
            f"`/pirate-ship export-batch`.",
            ephemeral=True,
        )


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

    @finance_group.command(
        name="set-shipping-info",
        description="Record a buyer's shipping address for a non-eBay sale. Run inside that pallet's category.",
    )
    @app_commands.describe(item_number="The item's number shown on its card (e.g. 3)")
    async def set_shipping_info(self, interaction: discord.Interaction, item_number: int):
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

        await interaction.response.send_modal(ShippingAddressModal(item_number, item["id"]))

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
