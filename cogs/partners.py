"""
/partners - the partner ledger (see partner_ledger.py): money put in,
expenses paid personally, reimbursements, profit payouts, and each
partner's equal share of the profit. A record only - nothing here moves
money or touches QuickBooks.

/partners add / remove        - Pallet Admin: who the partners are
/partners put-in              - Finance Management: a partner put money in
/partners paid-expense        - Finance Management: a partner paid a business
                                expense personally (business owes it back)
/partners reimburse           - Finance Management: business paid a partner back
/partners payout              - Finance Management: profit paid to a partner
/partners void                - Finance Management: undo a mistaken entry
/partners summary / history   - Finance Management or any partner
Every entry is also posted to #finance-audit-log.
"""
import discord
from discord import app_commands
from discord.ext import commands

import config
import database as db
import finance_utils
import partner_ledger
import runtime_settings


def _has_role(interaction: discord.Interaction, role_name: str) -> bool:
    role = runtime_settings.resolve_role(interaction.guild, role_name)
    return bool(role and role in interaction.user.roles)


async def _require(interaction: discord.Interaction, *role_names: str) -> bool:
    if _has_role(interaction, config.ROLE_ADMIN) or any(_has_role(interaction, r) for r in role_names):
        return True
    names = " or ".join(f"**{r}**" for r in role_names) or f"**{config.ROLE_ADMIN}**"
    await interaction.response.send_message(f"You need {names} to do that.", ephemeral=True)
    return False


async def _require_finance_or_partner(interaction: discord.Interaction) -> bool:
    partner = db.get_partner_by_discord_id(interaction.user.id)
    if (partner and partner["active"]) or _has_role(interaction, config.ROLE_FINANCE_MGMT) \
            or _has_role(interaction, config.ROLE_ADMIN):
        return True
    await interaction.response.send_message(
        f"Only partners and **{config.ROLE_FINANCE_MGMT}** can see the partner ledger.", ephemeral=True
    )
    return False


def _money(value: float) -> str:
    value = round(value or 0.0, 2) + 0.0
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def build_summary_embed(data: dict) -> discord.Embed:
    profit = data["profit"]
    lines = [
        f"Profit to date: **{_money(profit['profit'])}** "
        f"(net revenue {_money(profit['net_revenue'])} - cost {_money(profit['cost'])})",
        f"Split equally {len(data['partners'])} ways: **{_money(data['share'])}** each",
    ]
    if profit["pallets_missing_cost"]:
        names = ", ".join(profit["pallets_missing_cost"][:5])
        lines.append(f"⚠️ No cost recorded yet for: {names} - profit is overstated until it is.")
    if data["total_owed"]:
        lines.append(f"Business owes partners **{_money(data['total_owed'])}** in unreimbursed expenses.")
    embed = discord.Embed(title="🤝 Partner Ledger", description="\n".join(lines), color=discord.Color.dark_teal())
    for row in data["partners"]:
        value = (
            f"Put in {_money(row['contributed'])} · Payouts {_money(row['payouts'])}\n"
            f"**Equity {_money(row['equity'])}** (put in + share - payouts)\n"
            f"Owed to them: {_money(row['owed'])}"
            + (f" · Behind on payouts by {_money(row['behind'])}" if row["behind"] else "")
        )
        embed.add_field(name=row["partner"]["display_name"], value=value, inline=False)
    if not data["partners"]:
        embed.add_field(name="No partners yet", value="A Pallet Admin can add them with `/partners add`.", inline=False)
    embed.set_footer(text="A record only - the bot never moves money. Profit uses the bot's own pallet numbers.")
    return embed


class Partners(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(name="partners", description="Partner ledger - money in, reimbursements, payouts")

    # ------------------------------------------------------------ who

    @group.command(name="add", description="Add a partner (profit is split equally between partners).")
    @app_commands.describe(member="The partner's Discord account")
    async def add(self, interaction: discord.Interaction, member: discord.Member):
        if not await _require(interaction):
            return
        partner = db.add_partner(member.id, member.display_name, added_by=interaction.user.id)
        count = len(db.get_partners())
        await interaction.response.send_message(
            f"🤝 **{partner['display_name']}** is a partner. {count} active partner(s) - profit splits {count} ways.",
            ephemeral=True,
        )

    @group.command(name="remove", description="Remove a partner from the split (their history is kept).")
    @app_commands.describe(member="The partner's Discord account")
    async def remove(self, interaction: discord.Interaction, member: discord.Member):
        if not await _require(interaction):
            return
        if not db.deactivate_partner(member.id):
            await interaction.response.send_message(f"{member.display_name} isn't an active partner.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{member.display_name}** removed from the split. Their ledger history is kept.", ephemeral=True
        )

    # ---------------------------------------------------------- entries

    async def _record(self, interaction: discord.Interaction, member, entry_type: str, amount: float,
                      note: str, date: str):
        if not await _require(interaction, config.ROLE_FINANCE_MGMT):
            return
        target = member or interaction.user
        partner = db.get_partner_by_discord_id(target.id)
        if not partner or not partner["active"]:
            await interaction.response.send_message(
                f"**{target.display_name}** isn't a partner - a Pallet Admin can add them with `/partners add`.",
                ephemeral=True,
            )
            return
        if amount <= 0:
            await interaction.response.send_message("Amount must be more than $0.", ephemeral=True)
            return
        try:
            entry_date = partner_ledger.parse_entry_date(date)
        except ValueError as e:
            await interaction.response.send_message(f"`date` {e}.", ephemeral=True)
            return
        if entry_type == "reimbursement":
            owed = partner_ledger.owed_to_partner(partner["id"])
            if amount > owed + 0.005:
                await interaction.response.send_message(
                    f"The business only owes **{partner['display_name']}** {_money(owed)} for expenses they paid - "
                    f"record those first with `/partners paid-expense`, or use `/partners payout` for profit.",
                    ephemeral=True,
                )
                return

        entry_id = db.add_partner_ledger_entry(partner["id"], entry_type, amount, (note or "").strip() or None,
                                               entry_date, created_by=interaction.user.id)
        label = partner_ledger.ENTRY_TYPES[entry_type]
        note_text = f" - {note.strip()}" if note and note.strip() else ""
        await interaction.response.send_message(
            f"✅ Entry #{entry_id}: **{partner['display_name']}** - {label} {_money(amount)} on {entry_date}{note_text}. "
            f"(`/partners void entry_id:{entry_id}` if that's wrong.)",
            ephemeral=True,
        )
        await finance_utils.post_to_finance_audit_log(
            interaction.client,
            f"🤝 **Partner ledger #{entry_id}**: {partner['display_name']} - {label} {_money(amount)} "
            f"on {entry_date}{note_text} (recorded by {interaction.user.display_name})",
        )

    @group.command(name="put-in", description="Record a partner putting money into the business.")
    @app_commands.describe(amount="How much they put in", member="Which partner (default: you)",
                           note="What it was for (optional)", date="YYYY-MM-DD (default today)")
    async def put_in(self, interaction: discord.Interaction, amount: float, member: discord.Member = None,
                     note: str = None, date: str = None):
        await self._record(interaction, member, "contribution", amount, note, date)

    @group.command(name="paid-expense", description="Record a partner paying a business expense personally.")
    @app_commands.describe(amount="How much they paid", note="What it was (e.g. pallet deposit, tape)",
                           member="Which partner (default: you)", date="YYYY-MM-DD (default today)")
    async def paid_expense(self, interaction: discord.Interaction, amount: float, note: str,
                           member: discord.Member = None, date: str = None):
        await self._record(interaction, member, "fronted", amount, note, date)

    @group.command(name="reimburse", description="Record the business paying a partner back for expenses.")
    @app_commands.describe(member="Which partner was paid back", amount="How much",
                           note="How it was paid (optional)", date="YYYY-MM-DD (default today)")
    async def reimburse(self, interaction: discord.Interaction, member: discord.Member, amount: float,
                        note: str = None, date: str = None):
        await self._record(interaction, member, "reimbursement", amount, note, date)

    @group.command(name="payout", description="Record profit paid out to a partner.")
    @app_commands.describe(member="Which partner was paid", amount="How much",
                           note="How it was paid (optional)", date="YYYY-MM-DD (default today)")
    async def payout(self, interaction: discord.Interaction, member: discord.Member, amount: float,
                     note: str = None, date: str = None):
        await self._record(interaction, member, "payout", amount, note, date)

    @group.command(name="void", description="Undo a mistaken partner ledger entry (kept in history as voided).")
    @app_commands.describe(entry_id="The entry number (from /partners history)", reason="Why")
    async def void(self, interaction: discord.Interaction, entry_id: int, reason: str):
        if not await _require(interaction, config.ROLE_FINANCE_MGMT):
            return
        entry = db.get_partner_ledger_entry(entry_id)
        if not entry or not db.void_partner_ledger_entry(entry_id, reason.strip(), voided_by=interaction.user.id):
            await interaction.response.send_message(
                f"No active entry #{entry_id} (wrong number, or already voided).", ephemeral=True
            )
            return
        label = partner_ledger.ENTRY_TYPES[entry["entry_type"]]
        await interaction.response.send_message(
            f"↩️ Voided entry #{entry_id} ({label} {_money(entry['amount'])}): {reason.strip()}", ephemeral=True
        )
        await finance_utils.post_to_finance_audit_log(
            interaction.client,
            f"↩️ **Partner ledger #{entry_id} voided** ({label} {_money(entry['amount'])}): {reason.strip()} "
            f"(by {interaction.user.display_name})",
        )

    # ------------------------------------------------------------ views

    @group.command(name="summary", description="Each partner's equity, profit share, money owed, and payouts.")
    async def summary(self, interaction: discord.Interaction):
        if not await _require_finance_or_partner(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(embed=build_summary_embed(partner_ledger.summary()), ephemeral=True)

    @group.command(name="history", description="Recent partner ledger entries, with their entry numbers.")
    @app_commands.describe(member="Only this partner (optional)")
    async def history(self, interaction: discord.Interaction, member: discord.Member = None):
        if not await _require_finance_or_partner(interaction):
            return
        partner_id = None
        if member:
            partner = db.get_partner_by_discord_id(member.id)
            if not partner:
                await interaction.response.send_message(f"{member.display_name} isn't a partner.", ephemeral=True)
                return
            partner_id = partner["id"]
        entries = db.get_partner_ledger(partner_id, include_voided=True, limit=25)
        if not entries:
            await interaction.response.send_message("No partner ledger entries yet.", ephemeral=True)
            return
        lines = []
        for e in entries:
            text = (f"`#{e['id']}` {e['entry_date']} **{e['display_name']}** - "
                    f"{partner_ledger.ENTRY_TYPES[e['entry_type']]} {_money(e['amount'])}"
                    + (f" - {e['note']}" if e["note"] else ""))
            lines.append(f"~~{text}~~ (voided: {e['void_reason']})" if e["voided_at"] else text)
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900].rsplit("\n", 1)[0] + "\n…"
        await interaction.response.send_message(body, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Partners(bot))
