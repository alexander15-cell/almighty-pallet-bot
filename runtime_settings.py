"""
Optional role-ID bindings, editable from Discord without restarting the
bot or touching .env - stored in data/settings.json.

Every role check in this bot normally matches by role NAME (config.ROLE_*,
e.g. "Pallet Admin") via discord.utils.get(guild.roles, name=...). That's
simple and needs no setup, but breaks the moment someone renames the role
in Discord. Binding a role's ID here (via /admin bind-role) makes that
check resilient to a rename - resolve_role() below prefers the bound ID
and only falls back to name-matching when nothing's bound. This is always
optional: an unconfigured bot behaves exactly as if this module didn't
exist, never a hard requirement at startup (unlike a raw PALLET_ROLE_IDS
env var that has to be filled in correctly before the bot will even run).
"""
import json
import logging
from pathlib import Path

import discord

import config

log = logging.getLogger(__name__)

_SETTINGS_PATH = Path(config.SETTINGS_PATH)

# In-memory cache, loaded once at import time and kept in sync by
# set_role_id()/clear_role_id() - avoids re-reading the file on every single
# permission check, which happens on nearly every command.
_role_ids: dict = {}


def _load() -> dict:
    if not _SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return data.get("role_ids", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Couldn't read {_SETTINGS_PATH}, ignoring role bindings: {e}")
        return {}


def _save():
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _SETTINGS_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps({"role_ids": _role_ids}, indent=2), encoding="utf-8")
    temp_path.replace(_SETTINGS_PATH)  # atomic on POSIX - never leaves a half-written settings.json


_role_ids = _load()


def set_role_id(role_name: str, role_id: int):
    _role_ids[role_name] = role_id
    _save()


def clear_role_id(role_name: str):
    _role_ids.pop(role_name, None)
    _save()


def get_bindings() -> dict:
    """A copy, so callers can't accidentally mutate the live cache."""
    return dict(_role_ids)


def resolve_role(guild: discord.Guild, role_name: str):
    """
    The one place every permission check in this bot should look up a role
    by its config name - prefers a bound ID (survives a rename), falls back
    to matching by name (today's default, and what happens for any role
    that's never been bound).
    """
    role_id = _role_ids.get(role_name)
    if role_id:
        role = guild.get_role(role_id)
        if role:
            return role
        # The bound ID no longer resolves (role deleted) - fall back to
        # name-matching rather than leaving the bot unable to recognize
        # this role name at all until someone re-binds it.
    return discord.utils.get(guild.roles, name=role_name)
