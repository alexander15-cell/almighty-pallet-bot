"""Owner-enabled no-password sign-in by Discord username (WEB_USERNAME_LOGIN)."""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import config
from webapp import auth
from webapp import server
from tests.test_web_dashboard import _Guild, _Member, env, run  # noqa: F401 (fixture)


class _NamedGuild(_Guild):
    """discord.py's guild.members is a list; members here also have usernames."""

    def __init__(self):
        self._by_id = {}
        super().__init__()

    @property
    def members(self):
        return list(self._by_id.values())

    @members.setter
    def members(self, value):
        self._by_id = dict(value)

    def add(self, uid, *role_names, name=None):
        member = _Member(uid, [self.role(n) for n in role_names], self)
        member.name = name or f"user{uid}"
        self._by_id[uid] = member

    def get_member(self, uid):
        return self._by_id.get(uid)

    async def query_members(self, query, limit=10):
        return []


def _post(c, username):
    return c.post("/login/username", data={"username": username}, allow_redirects=False)


def test_username_sign_in_uses_that_members_current_roles(env, monkeypatch):
    env.guild = env.bot.guild = _NamedGuild()
    env.guild.add(7, config.ROLE_FINANCE_MGMT, name="alex15")
    env.guild.add(8, name="noroles")
    server._login_tries.clear()

    async def scenario(c):
        assert (await _post(c, "alex15")).status == 404  # off unless the owner turns it on
        monkeypatch.setattr(config, "WEB_USERNAME_LOGIN", True)
        assert "Your Discord username" in await (await c.get("/login")).text()
        assert (await _post(c, "nobody")).status == 403
        assert (await _post(c, "noroles")).status == 403
        r = await _post(c, "@Alex15")  # case and a leading @ don't matter
        assert r.status == 302 and r.headers["Location"] == "/"
        token = r.cookies[auth.SESSION_COOKIE].value
        assert env.db.get_web_session(auth._hash(token))["discord_user_id"] == 7
        assert (await c.get("/finance")).status == 200
        r = await c.post("/login/username", data={"username": "alex15"}, headers={"Origin": "https://evil.test"})
        assert r.status == 403
    run(env, scenario)


def test_username_guessing_is_rate_limited(env, monkeypatch):
    env.guild = env.bot.guild = _NamedGuild()
    monkeypatch.setattr(config, "WEB_USERNAME_LOGIN", True)
    monkeypatch.setattr(server, "LOGIN_TRIES", 3)
    server._login_tries.clear()

    async def scenario(c):
        assert [(await _post(c, f"guess{n}")).status for n in range(4)] == [403, 403, 403, 429]
    run(env, scenario)
