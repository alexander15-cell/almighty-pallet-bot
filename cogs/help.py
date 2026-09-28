"""
/help - a plain-language command finder for anyone unsure which slash
command (or button/channel) they need. #information (/setup info-channel)
has the full written guide; this is the quick "just tell me what I'm trying
to do" search over the same ground, so nobody has to already know the right
command name or scroll through #information to find it.
"""
import discord
from discord import app_commands
from discord.ext import commands

import help_content

COLOR = discord.Color.blurple()


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="help", description="Not sure which command you need? Search by what you're trying to do.")
    @app_commands.describe(query='What are you trying to do? e.g. "submit invoice", "delete a pallet", "list on ebay"')
    async def help_command(self, interaction: discord.Interaction, query: str = None):
        if not query:
            embed = discord.Embed(
                title="🔎 How can I help?",
                description=(
                    "Run `/help` again with `query` set to what you're trying to do - e.g. "
                    "`/help query:submit invoice` or `/help query:delete a pallet` - and I'll "
                    "point you at the right command, button, or channel.\n\n"
                    "For the full written guide (pipeline, roles, every command), see "
                    "**#information**."
                ),
                color=COLOR,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        matches = help_content.search(query)
        if not matches:
            await interaction.response.send_message(
                f"Couldn't find anything for \"{query}\". Try different words, check "
                "**#information** for the full guide, or ask a Pallet Admin.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title=f'🔎 Results for "{query}"', color=COLOR)
        for entry in matches:
            embed.add_field(name=entry.usage, value=entry.description, inline=False)
        embed.set_footer(text="Didn't see what you needed? Try different words, or check #information for the full guide.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Help(bot))
