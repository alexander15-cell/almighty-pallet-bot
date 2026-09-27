"""Fail-closed interaction dispatch for the explicitly scoped combined bot.

Only clients marked with ``combined_guard`` are affected by the pinned Discord
dispatch wrappers. Importing this module does not patch, connect, or read state.
The same lock must also be used by message intake and website delivery.
"""
from __future__ import annotations

import asyncio
from functools import wraps
import inspect
from pathlib import PurePath
import re

import discord

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_ITEM_ID = 2147483647
LOCK_WAIT_SECONDS = 1.5
_PHOTO_EXTENSIONS = {
    'image/jpeg': {'.jpg', '.jpeg'}, 'image/png': {'.png'}, 'image/webp': {'.webp'},
}
_INTAKE_ACTIONS = frozenset({
    'qr_approve', 'qr_edit', 'qr_rereview', 'qr_reject', 'ebay_batch',
    'fb_marketplace_batch', 'ebay_api_list', 'mark_listed', 'mark_sold',
    'mark_shipped', 'confirm_ebay_listed', 'confirm_fb_listed', 'hold_resolved',
})
_SHOP_ACTIONS = frozenset({'details', 'approve', 'refresh', 'form'})
_BLOCKED = 'This action is unavailable. Use the newest item card in an approved workflow channel.'
_FAILED = 'This action could not be completed. Check the newest item card before trying again.'
_BUSY = 'The bot is finishing another update. Please try this button or command again in a moment.'
_installed_wrappers = None


class GuardSetupError(RuntimeError):
    def __init__(self):
        super().__init__('unsupported_discord_dispatch')
        self.code = 'unsupported_discord_dispatch'


def _id(value):
    if type(value) is int and 0 < value < 2**64:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value) and int(value) < 2**64:
        return value
    return None


class RuntimeGuard:
    """Identity checks first; state-changing work serialized with delivery.

    ``after_action`` is a zero-argument async reconciliation callback. It runs
    once per outermost permitted action, including after callback failures.
    Reconciliation failure stops further work by clearing ``ready``.
    """

    def __init__(self, settings, lock):
        self.settings = settings
        self.lock = lock
        self.ready = False
        self.after_action = None
        self.last_error = None
        self._owner_task = None

    def _scope(self, guild_id, channel_id, user):
        return (
            self.ready is True
            and _id(guild_id) == self.settings.guild_id
            and _id(channel_id) in self.settings.channels.values()
            and user is not None and getattr(user, 'bot', None) is False
            and _id(getattr(user, 'id', None)) in self.settings.operator_ids
        )

    def _owned_item(self, value):
        return (isinstance(value, (str, int)) and not isinstance(value, bool)
                and re.fullmatch(r'[1-9][0-9]{0,9}', str(value)) is not None
                and self.settings.item_id_floor <= int(value) <= MAX_ITEM_ID)

    def _custom_id(self, value, channel_id):
        if not isinstance(value, str) or not 1 <= len(value) <= 100:
            return False
        parts = value.split(':')
        if parts[0] == 'pallet_bot':
            return len(parts) == 3 and parts[1] in _INTAKE_ACTIONS and self._owned_item(parts[2])
        if parts[0] == 'fgshop':
            return (len(parts) == 4 and parts[1] in _SHOP_ACTIONS
                    and self._owned_item(parts[2])
                    and re.fullmatch(r'[1-9][0-9]{0,9}', parts[3]) is not None
                    and int(parts[3]) <= MAX_ITEM_ID
                    and _id(channel_id) == self.settings.channels['website_shop'])
        # discord.py generates these IDs for temporary selects and modals.
        # Unrecognised persistent component namespaces are not authorized.
        return re.fullmatch(r'[0-9a-f]{32}', value) is not None

    def allowed_interaction(self, interaction):
        if (_id(getattr(interaction, 'application_id', None)) != self.settings.application_id
                or not self._scope(getattr(interaction, 'guild_id', None),
                                   getattr(interaction, 'channel_id', None),
                                   getattr(interaction, 'user', None))):
            return False
        message = getattr(interaction, 'message', None)
        if message is not None:
            author = getattr(message, 'author', None)
            if (getattr(author, 'bot', None) is not True
                    or _id(getattr(author, 'id', None)) != self.settings.application_id):
                return False
            for actual, expected in (
                (getattr(getattr(message, 'guild', None), 'id', None), self.settings.guild_id),
                (getattr(getattr(message, 'channel', None), 'id', None), _id(interaction.channel_id)),
                (getattr(message, 'webhook_id', None), self.settings.application_id),
            ):
                if actual is not None and _id(actual) != expected:
                    return False
        data = getattr(interaction, 'data', None)
        kind = getattr(getattr(interaction, 'type', None), 'value', None)
        if not isinstance(data, dict) or kind not in {2, 3, 4, 5}:
            return False
        if kind in {3, 5}:
            if kind == 3 and message is None:
                return False
            return self._custom_id(data.get('custom_id'), interaction.channel_id)
        return 'custom_id' not in data  # Application commands and autocomplete only.

    def allowed_message(self, message):
        if (not self._scope(getattr(getattr(message, 'guild', None), 'id', None),
                            getattr(getattr(message, 'channel', None), 'id', None),
                            getattr(message, 'author', None))
                or _id(getattr(getattr(message, 'channel', None), 'id', None)) != self.settings.channels['data-entry']
                or getattr(message, 'webhook_id', None) is not None):
            return False
        photos = getattr(message, 'attachments', None)
        if not isinstance(photos, (list, tuple)) or not 1 <= len(photos) <= 10:
            return False
        for photo in photos:
            size = getattr(photo, 'size', None)
            mime = getattr(photo, 'content_type', None)
            name = getattr(photo, 'filename', None)
            if (type(size) is not int or not 1 <= size <= MAX_PHOTO_BYTES
                    or not isinstance(mime, str) or mime not in _PHOTO_EXTENSIONS
                    or not isinstance(name, str) or not 1 <= len(name) <= 255
                    or any(ch in name for ch in '/\\')
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
                    or PurePath(name).suffix.lower() not in _PHOTO_EXTENSIONS[mime]):
                return False
        return True

    async def _notice(self, interaction, text):
        # Never interpolate user input, exception messages, tokens or payloads.
        try:
            response = interaction.response
            if not response.is_done():
                await response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            pass

    async def run(self, interaction, callback):
        if not self.allowed_interaction(interaction):
            self.last_error = 'action_not_allowed'
            await self._notice(interaction, _BLOCKED)
            return False
        task = asyncio.current_task()
        if self._owner_task is task:
            # A child task must still acquire the lock: only this exact task is
            # reentrant, not a copied context inherited by asyncio.create_task.
            return await callback()
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=LOCK_WAIT_SECONDS)
        except asyncio.TimeoutError:
            self.last_error = 'action_busy'
            await self._notice(interaction, _BUSY)
            return False
        try:
            if not self.allowed_interaction(interaction):
                self.last_error = 'action_not_allowed'
                await self._notice(interaction, _BLOCKED)
                return False
            self._owner_task = task
            failed = False
            result = None
            self.last_error = None
            try:
                try:
                    result = await callback()
                except Exception:
                    self.last_error = 'action_failed'
                    failed = True
                finally:
                    if self.after_action is not None:
                        try:
                            await self.after_action()
                        except Exception:
                            self.ready = False
                            self.last_error = 'reconciliation_failed'
                            failed = True
            finally:
                self._owner_task = None
            if failed:
                await self._notice(interaction, _FAILED)
                return False
            return result
        finally:
            self.lock.release()


async def _dispatch(interaction, callback, *, owner=None):
    client = interaction.client
    if not hasattr(client, 'combined_guard'):
        return await callback()
    guard = client.combined_guard
    if not isinstance(guard, RuntimeGuard):
        return False  # A malformed marker must never bypass scope protection.
    # Ephemeral selects/modal IDs are random; their attached item still needs
    # to belong to the newly isolated inventory, never an old restored item.
    value = getattr(owner, 'item_id', None)
    if value is not None and not guard._owned_item(value):
        return False
    return await guard.run(interaction, callback)


def install_dispatch_guards():
    """Install the three inspected discord.py 2.7.1 dispatch boundaries once."""
    global _installed_wrappers
    from discord.ui.view import BaseView, ViewStore
    from discord.ui.modal import Modal

    targets = (
        (BaseView, '_scheduled_task', ('self', 'item', 'interaction')),
        (Modal, '_scheduled_task', ('self', 'interaction', 'components', 'resolved')),
        (ViewStore, 'schedule_dynamic_item_call', ('self', 'component_type', 'factory', 'interaction', 'custom_id', 'match')),
    )
    if discord.__version__ != '2.7.1':
        raise GuardSetupError()
    if _installed_wrappers is not None:
        if any(getattr(cls, name) is not wrapper for (cls, name, _), wrapper in zip(targets, _installed_wrappers)):
            raise GuardSetupError()
        return
    originals = tuple(getattr(cls, name) for cls, name, _ in targets)
    for original, (_, _, expected) in zip(originals, targets):
        if not inspect.iscoroutinefunction(original) or tuple(inspect.signature(original).parameters) != expected:
            raise GuardSetupError()

    @wraps(originals[0])
    async def view_task(self, item, interaction):
        return await _dispatch(interaction, lambda: originals[0](self, item, interaction), owner=self)

    @wraps(originals[1])
    async def modal_task(self, interaction, components, resolved):
        return await _dispatch(interaction, lambda: originals[1](self, interaction, components, resolved), owner=self)

    @wraps(originals[2])
    async def dynamic_task(self, component_type, factory, interaction, custom_id, match):
        # Guard BEFORE from_custom_id or any component state refresh runs.
        return await _dispatch(interaction, lambda: originals[2](self, component_type, factory, interaction, custom_id, match))

    wrappers = (view_task, modal_task, dynamic_task)
    for (cls, name, _), wrapper in zip(targets, wrappers):
        setattr(cls, name, wrapper)
    _installed_wrappers = wrappers
