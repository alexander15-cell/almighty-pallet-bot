"""Offline dispatch guard tests. Synthetic identities; no sockets or state DB."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest

import combined_guards as guards


APP = '100000000000000001'
GUILD = '100000000000000002'
ENTRY = '100000000000000003'
LISTED = '100000000000000004'
SHOP = '100000000000000005'
OPERATOR = '100000000000000006'
FLOOR = 1000000000


def settings():
    return NS(application_id=APP, guild_id=GUILD, operator_ids=(OPERATOR,), item_id_floor=FLOOR,
              channels={'data-entry': ENTRY, 'listed': LISTED, 'website_shop': SHOP})


class Response:
    def __init__(self):
        self.notices = []
        self.done = False

    def is_done(self):
        return self.done

    async def send_message(self, message, **kwargs):
        self.notices.append((message, kwargs))

    async def send(self, message, **kwargs):
        self.notices.append((message, kwargs))


def interaction(guard=None):
    response = Response()
    return NS(application_id=int(APP), guild_id=int(GUILD), channel_id=int(LISTED),
              user=NS(id=int(OPERATOR), bot=False), type=discord.InteractionType.component,
              data={'custom_id': f'pallet_bot:mark_sold:{FLOOR}'},
              message=NS(author=NS(id=int(APP), bot=True), guild=NS(id=int(GUILD)),
                         channel=NS(id=int(LISTED)), webhook_id=None),
              client=NS(combined_guard=guard), response=response, followup=response)


def message():
    return NS(guild=NS(id=int(GUILD)), channel=NS(id=int(ENTRY)), author=NS(id=int(OPERATOR), bot=False),
              webhook_id=None, attachments=[NS(size=1024, content_type='image/jpeg', filename='item.jpg')])


def ready_guard():
    guard = guards.RuntimeGuard(settings(), asyncio.Lock())
    guard.ready = True
    return guard


def test_not_ready_is_default_and_scope_is_fail_closed():
    guard = guards.RuntimeGuard(settings(), asyncio.Lock())
    assert guard.ready is False
    assert not guard.allowed_interaction(interaction(guard))
    assert not guard.allowed_message(message())


@pytest.mark.parametrize(('field', 'value'), [
    ('application_id', 1), ('application_id', True), ('guild_id', None), ('guild_id', 1),
    ('channel_id', 1), ('user', None), ('user', NS(id=1, bot=False)),
    ('user', NS(id=int(OPERATOR), bot=True)), ('user', NS(id=int(OPERATOR))),
    ('data', None), ('data', {}), ('type', NS(value=1)), ('message', None),
])
def test_interaction_rejects_wrong_identity_and_incomplete_component(field, value):
    guard = ready_guard()
    value_interaction = interaction(guard)
    setattr(value_interaction, field, value)
    assert not guard.allowed_interaction(value_interaction)


@pytest.mark.parametrize(('field', 'value'), [
    ('author', NS(id=1, bot=True)), ('author', NS(id=int(APP), bot=False)),
    ('author', None), ('guild', NS(id=1)), ('channel', NS(id=1)), ('webhook_id', 1),
])
def test_component_message_must_belong_to_this_application_and_scope(field, value):
    guard = ready_guard()
    item = interaction(guard)
    setattr(item.message, field, value)
    assert not guard.allowed_interaction(item)


@pytest.mark.parametrize('custom_id', [
    'pallet_bot:mark_sold:1', f'pallet_bot:mark_sold:{FLOOR - 1}',
    'pallet_bot:mark_sold:2147483648', 'pallet_bot:mark_sold:-1',
    f'pallet_bot:mark_sold:0{FLOOR}', f'pallet_bot:unknown:{FLOOR}',
    f'pallet_bot:mark_sold:{FLOOR}:1', 'pallet_bot', '', None, 'a' * 101,
    'some_other_cog:delete', f'fgshop:approve:{FLOOR}:1',
])
def test_reserved_ids_and_old_inventory_do_not_dispatch(custom_id):
    guard = ready_guard()
    item = interaction(guard)
    item.data['custom_id'] = custom_id
    assert not guard.allowed_interaction(item)


@pytest.mark.parametrize('custom_id', [
    f'pallet_bot:mark_sold:{FLOOR}', 'pallet_bot:hold_resolved:2147483647',
    f'pallet_bot:confirm_ebay_listed:{FLOOR}', f'pallet_bot:confirm_fb_listed:{FLOOR}',
    '0123456789abcdef0123456789abcdef',
])
def test_new_inventory_and_discord_temporary_controls_are_allowed(custom_id):
    guard = ready_guard()
    item = interaction(guard)
    item.data['custom_id'] = custom_id
    assert guard.allowed_interaction(item)


@pytest.mark.parametrize('custom_id', [
    f'fgshop:approve:{FLOOR}:1', f'fgshop:form:{FLOOR}:2147483647',
    f'fgshop:details:{FLOOR}:5', f'fgshop:refresh:{FLOOR}:2',
])
def test_shop_controls_only_in_shop(custom_id):
    guard = ready_guard()
    item = interaction(guard)
    item.data['custom_id'] = custom_id
    item.channel_id = int(SHOP)
    item.message.channel.id = int(SHOP)
    assert guard.allowed_interaction(item)


@pytest.mark.parametrize('kind', [discord.InteractionType.application_command, discord.InteractionType.autocomplete,
                                  discord.InteractionType.modal_submit])
def test_commands_and_modals_may_have_no_source_message(kind):
    guard = ready_guard()
    item = interaction(guard)
    item.type, item.message = kind, None
    item.data = {'custom_id': 'a' * 32} if kind == discord.InteractionType.modal_submit else {'name': 'queue'}
    assert guard.allowed_interaction(item)


@pytest.mark.parametrize(('field', 'value'), [
    ('guild', None), ('guild', NS(id=1)), ('channel', NS(id=int(LISTED))),
    ('channel', NS(id=1)), ('author', NS(id=1, bot=False)), ('author', NS(id=int(OPERATOR), bot=True)),
    ('webhook_id', int(APP)), ('attachments', []), ('attachments', None),
])
def test_message_identity_and_channel_scope(field, value):
    guard = ready_guard()
    item = message()
    setattr(item, field, value)
    assert not guard.allowed_message(item)


@pytest.mark.parametrize(('field', 'value'), [
    ('size', True), ('size', 0), ('size', -1), ('size', guards.MAX_PHOTO_BYTES + 1),
    ('size', '100'), ('content_type', None), ('content_type', 'image/gif'),
    ('content_type', 'application/octet-stream'), ('filename', 'photo.exe'),
    ('filename', 'photo.png'), ('filename', '../photo.jpg'), ('filename', '..\\photo.jpg'),
    ('filename', 'photo\x00.jpg'), ('filename', ''), ('filename', None),
])
def test_attachment_metadata_is_bounded_before_download(field, value):
    guard = ready_guard()
    item = message()
    setattr(item.attachments[0], field, value)
    assert not guard.allowed_message(item)


def test_attachment_count_and_exact_size_limits():
    guard = ready_guard()
    item = message()
    item.attachments[0].size = guards.MAX_PHOTO_BYTES
    item.attachments *= 10
    assert guard.allowed_message(item)
    item.attachments.append(item.attachments[0])
    assert not guard.allowed_message(item)


def test_allowed_photos_and_case_insensitive_extension():
    guard = ready_guard()
    item = message()
    item.attachments = [NS(filename='a.JPG', content_type='image/jpeg', size=1),
                        NS(filename='b.png', content_type='image/png', size=1),
                        NS(filename='c.webp', content_type='image/webp', size=1)]
    assert guard.allowed_message(item)


def test_guard_runs_callbacks_and_reconciliation_under_same_lock_once():
    async def scenario():
        guard = ready_guard()
        item = interaction(guard)
        events = []

        async def after():
            assert guard.lock.locked()
            events.append('after')

        async def nested():
            assert guard.lock.locked()
            events.append('nested')
            return 7

        async def callback():
            events.append('outer')
            return await guard.run(item, nested)

        guard.after_action = after
        assert await guard.run(item, callback) == 7
        assert events == ['outer', 'nested', 'after']
        assert not guard.lock.locked()
    asyncio.run(scenario())


def test_denial_precedes_every_handler_and_reconciliation():
    async def scenario():
        guard = ready_guard()
        guard.ready = False

        async def forbidden():
            raise AssertionError('must never run')

        guard.after_action = forbidden
        assert await guard.run(interaction(guard), forbidden) is False
        assert guard.last_error == 'action_not_allowed'
        assert not guard.lock.locked()
    asyncio.run(scenario())


def test_waiting_action_rechecks_scope_after_shared_delivery_lock():
    async def scenario():
        guard = ready_guard()
        calls = []

        async def callback():
            calls.append('ran')

        await guard.lock.acquire()
        pending = asyncio.create_task(guard.run(interaction(guard), callback))
        await asyncio.sleep(0)
        assert not pending.done()
        guard.ready = False
        guard.lock.release()
        assert await pending is False
        assert calls == []
    asyncio.run(scenario())


def test_separate_child_tasks_do_not_inherit_reentrant_privileges():
    async def scenario():
        guard = ready_guard()
        events, children = [], []

        async def child():
            events.append('child')

        async def parent():
            children.append(asyncio.create_task(guard.run(interaction(guard), child)))
            await asyncio.sleep(0)
            assert not children[0].done()
            events.append('parent')

        await guard.run(interaction(guard), parent)
        await children[0]
        assert events == ['parent', 'child']
    asyncio.run(scenario())


def test_busy_delivery_lock_gives_bounded_notice_without_running_action(monkeypatch):
    monkeypatch.setattr(guards, 'LOCK_WAIT_SECONDS', 0.01)

    async def scenario():
        guard = ready_guard()
        item = interaction(guard)
        await guard.lock.acquire()

        async def forbidden():
            raise AssertionError('busy action must not run')

        try:
            assert await guard.run(item, forbidden) is False
            assert guard.last_error == 'action_busy'
            assert guard.lock.locked()  # Never release another worker's lock.
            assert 'another update' in item.response.notices[0][0]
        finally:
            guard.lock.release()
        assert not guard.lock.locked()
    asyncio.run(scenario())


def test_cancelled_waiter_never_releases_another_workers_lock():
    async def scenario():
        guard = ready_guard()
        await guard.lock.acquire()

        async def forbidden():
            raise AssertionError('cancelled action must not run')

        pending = asyncio.create_task(guard.run(interaction(guard), forbidden))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert guard.lock.locked()
        guard.lock.release()
    asyncio.run(scenario())


def test_errors_are_fixed_and_reconcile_even_after_partial_callback_failure(capsys):
    async def scenario():
        guard = ready_guard()
        item = interaction(guard)
        events = []

        async def callback():
            events.append('action')
            raise ValueError('TOKEN-DO-NOT-LOG')

        async def after():
            assert guard.lock.locked()
            events.append('after')

        guard.after_action = after
        assert await guard.run(item, callback) is False
        assert guard.last_error == 'action_failed'
        assert events == ['action', 'after']
        assert all('TOKEN' not in notice[0] for notice in item.response.notices)
        assert all(notice[1]['ephemeral'] for notice in item.response.notices)
    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert 'TOKEN' not in captured.out + captured.err


def test_failed_reconciliation_stops_new_work_and_releases_lock():
    async def scenario():
        guard = ready_guard()

        async def callback():
            return 3

        async def after():
            raise ValueError('PRIVATE-PAYLOAD')

        guard.after_action = after
        assert await guard.run(interaction(guard), callback) is False
        assert guard.ready is False
        assert guard.last_error == 'reconciliation_failed'
        assert not guard.lock.locked()
        assert guard._owner_task is None
    asyncio.run(scenario())


def test_cancellation_propagates_after_reconciliation_and_releases_lock():
    async def scenario():
        guard = ready_guard()
        started = asyncio.Event()
        events = []

        async def callback():
            started.set()
            await asyncio.Event().wait()

        async def after():
            events.append('after')

        guard.after_action = after
        pending = asyncio.create_task(guard.run(interaction(guard), callback))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert events == ['after']
        assert not guard.lock.locked()
        assert guard._owner_task is None
    asyncio.run(scenario())


@pytest.fixture
def dispatch_boundaries(monkeypatch):
    from discord.ui.view import BaseView, ViewStore
    from discord.ui.modal import Modal
    calls = []

    async def view(self, item, interaction):
        calls.append(('view', self, item, interaction))
        return 'view-result'

    async def modal(self, interaction, components, resolved):
        calls.append(('modal', self, interaction, components, resolved))
        return 'modal-result'

    async def dynamic(self, component_type, factory, interaction, custom_id, match):
        calls.append(('dynamic-factory-and-callback', self, component_type, factory, interaction, custom_id, match))
        return 'dynamic-result'

    monkeypatch.setattr(BaseView, '_scheduled_task', view)
    monkeypatch.setattr(Modal, '_scheduled_task', modal)
    monkeypatch.setattr(ViewStore, 'schedule_dynamic_item_call', dynamic)
    monkeypatch.setattr(guards, '_installed_wrappers', None)
    return BaseView, Modal, ViewStore, calls


def test_all_three_dispatch_boundaries_guard_before_original_work(dispatch_boundaries):
    BaseView, Modal, ViewStore, calls = dispatch_boundaries
    guards.install_dispatch_guards()

    async def scenario():
        guard = ready_guard()
        item = interaction(guard)
        item.user.id = 1
        owner, control, factory, match = NS(), object(), object(), object()
        assert await BaseView._scheduled_task(owner, control, item) is False
        assert await Modal._scheduled_task(owner, item, [], {}) is False
        assert await ViewStore.schedule_dynamic_item_call(owner, 2, factory, item, item.data['custom_id'], match) is False
        assert calls == []
        item.user.id = int(OPERATOR)
        assert await BaseView._scheduled_task(owner, control, item) == 'view-result'
        assert await Modal._scheduled_task(owner, item, [], {}) == 'modal-result'
        assert await ViewStore.schedule_dynamic_item_call(owner, 2, factory, item, item.data['custom_id'], match) == 'dynamic-result'
        assert [call[0] for call in calls] == ['view', 'modal', 'dynamic-factory-and-callback']
    asyncio.run(scenario())


def test_unmarked_clients_keep_original_behavior_and_arguments(dispatch_boundaries):
    BaseView, Modal, ViewStore, calls = dispatch_boundaries
    guards.install_dispatch_guards()

    async def scenario():
        item = interaction()
        item.client = NS()
        item.guild_id = 1
        owner, control, components, resolved, factory, match = object(), object(), [], {}, object(), object()
        assert await BaseView._scheduled_task(owner, control, item) == 'view-result'
        assert await Modal._scheduled_task(owner, item, components, resolved) == 'modal-result'
        assert await ViewStore.schedule_dynamic_item_call(owner, 2, factory, item, 'old-custom-id', match) == 'dynamic-result'
        assert calls[0] == ('view', owner, control, item)
        assert calls[1] == ('modal', owner, item, components, resolved)
        assert calls[2] == ('dynamic-factory-and-callback', owner, 2, factory, item, 'old-custom-id', match)
    asyncio.run(scenario())


def test_malformed_marked_client_and_old_modal_owner_fail_closed(dispatch_boundaries):
    BaseView, Modal, _, calls = dispatch_boundaries
    guards.install_dispatch_guards()

    async def scenario():
        item = interaction(None)
        assert await BaseView._scheduled_task(NS(), object(), item) is False
        guard = ready_guard()
        item = interaction(guard)
        item.type, item.message = discord.InteractionType.modal_submit, None
        item.data = {'custom_id': 'a' * 32}
        assert await Modal._scheduled_task(NS(item_id=1), item, [], {}) is False
        assert calls == []
    asyncio.run(scenario())


def test_install_is_idempotent_and_refuses_later_replacement(dispatch_boundaries, monkeypatch):
    BaseView, _, _, _ = dispatch_boundaries
    guards.install_dispatch_guards()
    wrapper = BaseView._scheduled_task
    guards.install_dispatch_guards()
    assert BaseView._scheduled_task is wrapper

    async def replacement(self, item, interaction):
        pass

    monkeypatch.setattr(BaseView, '_scheduled_task', replacement)
    with pytest.raises(guards.GuardSetupError, match='unsupported_discord_dispatch'):
        guards.install_dispatch_guards()


def test_wrong_discord_version_fails_before_any_patch(dispatch_boundaries, monkeypatch):
    BaseView, Modal, ViewStore, _ = dispatch_boundaries
    before = (BaseView._scheduled_task, Modal._scheduled_task, ViewStore.schedule_dynamic_item_call)
    monkeypatch.setattr(discord, '__version__', '2.8.0')
    with pytest.raises(guards.GuardSetupError):
        guards.install_dispatch_guards()
    assert before == (BaseView._scheduled_task, Modal._scheduled_task, ViewStore.schedule_dynamic_item_call)


def test_dispatch_signature_mismatch_fails_before_partial_patch(dispatch_boundaries, monkeypatch):
    BaseView, Modal, _, _ = dispatch_boundaries
    original = BaseView._scheduled_task

    async def incompatible(self, interaction):
        pass

    monkeypatch.setattr(Modal, '_scheduled_task', incompatible)
    with pytest.raises(guards.GuardSetupError):
        guards.install_dispatch_guards()
    assert BaseView._scheduled_task is original


def test_real_bot_constructor_is_offline_unready_and_has_only_guild_intents(monkeypatch):
    from combined_runtime import COGS, GuardedBot, GuardedTree
    from discord.http import HTTPClient
    request = AsyncMock(side_effect=AssertionError('network is forbidden'))
    monkeypatch.setattr(HTTPClient, 'request', request)

    async def scenario():
        bot = GuardedBot(settings())
        try:
            assert isinstance(bot.tree, GuardedTree)
            assert not bot.combined_guard.ready
            assert bot.combined_guard.lock is bot.workflow_lock
            assert bot._worker_task is None
            assert bot.store is bot.adapter is bot.journal is bot.delivery is None
            assert bot.intents == discord.Intents(guilds=True, guild_messages=True, message_content=True)
            assert COGS == ('cogs.item_flow', 'cogs.ebay', 'cogs.fb_marketplace')
            assert not bot.cogs
            assert bot.get_channel(1) is None
        finally:
            await bot.close()
        request.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize(('method', 'path', 'parameters'), [
    ('GET', '/channels/{channel_id}/messages', {'channel_id': 1}),
    ('GET', '/guilds/{guild_id}/channels', {'guild_id': 1}),
    ('DELETE', '/channels/{channel_id}/messages/{message_id}', {'channel_id': int(LISTED), 'message_id': 123}),
    ('PATCH', '/channels/{channel_id}/messages/{message_id}', {'channel_id': int(LISTED), 'message_id': 123}),
    ('PUT', '/channels/{channel_id}/pins/{message_id}', {'channel_id': int(LISTED), 'message_id': 123}),
    ('POST', '/channels/{channel_id}/messages/bulk-delete', {'channel_id': int(LISTED)}),
    ('POST', '/channels/{channel_id}/threads', {'channel_id': int(LISTED)}),
    ('POST', '/guilds/{guild_id}/channels', {'guild_id': int(GUILD)}),
    ('POST', '/guilds/{guild_id}/roles', {'guild_id': int(GUILD)}),
    ('DELETE', '/guilds/{guild_id}/roles/{role_id}', {'guild_id': int(GUILD), 'role_id': 123}),
    ('PATCH', '/users/@me', {}),
])
def test_http_boundary_blocks_unscoped_reads_and_destructive_writes(monkeypatch, method, path, parameters):
    from combined_runtime import GuardedBot
    from combined_settings import SetupError
    from discord.http import HTTPClient, Route
    request = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(HTTPClient, 'request', request)

    async def scenario():
        bot = GuardedBot(settings())
        try:
            with pytest.raises(SetupError):
                await bot.http.request(Route(method, path, **parameters))
        finally:
            await bot.close()
        request.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize(('method', 'path', 'parameters'), [
    ('GET', '/oauth2/applications/@me', {}),
    ('GET', '/channels/{channel_id}/messages', {'channel_id': int(LISTED)}),
    ('POST', '/channels/{channel_id}/messages', {'channel_id': int(SHOP)}),
])
def test_http_boundary_preserves_allowed_reads_and_append_only_posts(monkeypatch, method, path, parameters):
    from combined_runtime import GuardedBot
    from discord.http import HTTPClient, Route
    request = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(HTTPClient, 'request', request)

    async def scenario():
        bot = GuardedBot(settings())
        try:
            route = Route(method, path, **parameters)
            assert await bot.http.request(route) == {'ok': True}
            request.assert_awaited_once_with(route)
        finally:
            await bot.close()
    asyncio.run(scenario())


def test_setup_hook_composes_scoped_services_without_connecting_or_loading_extra_cogs(tmp_path, monkeypatch):
    import combined_runtime as runtime
    import combined_intake
    import combined_delivery
    import shop_approval
    import publisher.journal
    import publisher.transport

    scoped = settings()
    scoped.channels['sold'] = '100000000000000007'
    scoped.state_directory = tmp_path
    scoped.source_id = '12345678123442348234123456781234'
    scoped.website = 'https://shop.example.com'
    scoped.secret = 'not-a-live-key'
    store, journal, delivery = Mock(), Mock(), Mock()
    adapter = NS(reconcile=AsyncMock(), resolve_content=AsyncMock(), lifecycle=AsyncMock(), close=Mock())
    store_factory, adapter_factory, journal_factory = Mock(return_value=store), Mock(return_value=adapter), Mock(return_value=journal)
    delivery_factory = Mock(return_value=delivery)
    transport_factory = Mock(side_effect=AssertionError('preview must not create transport'))
    check, install = Mock(), Mock()
    monkeypatch.setattr(runtime, 'check_state', check)
    monkeypatch.setattr(runtime, 'install_dispatch_guards', install)
    monkeypatch.setattr(shop_approval, 'ShopReviewStore', store_factory)
    monkeypatch.setattr(combined_intake, 'IntakeAdapter', adapter_factory)
    monkeypatch.setattr(combined_delivery, 'DeliveryService', delivery_factory)
    monkeypatch.setattr(publisher.journal, 'Journal', journal_factory)
    monkeypatch.setattr(publisher.transport, 'Transport', transport_factory)

    async def scenario():
        bot = runtime.GuardedBot(scoped)
        bot._connection.user = NS(id=int(APP), bot=True)
        bot.application_info = AsyncMock(return_value=NS(id=int(APP)))
        bot.load_extension = AsyncMock()
        try:
            await bot.setup_hook()
            check.assert_called_once_with(scoped)
            install.assert_called_once_with()
            assert bot.combined_guard.after_action is adapter.reconcile
            assert [args.args[0] for args in bot.load_extension.await_args_list] == list(runtime.COGS)
            store_factory.assert_called_once_with(tmp_path / 'shop.sqlite', guild_id=GUILD,
                shop_channel_id=SHOP, operator_ids=(OPERATOR,))
            adapter_factory.assert_called_once_with(bot, scoped, store)
            journal_factory.assert_called_once_with(tmp_path / 'publisher.sqlite', scoped.source_id,
                GUILD, scoped.website, APP, LISTED, scoped.channels['sold'])
            delivery_factory.assert_called_once_with(store, journal, None, adapter.resolve_content,
                adapter.lifecycle, enabled=False, max_deliveries=1)
            assert bot.tree.get_command('website') is not None
            assert not bot.combined_guard.ready
            assert bot._worker_task is None
            transport_factory.assert_not_called()
        finally:
            await bot.close()
        store.close.assert_called_once()
        journal.close.assert_called_once()
        adapter.close.assert_called_once()
    asyncio.run(scenario())


@pytest.mark.parametrize(('application_id', 'user_id'), [(1, int(APP)), (int(APP), 1)])
def test_setup_hook_rejects_wrong_application_before_opening_local_services(monkeypatch, application_id, user_id):
    import combined_runtime as runtime
    from combined_settings import SetupError
    install = Mock(side_effect=AssertionError('identity must be checked before dispatch setup'))
    monkeypatch.setattr(runtime, 'check_state', Mock())
    monkeypatch.setattr(runtime, 'install_dispatch_guards', install)

    async def scenario():
        bot = runtime.GuardedBot(settings())
        bot._connection.user = NS(id=user_id, bot=True)
        bot.application_info = AsyncMock(return_value=NS(id=application_id))
        try:
            with pytest.raises(SetupError):
                await bot.setup_hook()
            assert bot.store is bot.adapter is bot.journal is bot.delivery is None
            install.assert_not_called()
        finally:
            await bot.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(('sync', 'application_id', 'guild_id', 'allowed'), [
    (False, APP, GUILD, False), (True, APP, GUILD, True),
    (True, '100000000000000099', GUILD, False), (True, APP, '100000000000000099', False),
])
def test_command_sync_only_explicit_and_exact_application_guild(monkeypatch, sync, application_id, guild_id, allowed):
    from combined_runtime import GuardedBot
    from combined_settings import SetupError
    from discord.http import HTTPClient, Route
    request = AsyncMock(return_value=[])
    monkeypatch.setattr(HTTPClient, 'request', request)

    async def scenario():
        bot = GuardedBot(settings(), sync_commands=sync)
        route = Route('PUT', '/applications/{application_id}/guilds/{guild_id}/commands',
                      application_id=int(application_id), guild_id=int(guild_id))
        try:
            if allowed:
                assert await bot.http.request(route) == []
                request.assert_awaited_once()
            else:
                with pytest.raises(SetupError):
                    await bot.http.request(route)
                request.assert_not_awaited()
        finally:
            await bot.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [None, 'administrator', 'missing_channel', 'missing_permission', 'missing_role'])
def test_on_ready_checks_permissions_before_restoring_views_or_starting_worker(monkeypatch, failure):
    from combined_runtime import GuardedBot
    from combined_settings import SetupError

    async def scenario():
        scoped = settings()
        scoped.role_ids = {'Pallet Admin': '100000000000000009'}
        bot = GuardedBot(scoped)
        permissions = NS(view_channel=True, read_message_history=True, send_messages=True,
                         embed_links=True, attach_files=failure != 'missing_permission')
        guild = NS(id=int(GUILD), me=NS(guild_permissions=NS(administrator=failure == 'administrator')),
                   get_role=lambda role_id: None if failure == 'missing_role' else NS(id=role_id))
        channels = {}
        for channel_id in scoped.channels.values():
            channel = Mock(spec=discord.TextChannel)
            channel.id, channel.guild = int(channel_id), guild
            channel.permissions_for = Mock(return_value=permissions)
            channels[channel.id] = channel
        if failure == 'missing_channel':
            channels.pop(int(SHOP))
        bot.get_guild = lambda guild_id: guild
        bot._connection.get_channel = channels.get
        adapter = NS(restore_views=AsyncMock(), close=Mock())
        bot.adapter = adapter
        worker = AsyncMock()
        bot._worker = worker
        bot.tree.sync = AsyncMock()
        bot.tree.copy_global_to = Mock()
        try:
            if failure:
                with pytest.raises(SetupError):
                    await bot.on_ready()
                assert not bot.combined_guard.ready
                adapter.restore_views.assert_not_awaited()
                worker.assert_not_awaited()
            else:
                await bot.on_ready()
                await asyncio.sleep(0)
                assert bot.combined_guard.ready
                adapter.restore_views.assert_awaited_once()
                worker.assert_awaited_once()
            bot.tree.sync.assert_not_awaited()
            bot.tree.copy_global_to.assert_not_called()
        finally:
            await bot.close()
    asyncio.run(scenario())
