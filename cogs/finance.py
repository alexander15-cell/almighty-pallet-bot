"""
Purchase Management and Finance Management commands.

/finance setprice - Purchase Management, Finance Management, or Pallet Admin
            can set a pallet's total cost. Usable more than once (later calls
            just overwrite it), so Purchase can also use it to correct a
            mistake without needing Finance's help for that specific case.

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
/finance refund            - Finance Management or Admin. Logs a refund
                              against an item's sale. Doesn't touch the
                              original sale_price (that sale still
                              happened) - refunds net out separately
                              against revenue everywhere they're shown.
/finance expense           - Finance Management or Admin. Logs a cost
                              against a pallet (packaging, listing fees,
                              etc.), optionally tied to one item.
/finance reverse-sale       - Finance Management or Admin. Undoes an item's
                              recorded sale (e.g. a duplicate entry, a sale
                              that fell through) - clears sale_price back
                              to empty but keeps a record of what was
                              reversed and why.
/finance history            - Lists a pallet's recent refunds/expenses/
                              reversals.
/finance summary            - posts a fresh (non-pinned) copy of the same
                              numbers shown on the pinned card, for a
                              record in chat or to check a pallet from
                              somewhere else.

Every command that changes a number refreshes that pallet's pinned live
status card in #pallet-discussion via finance_utils.
"""
import logging
import secrets
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database as db
import discord_resilience
import finance_utils
import pirate_ship_import
import quickbooks
import runtime_settings

log = logging.getLogger(__name__)


def _has_role(interaction: discord.Interaction, role_name: str) -> bool:
    role = runtime_settings.resolve_role(interaction.guild, role_name)
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


class QuickBooksRedirectModal(discord.ui.Modal, title="Connect QuickBooks"):
    """
    Where the /finance connect-quickbooks flow actually finishes - this bot
    has no web server to receive QuickBooks' OAuth redirect, so instead of
    a callback, whoever's connecting copies the full address bar URL after
    approving access (the page itself failing to load is expected - only
    the URL matters) and pastes it here.
    """

    redirect_url = discord.ui.TextInput(
        label="Full redirect URL",
        placeholder="http://localhost:8000/callback?code=...&realmId=...",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, state: str):
        super().__init__()
        self.state = state

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            parsed = quickbooks.parse_redirect_url(self.redirect_url.value, expected_state=self.state)
            connection = await quickbooks.exchange_code_for_tokens(
                parsed["code"], parsed["realm_id"], actor_id=interaction.user.id
            )
        except quickbooks.QuickBooksError as e:
            await interaction.followup.send(f"❌ Couldn't connect: {e}", ephemeral=True)
            return

        note = ""
        if not config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID:
            note = (
                "\n\n⚠️ `QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID` isn't set yet, so the credit-card "
                "poller won't watch anything until it is - set it to the account you want "
                "watched (Accounting → Chart of Accounts → open the card → the `id=` in the "
                "URL) in `.env` and restart the bot."
            )
        await interaction.followup.send(
            f"✅ Connected to QuickBooks (company/realm `{connection['realm_id']}`, "
            f"**{config.QUICKBOOKS_ENVIRONMENT}** environment)." + note,
            ephemeral=True,
        )


class QuickBooksConnectView(discord.ui.View):
    """Holds the button that opens QuickBooksRedirectModal - a plain link
    can't open a modal on its own, so this is the bridge between "click the
    authorization URL" and "paste the result back in"."""

    def __init__(self, state: str):
        super().__init__(timeout=600)
        self.state = state

    @discord.ui.button(label="Paste redirect URL", style=discord.ButtonStyle.primary, emoji="🔗")
    async def paste_url(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QuickBooksRedirectModal(self.state))


async def _mark_charge_message_handled(message: discord.Message, note: str):
    """Edits a posted #credit-card-charges card to show how it was resolved
    and removes its Allocate button, so it's obvious at a glance which
    charges still need attention."""
    try:
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.add_field(name="Status", value=note, inline=False)
        await message.edit(embed=embed, view=None)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        log.warning("Couldn't update charge message %s after allocation", message.id)


async def _post_awaiting_charge_card(bot: discord.Client, charge: dict):
    """Posts a charge's standing card in #awaiting-pallet-charges once it's
    been filed there (no matching pallet exists yet) - stays up until a new
    pallet's creation flow claims it (see pallet_setup.py)."""
    channel_id = db.get_shared_channel_id("awaiting-pallet-charges")
    if not channel_id:
        log.warning("No #awaiting-pallet-charges channel configured - run /setup shared-channels.")
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return
    embed = discord.Embed(
        title="📥 Awaiting a pallet",
        description=f"**{charge['merchant']}**\n${charge['amount']:.2f} on {charge['txn_date']}",
        color=discord.Color.dark_gold(),
    )
    embed.set_footer(text=f"QuickBooks transaction {charge['quickbooks_txn_id']} - claimed automatically when a matching pallet is created")
    message = await channel.send(embed=embed)
    db.set_awaiting_pallet_charge_message(charge["id"], message.id)


async def _handle_allocation_choice(interaction: discord.Interaction, txn_id: str, amount: float,
                                     merchant: str, txn_date: str, original_message: discord.Message,
                                     choice: str):
    """
    The actual allocation logic behind PalletAllocateSelect - pulled out of
    the discord.ui.Select subclass so it can be exercised directly in tests
    without fighting discord.py's internal Select/Interaction state, since
    this is the money-affecting half of the credit-card workflow (it's the
    only place a pallet_costs row or an awaiting_pallet_charges row actually
    gets created from a QuickBooks charge).
    """
    await interaction.response.defer(ephemeral=True, thinking=True)

    # Guards against a race where two people tap Allocate on the same
    # charge before either finishes - whoever's select lands second backs
    # out instead of double-counting the cost.
    if db.has_quickbooks_txn_been_allocated(txn_id) or db.get_awaiting_pallet_charge_by_txn(txn_id):
        await interaction.followup.send("This charge was already allocated by someone else.", ephemeral=True)
        return

    if choice == "__new_pallet__":
        charge_id = db.create_awaiting_pallet_charge(txn_id, amount, merchant, txn_date, allocated_by=interaction.user.id)
        await _post_awaiting_charge_card(interaction.client, {
            "id": charge_id, "merchant": merchant, "amount": amount,
            "txn_date": txn_date, "quickbooks_txn_id": txn_id,
        })
        await _mark_charge_message_handled(
            original_message, f"📥 Sent to #awaiting-pallet-charges by {interaction.user.mention} (no pallet yet).",
        )
        await interaction.followup.send(
            "Filed under #awaiting-pallet-charges until a matching pallet is created.", ephemeral=True
        )
        return

    pallet_id = int(choice)
    pallet = db.get_pallet(pallet_id)
    if not pallet:
        await interaction.followup.send("That pallet no longer exists.", ephemeral=True)
        return

    db.add_pallet_cost(
        pallet_id=pallet_id, cost_type="credit_card", amount=amount, source="quickbooks",
        description=merchant, quickbooks_txn_id=txn_id, actor_id=interaction.user.id,
    )

    qb_note = ""
    try:
        await quickbooks.create_expense(
            config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID, amount, txn_date,
            memo=f"{pallet['name']} (Pallet #{pallet_id}) - {merchant}",
        )
    except quickbooks.QuickBooksError as e:
        qb_note = f"\n⚠️ Allocated here, but pushing the matching expense back to QuickBooks failed: {e}"

    await finance_utils.refresh_finance_message(interaction.client, pallet_id)
    await _mark_charge_message_handled(original_message, f"✅ Allocated to **{pallet['name']}** by {interaction.user.mention}.")
    await interaction.followup.send(f"💳 Allocated ${amount:.2f} to **{pallet['name']}**.{qb_note}", ephemeral=True)


class PalletAllocateSelect(discord.ui.Select):
    """The actual pallet picker, shown ephemerally after tapping a charge's
    Allocate button. Built fresh on every click (not persisted) since the
    list of active pallets changes over time - only the triggering button
    needs to survive a bot restart, not this follow-up menu."""

    def __init__(self, txn_id: str, amount: float, merchant: str, txn_date: str,
                 original_message: discord.Message, pallets: list):
        options = [
            discord.SelectOption(label=p["name"][:100], value=str(p["id"]))
            for p in pallets
        ]
        options.append(discord.SelectOption(label="🆕 New Pallet (not arrived yet)", value="__new_pallet__"))
        super().__init__(placeholder="Allocate this charge to...", options=options, min_values=1, max_values=1)
        self.txn_id = txn_id
        self.amount = amount
        self.merchant = merchant
        self.txn_date = txn_date
        self.original_message = original_message

    async def callback(self, interaction: discord.Interaction):
        await _handle_allocation_choice(
            interaction, self.txn_id, self.amount, self.merchant, self.txn_date,
            self.original_message, self.values[0],
        )


class PalletAllocateView(discord.ui.View):
    def __init__(self, txn_id: str, amount: float, merchant: str, txn_date: str,
                 original_message: discord.Message, pallets: list):
        super().__init__(timeout=300)
        self.add_item(PalletAllocateSelect(txn_id, amount, merchant, txn_date, original_message, pallets))


class AllocateChargeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"qb_allocate:(?P<txn_id>.+)"):
    """
    The button on every posted #credit-card-charges card. A DynamicItem
    (discord.py 2.4+) rather than a plain View button so it keeps working
    across bot restarts - only the txn_id is encoded in its custom_id, and
    everything else about the charge is re-fetched from QuickBooks fresh on
    click (see quickbooks.get_transaction), so a stale/edited amount is
    never trusted from whatever the message last showed.
    """

    def __init__(self, txn_id: str):
        super().__init__(discord.ui.Button(
            label="Allocate to a pallet", style=discord.ButtonStyle.primary, emoji="🧾",
            custom_id=f"qb_allocate:{txn_id}",
        ))
        self.txn_id = txn_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: dict):
        return cls(match["txn_id"])

    async def callback(self, interaction: discord.Interaction):
        if db.has_quickbooks_txn_been_allocated(self.txn_id) or db.get_awaiting_pallet_charge_by_txn(self.txn_id):
            await interaction.response.send_message("This charge has already been allocated.", ephemeral=True)
            return

        try:
            txn = await quickbooks.get_transaction(self.txn_id)
        except quickbooks.QuickBooksError as e:
            await interaction.response.send_message(f"Couldn't look up this charge in QuickBooks: {e}", ephemeral=True)
            return

        # Discord select menus cap out at 25 options - leave room for "New
        # Pallet" and just show the most recently created active pallets if
        # there happen to be more than that many in flight at once.
        pallets = db.get_all_pallets(include_archived=False)[:24]
        view = PalletAllocateView(self.txn_id, txn["amount"], txn["merchant"], txn["date"], interaction.message, pallets)
        await interaction.response.send_message(
            f"Allocate this ${txn['amount']:.2f} charge from **{txn['merchant']}** to which pallet?",
            view=view, ephemeral=True,
        )


async def _post_credit_card_charge(bot: discord.Client, txn: dict):
    """Posts one newly-seen QuickBooks charge to #credit-card-charges with
    its Allocate button - see the credit_card_poll_loop that calls this."""
    channel_id = db.get_shared_channel_id("credit-card-charges")
    if not channel_id:
        log.warning("QuickBooks charge %s seen but #credit-card-charges isn't set up - run /setup shared-channels.", txn["id"])
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        log.warning("Configured #credit-card-charges channel %s not found.", channel_id)
        return

    embed = discord.Embed(
        title="💳 New credit card charge",
        description=f"**{txn['merchant']}**\n${txn['amount']:.2f} on {txn['date']}",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"QuickBooks transaction {txn['id']}")
    view = discord.ui.View(timeout=None)
    view.add_item(AllocateChargeButton(txn["id"]))
    await channel.send(embed=embed, view=view)


async def _handle_unmatched_shipping_assignment(interaction: discord.Interaction, row_label: str,
                                                  amount: float, pallet_choice: str):
    """The logic behind UnmatchedShippingSelect - pulled out for direct
    testing, same reasoning as _handle_allocation_choice above."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    pallet_id = int(pallet_choice)
    pallet = db.get_pallet(pallet_id)
    if not pallet:
        await interaction.followup.send("That pallet no longer exists.", ephemeral=True)
        return

    db.add_pallet_cost(
        pallet_id=pallet_id, cost_type="shipping", amount=amount, source="pirateship",
        description=f"Pirate Ship shipping (manually assigned) - {row_label}",
        actor_id=interaction.user.id,
    )
    await finance_utils.refresh_finance_message(interaction.client, pallet_id)
    await interaction.followup.send(f"📦 Assigned ${amount:.2f} shipping to **{pallet['name']}**.", ephemeral=True)


class UnmatchedShippingSelect(discord.ui.Select):
    """One picker per unmatched Pirate Ship CSV row (see /finance
    import-pirateship) - lets a human assign a shipping cost that couldn't
    be auto-matched to a specific item's pallet-<id>-item-<number>
    reference. No "New Pallet" option here (unlike the credit-card
    Allocate flow) since a shipping cost only ever exists for an item that
    already sold, which means its pallet already exists."""

    def __init__(self, row_label: str, amount: float, pallets: list):
        options = [discord.SelectOption(label=p["name"][:100], value=str(p["id"])) for p in pallets]
        placeholder = f"Assign ${amount:.2f} ({row_label}) to..."
        super().__init__(placeholder=placeholder[:150], options=options, min_values=1, max_values=1)
        self.row_label = row_label
        self.amount = amount

    async def callback(self, interaction: discord.Interaction):
        await _handle_unmatched_shipping_assignment(interaction, self.row_label, self.amount, self.values[0])


class Finance(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.credit_card_poll_loop.start()

    async def cog_load(self):
        # Registers the (txn_id -> button) template once so every
        # #credit-card-charges card's Allocate button keeps working across
        # bot restarts, not just the ones posted during this process's
        # lifetime - see AllocateChargeButton.
        self.bot.add_dynamic_items(AllocateChargeButton)

    def cog_unload(self):
        self.credit_card_poll_loop.cancel()

    finance_group = app_commands.Group(name="finance", description="Finance Management commands")

    @tasks.loop(minutes=config.QUICKBOOKS_POLL_MINUTES)
    async def credit_card_poll_loop(self):
        """
        Checks QuickBooks for new charges on the configured credit card
        account every QUICKBOOKS_POLL_MINUTES and posts any not already
        seen (database.quickbooks_seen_charges is the source of truth for
        de-duplication, not the query window below, since a backdated/
        edited transaction could otherwise slip past a narrow date filter).
        No-ops entirely until both a company is connected AND
        QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID is configured - watching zero
        accounts is the safe default (see config.py).
        """
        if not (quickbooks.is_connected() and config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID):
            return
        try:
            since = datetime.now(timezone.utc) - timedelta(days=3)
            transactions = await quickbooks.list_recent_transactions(
                config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID, since=since
            )
        except quickbooks.QuickBooksError:
            log.exception("QuickBooks credit-card poll failed")
            return

        for txn in transactions:
            if db.has_seen_quickbooks_txn(txn["id"]):
                continue
            try:
                await _post_credit_card_charge(self.bot, txn)
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                # Marked seen only on a successful post, below - a transient
                # failure here (e.g. a DNS/network blip) should retry on the
                # next poll, not silently drop the charge forever.
                log.exception("Failed to post QuickBooks charge %s - will retry next poll", txn["id"])
                continue
            db.mark_quickbooks_txn_seen(txn["id"])

    @credit_card_poll_loop.before_loop
    async def _before_credit_card_poll_loop(self):
        await self.bot.wait_until_ready()

    @finance_group.command(name="setprice", description="Set this pallet's total cost. Run inside that pallet's category.")
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

    @finance_group.command(name="refund", description="Log a refund against an item's sale. Run inside that pallet's category.")
    @app_commands.describe(
        item_number="The item's number shown on its card (e.g. 3)",
        amount="Amount refunded",
        reason="Why - shown in /finance history",
    )
    async def refund(self, interaction: discord.Interaction, item_number: int, amount: float, reason: str):
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
        if amount <= 0:
            await interaction.response.send_message("Refund amount must be positive.", ephemeral=True)
            return

        db.record_refund(item["id"], amount, reason.strip(), actor_id=interaction.user.id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.response.send_message(
            f"↩️ Logged a ${amount:.2f} refund for **{pallet['name']}** item #{item_number} ({reason.strip()}). "
            f"Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(name="expense", description="Log a cost against this pallet (packaging, fees, etc). Run inside that pallet's category.")
    @app_commands.describe(
        amount="Amount spent",
        reason="What it was for - shown in /finance history",
        item_number="Optional - tie this expense to a specific item's number",
    )
    async def expense(self, interaction: discord.Interaction, amount: float, reason: str, item_number: int = None):
        if not await _require_any_role(interaction, [config.ROLE_FINANCE_MGMT]):
            return
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return
        if amount <= 0:
            await interaction.response.send_message("Expense amount must be positive.", ephemeral=True)
            return

        item_id = None
        if item_number is not None:
            item = db.get_item_by_pallet_and_number(pallet["id"], item_number)
            if not item:
                await interaction.response.send_message(f"No item #{item_number} found in **{pallet['name']}**.", ephemeral=True)
                return
            item_id = item["id"]

        db.record_expense(pallet["id"], amount, reason.strip(), actor_id=interaction.user.id, item_id=item_id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        item_note = f" (item #{item_number})" if item_number is not None else ""
        await interaction.response.send_message(
            f"🧾 Logged a ${amount:.2f} expense for **{pallet['name']}**{item_note}: {reason.strip()}. Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(name="reverse-sale", description="Undo an item's recorded sale (duplicate entry, fell through). Run inside that pallet's category.")
    @app_commands.describe(
        item_number="The item's number shown on its card (e.g. 3)",
        reason="Why - shown in /finance history",
    )
    async def reverse_sale(self, interaction: discord.Interaction, item_number: int, reason: str):
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

        old_price = db.reverse_sale(item["id"], reason.strip(), actor_id=interaction.user.id)
        if old_price is None:
            await interaction.response.send_message(
                f"Item #{item_number} doesn't have a sale price recorded - nothing to reverse.", ephemeral=True
            )
            return
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.response.send_message(
            f"⏪ Reversed **{pallet['name']}** item #{item_number}'s sale (was ${old_price:.2f}, {reason.strip()}). "
            f"Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(name="history", description="List this pallet's recent refunds/expenses/reversals. Run inside that pallet's category.")
    async def history(self, interaction: discord.Interaction):
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return

        transactions = db.get_finance_transactions(pallet["id"])
        if not transactions:
            await interaction.response.send_message(
                f"No refunds, expenses, or reversals recorded for **{pallet['name']}** yet.", ephemeral=True
            )
            return

        type_labels = {"refund": "↩️ Refund", "expense": "🧾 Expense", "reversal": "⏪ Reversal"}
        lines = []
        for tx in transactions:
            item_note = f" (item #{tx['item_number']})" if tx.get("item_number") else ""
            lines.append(f"{type_labels.get(tx['type'], tx['type'])} ${tx['amount']:.2f}{item_note} - {tx['note'] or 'no reason given'}")
        embed = discord.Embed(
            title=f"Finance History - {pallet['name']}",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    @finance_group.command(
        name="refresh-card",
        description="Force-refresh this pallet's pinned status card now, without waiting for an item to move.",
    )
    async def refresh_card(self, interaction: discord.Interaction):
        pallet = _get_pallet_or_none(interaction)
        if not pallet:
            await interaction.response.send_message(
                "Run this inside one of the pallet's own channels, not somewhere else.", ephemeral=True
            )
            return

        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.response.send_message(
            f"🔄 **{pallet['name']}**'s pinned status card in #pallet-discussion refreshed with current numbers.",
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

    @finance_group.command(
        name="pallet-summary",
        description="Full cost-basis breakdown by type, revenue, and margin for a pallet.",
    )
    @app_commands.describe(pallet_name="Pallet name (omit to use the pallet whose channel you're in)")
    async def pallet_summary(self, interaction: discord.Interaction, pallet_name: str = None):
        if pallet_name:
            pallet = db.get_pallet_by_name(pallet_name.strip())
            if not pallet:
                await interaction.response.send_message(f"No pallet named `{pallet_name}` found.", ephemeral=True)
                return
        else:
            pallet = _get_pallet_or_none(interaction)
            if not pallet:
                await interaction.response.send_message(
                    "Run this inside one of the pallet's own channels, or pass a `pallet_name`.", ephemeral=True
                )
                return

        fin = db.get_pallet_financials(pallet["id"])
        breakdown = db.get_pallet_cost_breakdown(pallet["id"])

        embed = discord.Embed(title=f"📊 {pallet['name']} - Cost & Revenue Breakdown", color=discord.Color.gold())

        cost_lines = []
        if fin["manual_pallet_cost"] is not None:
            cost_lines.append(f"Purchase (`/finance setprice`): ${fin['manual_pallet_cost']:.2f}")
        cost_type_labels = {"credit_card": "Credit Card", "shipping": "Shipping", "supplies": "Supplies", "misc": "Misc"}
        for cost_type, label in cost_type_labels.items():
            if breakdown.get(cost_type):
                cost_lines.append(f"{label}: ${breakdown[cost_type]:.2f}")
        embed.add_field(
            name="Cost Basis",
            value=("\n".join(cost_lines) if cost_lines else "Nothing recorded yet")
            + (f"\n**Total: ${fin['cost']:.2f}**" if fin["cost"] is not None else ""),
            inline=False,
        )

        embed.add_field(name="Revenue So Far", value=f"${fin['revenue_so_far']:.2f}", inline=True)
        embed.add_field(
            name="Items Priced",
            value=f"{fin['items_priced']} (avg ${fin['avg_sale_price']:.2f})" if fin["items_priced"] else "0",
            inline=True,
        )
        if fin["items_pending_sale"]:
            embed.add_field(
                name="Pending Sale Value",
                value=f"${fin['pending_sale_value']:.2f} ({fin['items_pending_sale']} priced, not yet sold)",
                inline=True,
            )

        if fin["cost"] is not None:
            margin_pct = (fin["profit_so_far"] / fin["revenue_so_far"] * 100) if fin["revenue_so_far"] else None
            pl_word = "Profit" if fin["profit_so_far"] >= 0 else "Loss"
            margin_note = f" ({margin_pct:.1f}% margin)" if margin_pct is not None else ""
            embed.add_field(name=pl_word, value=f"${fin['profit_so_far']:.2f}{margin_note}", inline=True)
        else:
            embed.add_field(name="Profit/Margin", value="Cost not set yet", inline=True)

        await interaction.response.send_message(embed=embed)

    @finance_group.command(name="overview", description="Business-wide snapshot: QuickBooks balance, month-to-date spend/revenue, pallet counts.")
    async def overview(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        embed = discord.Embed(title="📈 Business Overview", color=discord.Color.blurple())

        if quickbooks.is_connected() and config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID:
            try:
                balance = await quickbooks.get_account_balance(config.QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID)
                embed.add_field(
                    name="QuickBooks Balance", value=f"**{balance['name']}**: ${balance['balance']:.2f}", inline=False
                )
            except quickbooks.QuickBooksError as e:
                embed.add_field(name="QuickBooks Balance", value=f"⚠️ Couldn't fetch: {e}", inline=False)
        else:
            embed.add_field(
                name="QuickBooks Balance",
                value="Not connected - run `/finance connect-quickbooks`" if quickbooks.is_configured()
                else "Not configured",
                inline=False,
            )

        mtd = db.get_month_to_date_financials()
        embed.add_field(name="Month-to-Date Revenue", value=f"${mtd['revenue']:.2f}", inline=True)
        embed.add_field(name="Month-to-Date Spend", value=f"${mtd['spend']:.2f}", inline=True)

        progress = db.get_pallet_progress_counts()
        embed.add_field(
            name="Pallets",
            value=f"{progress['in_progress']} in progress, {progress['sold_out']} fully sold out",
            inline=True,
        )

        await interaction.followup.send(embed=embed)

    @finance_group.command(
        name="connect-quickbooks",
        description="Admin: connect this bot to QuickBooks Online for credit-card/expense tracking.",
    )
    async def connect_quickbooks(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            await interaction.response.send_message("You need **Pallet Admin** to do that.", ephemeral=True)
            return
        if not quickbooks.is_configured():
            await interaction.response.send_message(
                "QuickBooks isn't configured yet - set `QUICKBOOKS_CLIENT_ID` and "
                "`QUICKBOOKS_CLIENT_SECRET` in `.env` first (see config.py's QuickBooks section "
                "for where to get them - a registered app at developer.intuit.com), then restart "
                "the bot and run this again.",
                ephemeral=True,
            )
            return
        if quickbooks.is_connected():
            await interaction.response.send_message(
                "QuickBooks is already connected. Running this again will replace that connection "
                "with whatever company you authorize next - only do this if you actually mean to "
                "switch companies.",
                ephemeral=True,
            )
            # Fall through - still let them reconnect/switch if that's really what they want.

        state = secrets.token_urlsafe(16)
        url = quickbooks.build_authorization_url(state)
        view = QuickBooksConnectView(state)
        message = (
            "**Connect QuickBooks Online**\n\n"
            f"1️⃣ Open this link and sign in / approve access:\n{url}\n\n"
            "2️⃣ After approving, QuickBooks will try to redirect your browser - that page may "
            "fail to load (expected, this bot has no web server listening there), but copy the "
            "**full address bar URL** anyway.\n\n"
            "3️⃣ Tap the button below and paste that whole URL in."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(message, view=view, ephemeral=True)

    @finance_group.command(
        name="import-pirateship",
        description="Admin: import a Pirate Ship shipping export CSV and allocate its costs to pallets.",
    )
    @app_commands.describe(csv_file="Pirate Ship's order/shipping export CSV")
    async def import_pirateship(self, interaction: discord.Interaction, csv_file: discord.Attachment):
        if not _is_admin(interaction):
            await interaction.response.send_message("You need **Pallet Admin** to do that.", ephemeral=True)
            return
        if not csv_file.filename.lower().endswith(".csv"):
            await interaction.response.send_message("That doesn't look like a CSV file.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        csv_bytes = await csv_file.read()
        parsed = pirate_ship_import.parse_shipping_costs(csv_bytes)

        matched_count = 0
        still_unmatched = list(parsed["unmatched"])
        affected_pallets = set()
        for row in parsed["matched"]:
            item = db.get_item_by_pallet_and_number(row["pallet_id"], row["item_number"])
            if not item:
                still_unmatched.append({"row_label": row["row_label"], "amount": row["amount"]})
                continue
            db.add_pallet_cost(
                pallet_id=row["pallet_id"], cost_type="shipping", amount=row["amount"], source="pirateship",
                description=f"Pirate Ship shipping - item #{row['item_number']}", item_id=item["id"],
                actor_id=interaction.user.id,
            )
            affected_pallets.add(row["pallet_id"])
            matched_count += 1

        for pallet_id in affected_pallets:
            await finance_utils.refresh_finance_message(self.bot, pallet_id)

        lines = [f"✅ Allocated shipping cost to {matched_count} item(s) across {len(affected_pallets)} pallet(s)."]

        view = None
        if still_unmatched:
            lines.append(
                f"⚠️ {len(still_unmatched)} row(s) couldn't be matched to an item automatically - "
                f"use the menu(s) below to assign them by hand (or ignore rows that aren't from this business)."
            )
            assignable = [row for row in still_unmatched if row["amount"] is not None]
            active_pallets = db.get_all_pallets(include_archived=False)[:24]
            if assignable and active_pallets:
                view = discord.ui.View(timeout=600)
                # Discord caps a message at 5 component rows - one Select
                # per row, so only the first 5 unmatched rows get a picker
                # here; anything past that needs /finance expense by hand.
                for row in assignable[:5]:
                    view.add_item(UnmatchedShippingSelect(row["row_label"][:60], row["amount"], active_pallets))
                if len(assignable) > 5:
                    lines.append(f"(Showing pickers for the first 5 of {len(assignable)} assignable rows.)")
            elif assignable and not active_pallets:
                lines.append("No active pallets exist to assign them to yet.")

        await interaction.followup.send("\n".join(lines), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Finance(bot))
