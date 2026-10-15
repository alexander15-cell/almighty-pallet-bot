"""
Starts the web dashboard (webapp/) inside the bot's own process, on the
same event loop - so its pages can call the same ItemFlow / finance code
and post to Discord exactly like a button click. Only loaded when
config.WEB_DASHBOARD_ENABLED (see bot.py).
"""
import logging

from aiohttp import web
from discord.ext import commands, tasks

import config
import stripe_shop
from webapp.server import create_app

log = logging.getLogger(__name__)


class WebDashboard(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.runner = None

    async def cog_load(self):
        if not (config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET):
            log.warning("Web dashboard: DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET aren't set - the site will "
                        "start but nobody can sign in until they are.")
        self.runner = web.AppRunner(create_app(self.bot), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, config.WEB_HOST, config.WEB_PORT)
        await site.start()
        log.info("Web dashboard listening on http://%s:%s (public address %s)",
                 config.WEB_HOST, config.WEB_PORT, config.WEB_BASE_URL)
        if config.STRIPE_ENABLED and not config.PRESERVE_DISCORD_HISTORY:
            self.stripe_retry_loop.start()

    @tasks.loop(minutes=10)
    async def stripe_retry_loop(self):
        """Finishes website orders whose fulfillment didn't complete (QuickBooks
        or Discord was down, Stripe's fee not reported yet)."""
        await stripe_shop.retry_pending(self.bot)

    @stripe_retry_loop.before_loop
    async def _before_stripe_retry(self):
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        self.stripe_retry_loop.cancel()
        if self.runner is not None:
            await self.runner.cleanup()


async def setup(bot: commands.Bot):
    await bot.add_cog(WebDashboard(bot))
