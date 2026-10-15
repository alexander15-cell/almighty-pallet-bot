"""
Sign in with Discord, sessions, and role checks for the web dashboard.

Sign-in is Discord's own OAuth2 "identify" flow (no password ever touches
this bot): /login sends the browser to Discord, Discord sends it back to
/oauth/callback with a one-time code, the bot swaps the code for the user's
Discord ID, and checks that ID is a member of this bot's server holding at
least one of its roles. Permissions are then looked up LIVE from that
member's Discord roles on every request (runtime_settings.resolve_role,
same as every Discord button) - take someone's role away in Discord and
the website follows immediately.

Sessions: a random token in an HttpOnly, SameSite=Lax cookie; only its
SHA-256 is stored (web_sessions), so a copy of the database can't be used
to sign in. Each session also has its own CSRF token that every form/POST
must echo back.
"""
import hashlib
import logging
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
import discord

import config
import database as db
import runtime_settings

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"
SESSION_COOKIE = "apb_session"
STATE_COOKIE = "apb_oauth_state"
SESSION_DAYS = 30

# Every role this bot knows - holding any one of them is what lets someone
# sign in at all.
ALL_ROLES = (config.ROLE_DATA_ENTRY, config.ROLE_QUEUE_REVIEW, config.ROLE_LISTING_MGMT,
             config.ROLE_PURCHASE_MGMT, config.ROLE_FINANCE_MGMT, config.ROLE_ADMIN)


def redirect_uri() -> str:
    return config.WEB_BASE_URL.rstrip("/") + "/oauth/callback"


def authorize_url(state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": config.DISCORD_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": "identify", "state": state, "prompt": "none",
    })
    return f"https://discord.com/oauth2/authorize?{query}"


async def exchange_code(code: str) -> dict:
    """The Discord user ({'id', 'username', 'global_name', 'avatar'}) a
    one-time OAuth code belongs to. Raises PermissionError on any failure."""
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "client_id": config.DISCORD_CLIENT_ID, "client_secret": config.DISCORD_CLIENT_SECRET}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{DISCORD_API}/oauth2/token", data=data,
                                headers={"Content-Type": "application/x-www-form-urlencoded"}) as resp:
            token = await resp.json(content_type=None)
            if resp.status >= 300 or not (token or {}).get("access_token"):
                raise PermissionError(f"Discord didn't accept the sign-in ({resp.status}).")
        async with session.get(f"{DISCORD_API}/users/@me",
                               headers={"Authorization": f"Bearer {token['access_token']}"}) as resp:
            user = await resp.json(content_type=None)
            if resp.status >= 300 or not (user or {}).get("id"):
                raise PermissionError(f"Couldn't read your Discord account ({resp.status}).")
    return user


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_state() -> str:
    return secrets.token_urlsafe(24)


def create_session(discord_user: dict) -> str:
    """Starts a session; returns the raw cookie token (only its hash is stored)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.create_web_session(_hash(token), int(discord_user["id"]),
                          discord_user.get("global_name") or discord_user.get("username") or "?",
                          secrets.token_urlsafe(24), expires.isoformat())
    return token


def load_session(token: str):
    if not token:
        return None
    session = db.get_web_session(_hash(token))
    if not session:
        return None
    if session["expires_at"] < datetime.now(timezone.utc).isoformat():
        db.delete_web_session(_hash(token))
        return None
    return session


def end_session(token: str):
    if token:
        db.delete_web_session(_hash(token))


@dataclass
class WebUser:
    """Who's signed in, with their CURRENT Discord roles (config names)."""
    discord_id: int
    name: str
    csrf: str
    roles: set = field(default_factory=set)
    member: object = None

    @property
    def is_admin(self) -> bool:
        return config.ROLE_ADMIN in self.roles

    def has(self, *role_names) -> bool:
        """True for a Pallet Admin, or anyone holding one of role_names -
        the same rule every Discord button uses."""
        return self.is_admin or any(r in self.roles for r in role_names)


async def member_for(bot, discord_id: int):
    """The guild member for a Discord user ID, or None if they're not in
    the server (or the bot can't see it yet)."""
    guild = bot.get_guild(config.GUILD_ID) if bot else None
    if guild is None:
        return None
    member = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return member


def role_names_for(member) -> set:
    """Which of this bot's roles (by config name) a member holds - resolved
    with runtime_settings.resolve_role, so bound/renamed roles work the same
    as in Discord."""
    if member is None:
        return set()
    held = {r.id for r in getattr(member, "roles", [])}
    names = set()
    for name in ALL_ROLES:
        role = runtime_settings.resolve_role(member.guild, name)
        if role is not None and role.id in held:
            names.add(name)
    return names


async def user_for_session(bot, session: dict):
    member = await member_for(bot, session["discord_user_id"])
    roles = role_names_for(member)
    if not roles:
        return None  # left the server or lost every bot role - signed out on the spot
    name = getattr(member, "display_name", None) or session["display_name"]
    return WebUser(session["discord_user_id"], name, session["csrf_token"], roles, member)


def check_csrf(user: WebUser, supplied: str) -> bool:
    return bool(user and supplied) and secrets.compare_digest(user.csrf, supplied)
