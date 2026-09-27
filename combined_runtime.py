"""One guarded Discord connection for Almighty intake and approved publishing."""
import asyncio
from contextlib import suppress
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from combined_guards import RuntimeGuard, install_dispatch_guards
from combined_settings import SetupError, check_state

log = logging.getLogger('four_guys')
COGS = ('cogs.item_flow', 'cogs.ebay', 'cogs.fb_marketplace')

class GuardedTree(app_commands.CommandTree):
    async def _call(self, interaction):
        return await self.client.combined_guard.run(interaction, lambda: super(GuardedTree, self)._call(interaction))

    async def on_error(self, interaction, error):
        log.warning('Command failed; no private error details logged.')
        with suppress(Exception):
            if interaction.response.is_done():
                await interaction.followup.send('This action could not finish. Check the latest item card and try again.', ephemeral=True)
            else:
                await interaction.response.send_message('This action could not finish. Check the latest item card and try again.', ephemeral=True)

class GuardedBot(commands.Bot):
    def __init__(self, settings, *, send=False, sync_commands=False):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents,
                         tree_cls=GuardedTree, allowed_mentions=discord.AllowedMentions.none(),
                         max_messages=100, enable_debug_events=False)
        self.settings, self.sending, self.sync_commands = settings, send, sync_commands
        self.workflow_lock = asyncio.Lock()
        self.combined_guard = RuntimeGuard(settings, self.workflow_lock)
        self._worker_task = None
        self.store = self.adapter = self.journal = self.delivery = None
        self._connected_once = False
        self._http_guard()
        # Discard unrelated message events before caching or dispatch. No DMs,
        # general-chat history, finance channels or other guilds are processed.
        for event in ('MESSAGE_CREATE', 'MESSAGE_UPDATE', 'MESSAGE_DELETE', 'MESSAGE_DELETE_BULK'):
            original = self._connection.parsers[event]
            def limited(data, parser=original):
                if str(data.get('guild_id')) == settings.guild_id and str(data.get('channel_id')) in settings.channels.values():
                    return parser(data)
            self._connection.parsers[event] = limited

    def _http_guard(self):
        original = self.http.request
        async def request(route, **kwargs):
            channel = route.channel_id
            guild = route.guild_id
            if channel is not None and str(channel) not in self.settings.channels.values():
                raise SetupError('Discord request outside configured workflow channels blocked.')
            if guild is not None and str(guild) != self.settings.guild_id:
                raise SetupError('Discord request outside configured server blocked.')
            if route.path.startswith('/channels/') and route.method != 'GET' and not (
                    route.method == 'POST' and route.path == '/channels/{channel_id}/messages'):
                raise SetupError('Only new workflow messages are allowed; channel/message changes are disabled.')
            if not route.path.startswith('/channels/') and route.method != 'GET':
                command_suffix = f'/applications/{self.settings.application_id}/guilds/{self.settings.guild_id}/commands'
                if not (self.sync_commands and route.method == 'PUT'
                        and route.path == '/applications/{application_id}/guilds/{guild_id}/commands'
                        and route.url.endswith(command_suffix)):
                    raise SetupError('Server, role, account and global command changes are disabled.')
            return await original(route, **kwargs)
        self.http.request = request

    def get_channel(self, id):
        if str(id) not in self.settings.channels.values():
            return None
        return super().get_channel(id)

    def _schedule_event(self, coro, event_name, *args, **kwargs):
        if event_name == 'on_message':
            original = coro
            async def scoped(*inner_args, **inner_kwargs):
                if not inner_args or not self.combined_guard.allowed_message(inner_args[0]):
                    return
                async with self.workflow_lock:
                    if not self.combined_guard.allowed_message(inner_args[0]):
                        return
                    try:
                        await original(*inner_args, **inner_kwargs)
                    finally:
                        if self.adapter is not None:
                            await self.adapter.reconcile()
            coro = scoped
        return super()._schedule_event(coro, event_name, *args, **kwargs)

    async def on_message(self, message):
        # Intake is handled by the scoped ItemFlow listener, not prefix commands.
        pass

    async def setup_hook(self):
        check_state(self.settings)
        application = await self.application_info()
        if str(application.id) != self.settings.application_id or str(self.user.id) != self.settings.application_id:
            raise SetupError('This token belongs to a different bot. No workflow was started.')
        install_dispatch_guards()
        from shop_approval import ShopReviewStore
        from combined_intake import IntakeAdapter
        from combined_delivery import DeliveryService
        from publisher.journal import Journal
        from publisher.transport import Transport
        self.store = ShopReviewStore(self.settings.state_directory / 'shop.sqlite',
            guild_id=self.settings.guild_id, shop_channel_id=self.settings.channels['website_shop'],
            operator_ids=self.settings.operator_ids)
        self.adapter = IntakeAdapter(self, self.settings, self.store)
        self.combined_guard.after_action = self.adapter.reconcile
        self.journal = Journal(self.settings.state_directory / 'publisher.sqlite', self.settings.source_id,
            self.settings.guild_id, self.settings.website, self.settings.application_id,
            self.settings.channels['listed'], self.settings.channels['sold'])
        transport = Transport(self.settings.website, self.settings.secret) if self.sending else None
        self.delivery = DeliveryService(self.store, self.journal, transport, self.adapter.resolve_content,
                                        self.adapter.lifecycle, enabled=self.sending, max_deliveries=1)
        for cog in COGS:
            await self.load_extension(cog)
        self._add_commands()

    def _add_commands(self):
        group = app_commands.Group(name='website', description='Website approval and safe item controls')

        @group.command(name='status', description='Check approval and website delivery status')
        async def status(interaction: discord.Interaction):
            counts = self.journal.counts()
            mode = 'Live publishing' if self.sending else 'Preview only — no website sends'
            await interaction.response.send_message(f'{mode}. Delivery counts: {counts}', ephemeral=True)

        @group.command(name='review', description='Show or recover the latest website reviews')
        async def review(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self.adapter.reconcile()
            await interaction.followup.send('Checked website reviews. Use #website_shop to enter price and eBay link, then approve.', ephemeral=True)

        @group.command(name='hold', description='Put a new-inventory item on hold and queue website removal')
        async def hold(interaction: discord.Interaction, item_id: str):
            import database as db
            if not item_id.isascii() or not item_id.isdigit() or not self.settings.item_id_floor <= int(item_id) <= 2147483647:
                await interaction.response.send_message('Use the new item ID from this bot. Historical listings are not adopted.', ephemeral=True)
                return
            item = db.get_item(int(item_id))
            if not item or item['status'] in {db.STATUS_SOLD, db.STATUS_SHIPPED, db.STATUS_DELETED, db.STATUS_ON_HOLD}:
                await interaction.response.send_message('That item cannot be put on hold. Check its latest card.', ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            result = await self.get_cog('ItemFlow').place_item_on_hold(item, db.HOLD_REASON_MANUAL_LISTING, '', interaction.user.id)
            await interaction.followup.send('Placed on hold. Website removal is queued, not yet confirmed.' if result else 'Hold could not finish. Try again.', ephemeral=True)

        self.tree.add_command(group)

    async def on_ready(self):
        self.combined_guard.ready = False
        guild = self.get_guild(int(self.settings.guild_id))
        if not guild or not guild.me or guild.me.guild_permissions.administrator:
            await self.close()
            raise SetupError('Configured server unavailable, or bot has Administrator. Use narrow channel permissions.')
        for channel_id in self.settings.channels.values():
            channel = super().get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
                await self.close()
                raise SetupError('One of the configured workflow text channels is unavailable.')
            permissions = channel.permissions_for(guild.me)
            if not all(getattr(permissions, key) for key in ('view_channel', 'read_message_history', 'send_messages', 'embed_links', 'attach_files')):
                await self.close()
                raise SetupError('The bot needs the five workflow permissions in every configured channel.')
        if any(guild.get_role(int(role)) is None for role in self.settings.role_ids.values()):
            await self.close()
            raise SetupError('A configured staff role no longer exists; recheck role IDs.')
        async with self.workflow_lock:
            if not self._connected_once:
                if self.sync_commands:
                    target = discord.Object(id=guild.id)
                    self.tree.copy_global_to(guild=target)
                    await self.tree.sync(guild=target)
                await self.adapter.restore_views()
                self._connected_once = True
            self.combined_guard.ready = True
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
        log.info('Combined bot connected. Website sending: %s.', self.sending)

    async def on_disconnect(self):
        self.combined_guard.ready = False

    async def on_error(self, event_method, *args, **kwargs):
        # Raw Discord errors may contain message bodies or credentials. Do not log.
        log.warning('A workflow action could not finish. Check permissions and the latest item card.')

    async def _worker(self):
        while not self.is_closed():
            try:
                async with self.workflow_lock:
                    if self.combined_guard.ready:
                        intake = await self.adapter.reconcile()
                        delivery = await self.delivery.tick()
                        temporary = self.settings.state_directory / 'status.tmp'
                        temporary.write_text(json.dumps({'connected': True, 'publishing': self.sending,
                            'intake': intake, 'delivery': delivery}), encoding='utf-8')
                        temporary.replace(self.settings.state_directory / 'status.json')
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning('A review/delivery check failed; retained work will be checked again.')
            await asyncio.sleep(self.settings.poll_seconds)

    async def close(self):
        self.combined_guard.ready = False
        if self._worker_task and self._worker_task is not asyncio.current_task():
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
            self._worker_task = None
        await super().close()
        for name in ('adapter', 'journal', 'store'):
            value = getattr(self, name, None)
            if value is not None:
                value.close()
                setattr(self, name, None)
