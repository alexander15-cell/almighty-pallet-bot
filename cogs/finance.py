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
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
import shop_values

log = logging.getLogger(__name__)

# Computed absolute at import time (not just Path(config.INVOICE_DIR)) so
# every saved invoice path is absolute regardless of the process's working
# directory - config.INVOICE_DIR defaults to a relative path, and a stored
# relative path silently breaks later reads if the bot is ever launched
# from a different directory (see the equivalent PHOTO_DIR fix in
# cogs/item_flow.py for the exact failure this avoids).
INVOICE_DIR = Path(os.path.abspath(config.INVOICE_DIR))


def invoice_dir_for(charge_id: int) -> Path:
    d = INVOICE_DIR / str(charge_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    if config.PRESERVE_DISCORD_HISTORY:
        return
    try:
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.add_field(name="Status", value=note, inline=False)
        await message.edit(embed=embed, view=None)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        log.warning("Couldn't update charge message %s after allocation", message.id)


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ConfirmInvoiceLoggedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"invoice_confirmed:(?P<charge_id>\d+)"):
    """
    The button on every manually-submitted invoice's #awaiting-pallet-charges
    card (source='manual' only - a QuickBooks-sourced charge already gets
    pushed to QuickBooks automatically when claimed, so it never needs
    this). A DynamicItem (discord.py 2.4+) so it keeps working across bot
    restarts, same reasoning as AllocateChargeButton - these cards can sit
    for a while before Finance Management gets to them.

    Clicking it only removes the card - it never touches the underlying
    awaiting_pallet_charges row or marks it claimed, since a pallet may
    still be created and claim it later regardless of whether it's already
    been entered into QuickBooks by hand. The whole point is just "this
    doesn't need to keep cluttering Discord - it's recorded in QuickBooks
    now" - see the pallet-creation dropdown (pallet_setup.py) for where a
    charge/invoice actually gets attached to a pallet's cost basis.
    """

    def __init__(self, charge_id: int):
        super().__init__(discord.ui.Button(
            label="Confirm logged in QuickBooks", style=discord.ButtonStyle.success, emoji="✅",
            custom_id=f"invoice_confirmed:{charge_id}",
        ))
        self.charge_id = charge_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: dict):
        return cls(int(match["charge_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not (_has_role(interaction, config.ROLE_FINANCE_MGMT) or _is_admin(interaction)):
            await interaction.response.send_message(
                f"You need the **{config.ROLE_FINANCE_MGMT}** role to do that.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await interaction.message.delete()
        except discord_resilience.TRANSIENT_DISCORD_ERRORS:
            pass
        await interaction.followup.send("Confirmed - card removed.", ephemeral=True)


async def _post_awaiting_charge_card(bot: discord.Client, charge: dict):
    """Posts a charge's standing card in #awaiting-pallet-charges once it's
    been filed there (no matching pallet exists yet) - stays up until a new
    pallet's creation flow claims it (see pallet_setup.py). Covers both a
    QuickBooks-sourced charge and a manually-submitted invoice (source
    columns differ - see awaiting_pallet_charges' own CREATE TABLE comment)."""
    channel_id = db.get_shared_channel_id("awaiting-pallet-charges")
    if not channel_id:
        log.warning("No #awaiting-pallet-charges channel configured - run /setup shared-channels.")
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return
    if charge.get("source") == "manual":
        embed = discord.Embed(
            title="📥 Awaiting a pallet - submitted invoice",
            description=f"${charge['amount']:.2f}",
            color=discord.Color.dark_gold(),
        )
        embed.set_footer(
            text="Manually submitted invoice - claimed automatically when a matching pallet is created. "
                 "Once entered into QuickBooks, Finance Management can dismiss this card below."
        )
        file = None
        photo_path = charge.get("invoice_photo_path")
        if photo_path and Path(photo_path).is_file():
            path = Path(photo_path)
            file = discord.File(path, filename=path.name)
            if path.suffix.lower() in _IMAGE_EXTENSIONS:
                embed.set_image(url=f"attachment://{path.name}")
        view = discord.ui.View(timeout=None)
        view.add_item(ConfirmInvoiceLoggedButton(charge["id"]))
        message = (
            await channel.send(embed=embed, file=file, view=view) if file
            else await channel.send(embed=embed, view=view)
        )
    else:
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            txn = await quickbooks.get_transaction(self.txn_id)
        except quickbooks.QuickBooksError as e:
            await interaction.followup.send(f"Couldn't look up this charge in QuickBooks: {e}", ephemeral=True)
            return

        # Discord select menus cap out at 25 options - leave room for "New
        # Pallet" and just show the most recently created active pallets if
        # there happen to be more than that many in flight at once.
        pallets = db.get_all_pallets(include_archived=False)[:24]
        view = PalletAllocateView(self.txn_id, txn["amount"], txn["merchant"], txn["date"], interaction.message, pallets)
        await interaction.followup.send(
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


# --------------------------------------------------------------- log-sale --
# /finance log-sale (below) - logs a sale (single item or a bundle, possibly
# across different pallets) and books it to QuickBooks: a Sales Receipt
# (taxable for everything except eBay, which already collects/remits tax -
# still gets a non-taxable receipt posted to the eBay Sales account so every
# channel's revenue shows up in the books consistently) plus a Cost of Goods
# Sold Journal Entry either way. Purchase price and COGS are always entered
# fresh by a partner here - never pulled from any stored/default value.

_COST_COGS_RE = re.compile(
    r"cost\s*[:=]?\s*\$?\s*([\d]+(?:\.\d+)?).*?cogs\s*[:=]?\s*\$?\s*([\d]+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)


def _parse_combined_line(line: str):
    match = _COST_COGS_RE.search(line)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:
        return None


def _parse_item_references(text: str):
    """
    Parses /finance log-sale's `items` parameter - comma-separated
    "PalletName#ItemNumber" references, e.g. "Pallet-2026-014#3,
    Pallet-2026-009#7" (a bundle can span different pallets - see
    database.py's sales/sale_items tables). Returns (resolved_items,
    references, errors): resolved_items/references are same-length,
    same-order lists for everything that matched a real item; errors is a
    list of human-readable messages for anything that didn't - rejected
    with a clear error rather than guessed at (per the original spec).
    """
    resolved_items = []
    references = []
    errors = []
    for raw in text.split(","):
        ref = raw.strip()
        if not ref:
            continue
        if "#" not in ref:
            errors.append(f'"{ref}" - expected the format PalletName#ItemNumber')
            continue
        pallet_name, _, number_text = ref.rpartition("#")
        pallet_name = pallet_name.strip()
        number_text = number_text.strip()
        if not number_text.isdigit():
            errors.append(f'"{ref}" - item number must be a whole number')
            continue
        pallet = db.get_pallet_by_name(pallet_name)
        if not pallet:
            errors.append(f'"{ref}" - no pallet named "{pallet_name}"')
            continue
        item = db.get_item_by_pallet_and_number(pallet["id"], int(number_text))
        if not item:
            errors.append(f'"{ref}" - no item #{number_text} in "{pallet_name}"')
            continue
        resolved_items.append(item)
        references.append(ref)
    return resolved_items, references, errors


def _split_evenly(total: float, n: int) -> list:
    """Splits `total` into n amounts rounded to cents that sum EXACTLY to
    round(total, 2) - the rounding remainder goes on the last share(s), so
    a $10.00 bundle of 3 items is $3.33/$3.33/$3.34, not three $3.33s that
    silently lose a cent."""
    total_cents = round(total * 100)
    base = total_cents // n
    remainder = total_cents - base * n
    shares = [base] * n
    for i in range(remainder):
        shares[-(i + 1)] += 1
    return [s / 100 for s in shares]


def _parse_cogs_entry(text: str, references: list, resolved_items: list) -> dict:
    """
    Parses the cost/COGS modal's free-text entry - either ONE combined line
    (cost/COGS split evenly across every item in the sale) or exactly one
    line per item, each starting with that item's own reference - never a
    mix (the original spec's "all-or-nothing" requirement, so a sale can
    never end up itemized for some items and combined for others). Returns
    {item_id: (purchase_price, cogs_amount)}; raises ValueError with a
    user-facing message on anything malformed, rather than guessing.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Nothing entered - type at least one line.")

    starts_with_a_reference = any(lines[0].lower().startswith(ref.lower() + ":") for ref in references)
    if len(lines) == 1 and not starts_with_a_reference:
        parsed = _parse_combined_line(lines[0])
        if not parsed:
            raise ValueError(f'Couldn\'t read a cost/COGS pair from "{lines[0]}" - expected e.g. "cost 10.00, cogs 10.00".')
        cost, cogs = parsed
        n = len(resolved_items)
        cost_shares = _split_evenly(cost, n)
        cogs_shares = _split_evenly(cogs, n)
        return {item["id"]: (cost_shares[i], cogs_shares[i]) for i, item in enumerate(resolved_items)}

    if len(lines) != len(resolved_items):
        raise ValueError(
            f"Got {len(lines)} line(s) but this sale has {len(resolved_items)} item(s) - enter either "
            "ONE combined line, or exactly one line per item, never a mix."
        )

    result = {}
    remaining = list(zip(references, resolved_items))
    for line in lines:
        match = next((pair for pair in remaining if line.lower().startswith(pair[0].lower() + ":")), None)
        if not match:
            raise ValueError(
                f'Couldn\'t match line "{line}" to one of this sale\'s items - start each line with the '
                f'item reference, e.g. "{references[0]}: cost 10.00, cogs 10.00".'
            )
        ref, item = match
        remaining.remove(match)
        parsed = _parse_combined_line(line)
        if not parsed:
            raise ValueError(f'Couldn\'t read a cost/COGS pair from "{line}" - expected e.g. "{ref}: cost 10.00, cogs 10.00".')
        result[item["id"]] = parsed
    return result


class LogSaleCogsModal(discord.ui.Modal, title="Enter cost + COGS"):
    entries = discord.ui.TextInput(
        label="Cost & COGS - see pre-filled text for format",
        style=discord.TextStyle.paragraph,
        max_length=4000,
    )

    def __init__(self, resolved_items: list, references: list, platform: str, total_price: float,
                 already_deposited: bool, sale_date: str):
        super().__init__()
        self.resolved_items = resolved_items
        self.references = references
        self.platform = platform
        self.total_price = total_price
        self.already_deposited = already_deposited
        self.sale_date = sale_date
        if len(references) == 1:
            self.entries.default = "cost 0.00, cogs 0.00"
        else:
            self.entries.default = "\n".join(f"{ref}: cost 0.00, cogs 0.00" for ref in references)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            parsed = _parse_cogs_entry(self.entries.value, self.references, self.resolved_items)
        except ValueError as e:
            await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)
            return
        await _show_log_sale_confirmation(
            interaction, self.resolved_items, parsed, self.platform,
            self.total_price, self.already_deposited, self.sale_date,
        )


async def _show_log_sale_confirmation(interaction: discord.Interaction, resolved_items: list, parsed: dict,
                                       platform: str, total_price: float, already_deposited: bool, sale_date: str):
    n = len(resolved_items)
    allocated_shares = _split_evenly(total_price, n)
    lines = []
    total_cost = 0.0
    total_cogs = 0.0
    for i, item in enumerate(resolved_items):
        pallet = db.get_pallet(item["pallet_id"])
        purchase_price, cogs_amount = parsed[item["id"]]
        total_cost += purchase_price
        total_cogs += cogs_amount
        lines.append(
            f"**{pallet['name']}#{item['item_number']}** - sale ${allocated_shares[i]:.2f}, "
            f"cost ${purchase_price:.2f}, COGS ${cogs_amount:.2f}"
        )

    is_ebay = platform.strip().lower() == "ebay"
    tax_note = (
        "Non-taxable Sales Receipt posted to the eBay Sales account (eBay already collects/remits tax)."
        if is_ebay else
        "Taxable Sales Receipt - QuickBooks Automated Sales Tax will calculate and add the tax automatically."
    )
    deposit_note = "Already deposited" if already_deposited else "Undeposited Funds"

    embed = discord.Embed(title="Confirm sale + COGS", color=discord.Color.gold())
    embed.add_field(name=f"{n} item(s) - {platform}", value="\n".join(lines), inline=False)
    embed.add_field(name="Total Sale Price (pre-tax)", value=f"${total_price:.2f}", inline=True)
    embed.add_field(name="Total COGS", value=f"${total_cogs:.2f}", inline=True)
    embed.add_field(name="Deposit To", value=deposit_note, inline=True)
    embed.add_field(name="Sales Tax", value=tax_note, inline=False)
    embed.set_footer(text="Nothing is sent to QuickBooks until you confirm.")

    view = LogSaleConfirmView(resolved_items, parsed, platform, total_price, already_deposited, sale_date)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class LogSaleConfirmView(discord.ui.View):
    def __init__(self, resolved_items: list, parsed: dict, platform: str, total_price: float,
                 already_deposited: bool, sale_date: str):
        super().__init__(timeout=300)
        self.resolved_items = resolved_items
        self.parsed = parsed
        self.platform = platform
        self.total_price = total_price
        self.already_deposited = already_deposited
        self.sale_date = sale_date

    @discord.ui.button(label="Confirm & log to QuickBooks", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _finalize_log_sale(
            interaction, self.resolved_items, self.parsed, self.platform,
            self.total_price, self.already_deposited, self.sale_date,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled - nothing was logged.", embed=None, view=None)


async def _post_sale_to_quickbooks(sale_id: int, platform: str, total_price: float, already_deposited: bool,
                                    sale_date: str, je_lines: list) -> dict:
    """
    Creates the Sales Receipt (taxable unless platform is eBay) and the COGS
    Journal Entry for one sale, in that order - shared by _finalize_log_sale
    and /finance retry-sale so a QuickBooks failure is only ever handled one
    way. Idempotent against a partial prior attempt: skips creating another
    Sales Receipt if one's already saved on this sale (see
    database.set_sale_receipt_id) - so retrying after a Journal Entry
    failure never double-books revenue.

    Returns {'ok': True, 'sales_receipt_doc', 'journal_entry_doc'} on
    success, or {'ok': False, 'error'} - never raises, so a QuickBooks
    outage never loses the already-saved sale/sale_items rows.
    """
    sale = db.get_sale(sale_id)
    is_ebay = platform.strip().lower() == "ebay"
    sales_receipt_doc = "(already created)" if sale["quickbooks_sales_receipt_id"] else None

    if not sale["quickbooks_sales_receipt_id"]:
        income_account = config.QUICKBOOKS_EBAY_SALES_ACCOUNT_ID if is_ebay else config.QUICKBOOKS_SALES_INCOME_ACCOUNT_ID
        deposit_account = (
            config.QUICKBOOKS_BANK_ACCOUNT_ID if already_deposited else config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID
        )
        memo = f"Sale #{sale_id} via {platform} on {sale_date}"
        try:
            receipt = await quickbooks.create_sales_receipt(
                customer_id=config.QUICKBOOKS_CASH_SALES_CUSTOMER_ID,
                item_id=config.QUICKBOOKS_CASH_SALES_ITEM_ID,
                income_account_id=income_account,
                deposit_account_id=deposit_account,
                amount=total_price, date=sale_date, memo=memo,
                taxable=not is_ebay,
            )
        except quickbooks.QuickBooksError as e:
            return {"ok": False, "error": f"Sales Receipt failed: {e}"}
        db.set_sale_receipt_id(sale_id, receipt["id"])
        sales_receipt_doc = receipt.get("doc_number") or receipt["id"]

    je_memo = f"COGS for sale #{sale_id} via {platform} on {sale_date} (Sales Receipt {sales_receipt_doc})"
    try:
        je = await quickbooks.create_journal_entry(
            debit_account_id=config.QUICKBOOKS_COGS_ACCOUNT_ID,
            credit_account_id=config.QUICKBOOKS_INVENTORY_ACCOUNT_ID,
            lines=je_lines, date=sale_date, memo=je_memo,
        )
    except quickbooks.QuickBooksError as e:
        return {"ok": False, "error": f"Journal Entry failed: {e}"}

    db.mark_sale_logged(sale_id, je["id"])
    return {"ok": True, "sales_receipt_doc": sales_receipt_doc, "journal_entry_doc": je.get("doc_number") or je["id"]}


async def _finalize_log_sale(interaction: discord.Interaction, resolved_items: list, parsed: dict, platform: str,
                              total_price: float, already_deposited: bool, sale_date: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await interaction.message.edit(view=None)
    except discord_resilience.TRANSIENT_DISCORD_ERRORS:
        pass

    # Re-checked here, not just when /finance log-sale was first run - the
    # confirmation step can sit for minutes, and another sale could have
    # claimed one of these items in the meantime (the actual idempotency
    # guard is sale_items.item_id being UNIQUE; this is just the friendly,
    # all-or-nothing version of that check).
    already_sold = [item for item in resolved_items if db.get_item_sale(item["id"])]
    if already_sold:
        names = ", ".join(f"#{item['item_number']}" for item in already_sold)
        await interaction.followup.send(
            f"⚠️ Item(s) {names} were logged in another sale while this was pending - nothing was submitted.",
            ephemeral=True,
        )
        return

    n = len(resolved_items)
    allocated_shares = _split_evenly(total_price, n)
    sale_id = db.create_sale(platform, sale_date, total_price, already_deposited, created_by=interaction.user.id)

    affected_pallets = set()
    je_lines = []
    for i, item in enumerate(resolved_items):
        purchase_price, cogs_amount = parsed[item["id"]]
        allocated_price = allocated_shares[i]
        db.add_sale_item(sale_id, item["id"], allocated_price, purchase_price, cogs_amount)
        db.record_item_sale(item["id"], allocated_price, platform, actor_id=interaction.user.id)
        affected_pallets.add(item["pallet_id"])
        pallet = db.get_pallet(item["pallet_id"])
        je_lines.append({
            "amount": cogs_amount,
            "description": f"{pallet['name']}#{item['item_number']} - purchase price ${purchase_price:.2f}",
        })

    for pallet_id in affected_pallets:
        await finance_utils.refresh_finance_message(interaction.client, pallet_id)

    result = await _post_sale_to_quickbooks(sale_id, platform, total_price, already_deposited, sale_date, je_lines)

    if result["ok"]:
        item_list = ", ".join(
            f"{db.get_pallet(item['pallet_id'])['name']}#{item['item_number']}" for item in resolved_items
        )
        message = (
            f"✅ Logged sale #{sale_id} ({item_list}) via **{platform}**.\n"
            f"Sales Receipt: `{result['sales_receipt_doc']}`\n"
            f"Journal Entry: `{result['journal_entry_doc']}`"
        )
    else:
        message = (
            f"⚠️ Sale #{sale_id} saved, but QuickBooks failed: {result['error']}\n"
            f"Run `/finance retry-sale sale_id:{sale_id}` once it's fixed - nothing was lost."
        )
    await interaction.followup.send(message, ephemeral=True)


class Finance(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if not config.PRESERVE_DISCORD_HISTORY:
            self.credit_card_poll_loop.start()

    async def cog_load(self):
        # Registers the (txn_id -> button) template once so every
        # #credit-card-charges card's Allocate button keeps working across
        # bot restarts, not just the ones posted during this process's
        # lifetime - see AllocateChargeButton. Same reasoning for
        # ConfirmInvoiceLoggedButton on manually-submitted invoice cards.
        if not config.PRESERVE_DISCORD_HISTORY:
            self.bot.add_dynamic_items(AllocateChargeButton, ConfirmInvoiceLoggedButton)

    def cog_unload(self):
        self.credit_card_poll_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        #submit-invoices: one message per invoice - attach the invoice
        (photo or PDF) and put just the dollar amount in the same message,
        same idea as Data Entry's "attach photo(s) + note" pattern. Files a
        manually-submitted awaiting_pallet_charges row and posts the
        accountant's standing card in #awaiting-pallet-charges, same as a
        QuickBooks-sourced charge - the QuickBooks API being unavailable
        means this is the real, working replacement for that automatic
        path, not a fallback for when it fails.
        """
        if message.author.bot:
            return
        channel_id = db.get_shared_channel_id("submit-invoices")
        if not channel_id or message.channel.id != channel_id:
            return

        admin_role = runtime_settings.resolve_role(message.guild, config.ROLE_ADMIN)
        purchase_role = runtime_settings.resolve_role(message.guild, config.ROLE_PURCHASE_MGMT)
        author_roles = message.author.roles
        if not ((purchase_role and purchase_role in author_roles) or (admin_role and admin_role in author_roles)):
            await message.reply(
                f"You need the **{config.ROLE_PURCHASE_MGMT}** role to submit an invoice here.",
                delete_after=15,
            )
            return
        if not message.attachments:
            await message.reply(
                "Attach the invoice (photo or PDF) and put just the dollar amount in the same "
                "message, e.g. \"125.50\".",
                delete_after=15,
            )
            return
        try:
            cents = shop_values.parse_price_cents(message.content or "")
        except shop_values.ShopValidationError:
            await message.reply(
                "Couldn't read a dollar amount from your message - put just the amount, e.g. "
                "\"125.50\", alongside the attached invoice.",
                delete_after=15,
            )
            return

        amount = cents / 100
        attachment = message.attachments[0]
        charge_id = db.create_manual_invoice_charge(amount, message.author.id)
        ext = Path(attachment.filename).suffix or ".jpg"
        dest = invoice_dir_for(charge_id) / f"invoice{ext}"
        await attachment.save(dest)
        db.set_awaiting_pallet_charge_invoice_photo(charge_id, str(dest))

        charge = db.get_awaiting_pallet_charge(charge_id)
        try:
            await _post_awaiting_charge_card(self.bot, charge)
        except discord_resilience.TRANSIENT_DISCORD_ERRORS:
            log.exception("Failed to post awaiting-pallet-charges card for manual invoice %s", charge_id)

        if not config.PRESERVE_DISCORD_HISTORY:
            try:
                await message.delete()
            except discord_resilience.TRANSIENT_DISCORD_ERRORS:
                pass
        await message.channel.send(
            f"✅ Invoice for ${amount:.2f} logged - it'll show up in #awaiting-pallet-charges "
            f"until a matching pallet is created.",
            delete_after=15,
        )

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
        if config.PRESERVE_DISCORD_HISTORY:
            return
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        db.set_pallet_cost(pallet["id"], cost, actor_id=interaction.user.id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        was_already_priced = item["sale_price"] is not None
        db.record_item_sale(item["id"], price, platform.strip(), actor_id=interaction.user.id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])

        verb = "Corrected" if was_already_priced else "Recorded"
        await interaction.followup.send(
            f"💰 {verb} sale price for **{pallet['name']}** item #{item_number}: "
            f"${price:.2f} on {platform.strip()}. Live card updated.",
            ephemeral=True,
        )

    @finance_group.command(
        name="log-sale",
        description="Log a sale (single item or bundle) - opens a form for cost/COGS, then books it to QuickBooks.",
    )
    @app_commands.describe(
        items='Item references, comma-separated: "PalletName#3" or "PalletName#3, OtherPallet#7" for a bundle',
        total_price="Total sale price for everything in this sale, combined (pre-tax)",
        platform="Where this sold (leave blank to use what was picked at Mark as Sold, if all items agree)",
        already_deposited="Has this cash already been deposited to the real bank account? (default: No)",
    )
    async def log_sale(self, interaction: discord.Interaction, items: str, total_price: float,
                        platform: str = None, already_deposited: bool = False):
        if not await _require_any_role(interaction, [config.ROLE_FINANCE_MGMT]):
            return
        if total_price <= 0:
            await interaction.response.send_message("Total sale price must be positive.", ephemeral=True)
            return

        resolved_items, references, errors = _parse_item_references(items)
        if errors:
            await interaction.response.send_message(
                "Couldn't read some of these item references:\n" + "\n".join(f"- {e}" for e in errors),
                ephemeral=True,
            )
            return
        if not resolved_items:
            await interaction.response.send_message("No items given.", ephemeral=True)
            return

        already_sold = [item for item in resolved_items if db.get_item_sale(item["id"])]
        if already_sold:
            names = ", ".join(f"#{item['item_number']}" for item in already_sold)
            await interaction.response.send_message(
                f"Item(s) {names} are already part of a logged sale - can't log them again.", ephemeral=True,
            )
            return

        if platform:
            platform = platform.strip()
        else:
            platforms_picked = {item["sale_platform"] for item in resolved_items if item["sale_platform"]}
            if len(platforms_picked) == 1:
                platform = platforms_picked.pop()
            else:
                await interaction.response.send_message(
                    "Pass `platform` explicitly - these items don't all have the same platform recorded "
                    "from Mark as Sold (or none do).",
                    ephemeral=True,
                )
                return

        sale_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        await interaction.response.send_modal(
            LogSaleCogsModal(resolved_items, references, platform, total_price, already_deposited, sale_date)
        )

    @finance_group.command(
        name="retry-sale",
        description="Retry pushing a saved sale to QuickBooks after an earlier API failure.",
    )
    @app_commands.describe(sale_id="The sale ID from the earlier failure message")
    async def retry_sale(self, interaction: discord.Interaction, sale_id: int):
        if not await _require_any_role(interaction, [config.ROLE_FINANCE_MGMT]):
            return
        sale = db.get_sale(sale_id)
        if not sale:
            await interaction.response.send_message(f"No sale #{sale_id} found.", ephemeral=True)
            return
        if sale["cogs_logged_at"]:
            await interaction.response.send_message(f"Sale #{sale_id} is already fully logged to QuickBooks.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        sale_items = db.get_sale_items(sale_id)
        je_lines = [
            {
                "amount": si["cogs_amount"],
                "description": f"{si['pallet_name']}#{si['item_number']} - purchase price ${si['purchase_price']:.2f}",
            }
            for si in sale_items
        ]
        result = await _post_sale_to_quickbooks(
            sale_id, sale["platform"], sale["total_price"], bool(sale["already_deposited"]), sale["sale_date"], je_lines,
        )
        if result["ok"]:
            await interaction.followup.send(
                f"✅ Sale #{sale_id} logged. Sales Receipt: `{result['sales_receipt_doc']}`, "
                f"Journal Entry: `{result['journal_entry_doc']}`.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(f"⚠️ Still failing: {result['error']}", ephemeral=True)

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

        await interaction.response.defer(ephemeral=True, thinking=True)
        db.record_refund(item["id"], amount, reason.strip(), actor_id=interaction.user.id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        db.record_expense(pallet["id"], amount, reason.strip(), actor_id=interaction.user.id, item_id=item_id)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        item_note = f" (item #{item_number})" if item_number is not None else ""
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        old_price = db.reverse_sale(item["id"], reason.strip(), actor_id=interaction.user.id)
        if old_price is None:
            await interaction.followup.send(
                f"Item #{item_number} doesn't have a sale price recorded - nothing to reverse.", ephemeral=True
            )
            return
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        db.set_items_received_override(pallet["id"], count)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        db.clear_items_received_override(pallet["id"])
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True, thinking=True)
        await finance_utils.refresh_finance_message(self.bot, pallet["id"])
        await interaction.followup.send(
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
        cost_type_labels = {"credit_card": "Credit Card", "invoice": "Purchase Invoice", "shipping": "Shipping", "supplies": "Supplies", "misc": "Misc"}
        for cost_type, label in cost_type_labels.items():
            if breakdown.get(cost_type):
                cost_lines.append(f"{label}: ${breakdown[cost_type]:.2f}")
        embed.add_field(
            name="Cost Basis",
            value=("\n".join(cost_lines) if cost_lines else "Nothing recorded yet")
            + (f"\n**Total: ${fin['cost']:.2f}**" if fin["cost"] is not None else ""),
            inline=False,
        )

        embed.add_field(name="Revenue So Far (net of sales tax)", value=f"${fin['revenue_so_far']:.2f}", inline=True)
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

        cogs_logged = db.get_pallet_cogs_logged_total(pallet["id"])
        if cogs_logged:
            embed.add_field(name="COGS Logged (QuickBooks)", value=f"${cogs_logged:.2f}", inline=True)

        if fin["cost"] is not None:
            margin_pct = (fin["profit_so_far"] / fin["revenue_so_far"] * 100) if fin["revenue_so_far"] else None
            pl_word = "Profit" if fin["profit_so_far"] >= 0 else "Loss"
            margin_note = f" ({margin_pct:.1f}% margin)" if margin_pct is not None else ""
            embed.add_field(name=pl_word, value=f"${fin['profit_so_far']:.2f}{margin_note}", inline=True)
            breakeven_note = (
                f"✅ Broken even ({fin['cost_recovery_pct']:.0f}% of cost recovered)" if fin["broke_even"]
                else f"${fin['cost'] - fin['net_revenue']:.2f} more needed ({fin['cost_recovery_pct']:.0f}% recovered)"
                if fin["cost_recovery_pct"] is not None
                else "No revenue yet"
            )
            embed.add_field(name="Breakeven", value=breakeven_note, inline=True)
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
        name="list-accounts",
        description="Admin: list QuickBooks accounts and their IDs, for QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID.",
    )
    @app_commands.describe(account_type="Which account types to show (default: Bank and Credit Card - the only types this bot watches)")
    @app_commands.choices(account_type=[
        app_commands.Choice(name="Bank and Credit Card (default)", value="bank_and_credit_card"),
        app_commands.Choice(name="All account types", value="all"),
    ])
    async def list_accounts(self, interaction: discord.Interaction, account_type: app_commands.Choice[str] = None):
        if not _is_admin(interaction):
            await interaction.response.send_message("You need **Pallet Admin** to do that.", ephemeral=True)
            return
        if not quickbooks.is_connected():
            await interaction.response.send_message(
                "QuickBooks isn't connected yet - run `/finance connect-quickbooks` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        types = None if (account_type and account_type.value == "all") else ["Bank", "Credit Card"]
        try:
            accounts = await quickbooks.list_accounts(types)
        except quickbooks.QuickBooksError as e:
            await interaction.followup.send(f"⚠️ Couldn't fetch accounts: {e}", ephemeral=True)
            return

        if not accounts:
            await interaction.followup.send(
                "No matching accounts found. If you haven't linked a card/bank account inside "
                "QuickBooks yet (Banking → Link account), do that first - this only lists accounts "
                "that already exist there.",
                ephemeral=True,
            )
            return

        lines = [f"**{a['name']}** ({a['type']}) - ID `{a['id']}` - ${a['balance']:.2f}" for a in accounts]
        description = "\n".join(lines)
        if len(description) > 4096:
            description = description[:4090] + "\n..."
        embed = discord.Embed(
            title="🏦 QuickBooks Accounts",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Copy the ID of the account you want watched into QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID in .env, then restart the bot.")
        await interaction.followup.send(embed=embed, ephemeral=True)

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
