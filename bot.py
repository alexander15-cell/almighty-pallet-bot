"""
Entry point for the pallet tracking bot.

Run with:  python bot.py
Requires a .env file - see .env.example for the required variables.
"""
import asyncio
import logging

import discord
from discord.ext import commands

import config
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pallet_bot")

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # required to read text/attachments in Data Entry
INTENTS.members = True  # required to check roles reliably

bot = commands.Bot(command_prefix="!", intents=INTENTS)

COGS = [
    "cogs.pallet_setup",
    "cogs.item_flow",
    "cogs.admin_tools",
    "cogs.finance",
    "cogs.ebay",
]


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id={bot.user.id})")
    try:
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            # Commands registered via @app_commands.command() with no guild
            # restriction live in the GLOBAL command tree. Syncing straight
            # to a guild without this copy step syncs an empty guild-specific
            # list, which is why this previously logged "Synced 0 commands".
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        log.exception(f"Failed to sync commands: {e}")


async def main():
    if not config.DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set. Check your .env file.")
    if not config.AI_ENABLED:
        if config.AI_REVIEW_BACKEND == "anthropic":
            log.warning(
                "No ANTHROPIC_API_KEY set - running WITHOUT AI review. "
                "Items will skip straight from Data Entry to Queue Review. "
                "Add ANTHROPIC_API_KEY to .env later to turn AI review on."
            )
        else:
            log.warning(
                f"AI_REVIEW_BACKEND is set to '{config.AI_REVIEW_BACKEND}', which isn't "
                "'anthropic' or 'ollama' - running WITHOUT AI review. Items will skip "
                "straight from Data Entry to Queue Review."
            )
    elif config.AI_REVIEW_BACKEND == "ollama":
        log.info(
            f"AI review running against local Ollama ({config.OLLAMA_BASE_URL}, "
            f"model={config.OLLAMA_VISION_MODEL}) - make sure Ollama is running and "
            "the model is pulled, or each review will fail and fall back to the raw note."
        )

    db.init_db()
    log.info(f"Database ready at {config.DATABASE_PATH}")

    async with bot:
        for cog in COGS:
            await bot.load_extension(cog)
            log.info(f"Loaded {cog}")
        await bot.start(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
