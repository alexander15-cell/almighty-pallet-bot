"""
The web dashboard (webapp/) end to end against a fake Discord guild: sign-in
and role checks, CSRF, photo safety, and that each page action calls the
same shared function the Discord button does.
"""
import asyncio
import io
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import config
import database as db
import runtime_settings
from webapp import auth
from webapp.server import create_app

BASE = "http://dash.test"


class _Role:
    def __init__(self, rid, name):
        self.id, self.name = rid, name


class _Member:
    def __init__(self, uid, roles, guild):
        self.id, self.roles, self.guild, self.display_name = uid, roles, guild, f"User{uid}"


class _Guild:
    def __init__(self):
        self.roles = [_Role(i + 1, name) for i, name in enumerate(auth.ALL_ROLES)]
        self.members = {}

    def role(self, name):
        return next(r for r in self.roles if r.name == name)

    def add(self, uid, *role_names):
        self.members[uid] = _Member(uid, [self.role(n) for n in role_names], self)

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        import discord
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "no member")


class _Bot:
    def __init__(self, guild, flow=None):
        self.guild, self.flow = guild, flow

    def get_guild(self, gid):
        return self.guild

    def get_cog(self, name):
        return self.flow if name == "ItemFlow" else None

    def get_channel(self, cid):
        return None


@pytest.fixture
def env(fresh_db, monkeypatch):
    for key, value in {"WEB_BASE_URL": BASE, "DISCORD_CLIENT_ID": "cid", "DISCORD_CLIENT_SECRET": "secret",
                       "GUILD_ID": 1, "PRESERVE_DISCORD_HISTORY": False}.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(runtime_settings, "resolve_role",
                        lambda guild, name: next((r for r in guild.roles if r.name == name), None))
    guild = _Guild()
    flow = SimpleNamespace()
    bot = _Bot(guild, flow)
    monkeypatch.setattr("finance_utils.refresh_finance_message", AsyncMock())
    monkeypatch.setattr("finance_utils.post_to_finance_audit_log", AsyncMock())
    return SimpleNamespace(guild=guild, flow=flow, bot=bot, db=fresh_db)


def run(env, scenario):
    async def go():
        client = TestClient(TestServer(create_app(env.bot)))
        await client.start_server()
        try:
            return await scenario(client)
        finally:
            await client.close()
    return asyncio.run(go())


def sign_in(env, client, uid=42, *roles):
    env.guild.add(uid, *roles)
    token = auth.create_session({"id": str(uid), "username": f"u{uid}"})
    client.session.cookie_jar.update_cookies({auth.SESSION_COOKIE: token})
    session = auth.load_session(token)
    return session["csrf_token"]


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(buf, "JPEG")
    return buf.getvalue()


def _item(db_, status=None, pallet_name="Pallets 1-6"):
    pallet_id = db_.create_pallet(pallet_name, category_id=len(pallet_name), created_by=1)
    item_id = db_.create_item(pallet_id, "cordless drill", [], 1)
    if status:
        db_.update_status(item_id, status, actor_id=1)
    return db_.get_item(item_id)


# ------------------------------------------------------------------ sign-in

def test_signed_out_goes_to_login_and_public_pages_work(env):
    async def scenario(c):
        r = await c.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/login"
        assert (await c.get("/health")).status == 200
        r = await c.get("/login")
        assert "Sign in with Discord" in await r.text()
        r = await c.get("/login?go=1", allow_redirects=False)
        assert r.headers["Location"].startswith("https://discord.com/oauth2/authorize?")
        from urllib.parse import unquote
        assert "redirect_uri=http://dash.test/oauth/callback" in unquote(r.headers["Location"])
        assert "scope=identify" in r.headers["Location"]
    run(env, scenario)


def test_callback_rejects_a_bad_state(env):
    async def scenario(c):
        c.session.cookie_jar.update_cookies({auth.STATE_COOKIE: "right"})
        r = await c.get("/oauth/callback?state=wrong&code=x")
        assert r.status == 400
    run(env, scenario)


def test_callback_signs_in_a_member_with_a_role_and_refuses_others(env, monkeypatch):
    monkeypatch.setattr(auth, "exchange_code", AsyncMock(return_value={"id": "42", "username": "alex"}))

    async def scenario(c):
        c.session.cookie_jar.update_cookies({auth.STATE_COOKIE: "s1"})
        r = await c.get("/oauth/callback?state=s1&code=abc")
        assert r.status == 403  # not in the guild yet
        env.guild.add(42, config.ROLE_DATA_ENTRY)
        r = await c.get("/oauth/callback?state=s1&code=abc", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/"
        token = r.cookies[auth.SESSION_COOKIE].value
        assert env.db.get_web_session(auth._hash(token))["discord_user_id"] == 42
        assert token not in str(env.db.get_web_session(auth._hash(token)))  # only the hash is stored
    run(env, scenario)


def test_losing_every_role_in_discord_signs_you_out(env):
    async def scenario(c):
        sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        assert (await c.get("/", allow_redirects=False)).status == 200
        env.guild.members[42].roles = []
        assert (await c.get("/", allow_redirects=False)).status == 302
    run(env, scenario)


def test_pages_follow_discord_roles(env):
    async def scenario(c):
        sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        home = await (await c.get("/")).text()
        assert "/intake" in home and "/finance" not in home
        assert (await c.get("/finance")).status == 403
        assert (await c.get("/review")).status == 403
        assert (await c.get("/inventory")).status == 200
        env.guild.add(42, config.ROLE_ADMIN)  # Pallet Admin can do everything, like in Discord
        assert (await c.get("/finance")).status == 200
    run(env, scenario)


def test_security_headers(env):
    async def scenario(c):
        sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        r = await c.get("/")
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    run(env, scenario)


# --------------------------------------------------------------------- CSRF

def test_posts_need_the_csrf_token_and_same_origin(env):
    item = _item(env.db, env.db.STATUS_SOLD)
    env.flow.do_mark_shipped = AsyncMock()

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_LISTING_MGMT)
        r = await c.post(f"/items/{item['id']}/mark-shipped", data={"csrf": "nope"})
        assert r.status == 403
        r = await c.post(f"/items/{item['id']}/mark-shipped", data={"csrf": csrf},
                         headers={"Origin": "https://evil.example"})
        assert r.status == 403
        assert env.flow.do_mark_shipped.await_count == 0
        r = await c.post(f"/items/{item['id']}/mark-shipped", data={"csrf": csrf}, allow_redirects=False)
        assert r.status == 303
        env.flow.do_mark_shipped.assert_awaited_once_with(item["id"], 42)
        # multipart is only self-checked on /intake - anywhere else it needs the token too
        form = FormData()
        form.add_field("x", "1")
        form.add_field("f", b"data", filename="a.txt")
        r = await c.post(f"/items/{item['id']}/mark-shipped", data=form)
        assert r.status == 403
    run(env, scenario)


def test_logout_needs_csrf_and_ends_the_session(env):
    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        r = await c.post("/logout", data={"csrf": csrf}, allow_redirects=False)
        assert r.status == 302
        assert (await c.get("/", allow_redirects=False)).status == 302
    run(env, scenario)


# ------------------------------------------------------------------- intake

def test_intake_logs_items_through_the_shared_function(env):
    pallet_id = env.db.create_pallet("Pallets 1-6", category_id=5, created_by=1)
    calls = {}

    async def ingest(pallet, note, photos, actor, *, quantity=1, resubmit_item=None, before_announce=None, **kw):
        calls.update(pallet=pallet, note=note, photos=photos, actor=actor, quantity=quantity, resubmit=resubmit_item)
        ids = [env.db.create_item(pallet, note, [], actor) for _ in range(quantity)]
        await before_announce(ids)
        await asyncio.sleep(0)  # the AI-review part carries on after the page answers
        return ids
    env.flow.ingest_data_entry = ingest

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        form = FormData()
        for k, v in {"csrf": csrf, "pallet_id": str(pallet_id), "quantity": "2", "note": "drill, untested"}.items():
            form.add_field(k, v)
        form.add_field("photos", _jpeg(), filename="a.jpg", content_type="image/jpeg")
        r = await c.post("/intake", data=form, allow_redirects=False)
        assert r.status == 303
        assert "Items #1, #2 logged" in r.headers["Location"].replace("%23", "#").replace("+", " ").replace("%2C", ",")
    run(env, scenario)
    assert calls["pallet"] == pallet_id and calls["quantity"] == 2 and calls["actor"] == 42
    assert calls["note"] == "drill, untested" and calls["photos"][0][0] == ".jpg"


def test_intake_refuses_non_images_and_bad_csrf(env):
    pallet_id = env.db.create_pallet("P", category_id=5, created_by=1)
    env.flow.ingest_data_entry = AsyncMock()

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        form = FormData()
        form.add_field("csrf", csrf)
        form.add_field("pallet_id", str(pallet_id))
        form.add_field("photos", b"#!/bin/sh\necho hi", filename="x.jpg", content_type="image/jpeg")
        r = await c.post("/intake", data=form, allow_redirects=False)
        assert r.status == 303 and "isn" in r.headers["Location"]
        form = FormData()
        form.add_field("csrf", "wrong")
        form.add_field("photos", _jpeg(), filename="a.jpg", content_type="image/jpeg")
        assert (await c.post("/intake", data=form)).status == 403
    run(env, scenario)
    env.flow.ingest_data_entry.assert_not_awaited()


# --------------------------------------------------------------- pipeline

def test_approve_validates_like_discord_then_calls_do_approve(env):
    item = _item(env.db, env.db.STATUS_QUEUE_REVIEW)
    env.flow.do_approve = AsyncMock(return_value={"message": "Approved. Moved to <#5> for listing.", "channel_id": 5})

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_QUEUE_REVIEW)
        page = await (await c.get(f"/items/{item['id']}")).text()
        assert 'action="/items/%d/approve"' % item["id"] in page
        bad = {"csrf": csrf, "condition_id": "1500", "category_id": "123", "title": "Drill", "price": "x",
               "weight": "2", "dims": "1 x 2"}
        r = await c.post(f"/items/{item['id']}/approve", data=bad, allow_redirects=False)
        assert "Not+approved" in r.headers["Location"] or "Not%20approved" in r.headers["Location"]
        good = {**bad, "price": "19.99", "dims": "12 x 8 x 4", "specifics": "Brand: DeWalt\nnot a pair"}
        r = await c.post(f"/items/{item['id']}/approve", data=good, allow_redirects=False)
        assert r.status == 303
    run(env, scenario)
    args, kwargs = env.flow.do_approve.await_args
    assert args == (item["id"], 42, "1500", "123", "Drill", 19.99, {"Brand": "DeWalt"})
    assert kwargs["length_in"] == 12.0 and kwargs["listing_format"] == "FixedPrice"


def test_mark_sold_other_platform_and_wrong_role(env):
    item = _item(env.db, env.db.STATUS_LISTED)
    env.flow.do_mark_sold = AsyncMock(return_value=7)

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_QUEUE_REVIEW)
        assert (await c.post(f"/items/{item['id']}/mark-sold", data={"csrf": csrf, "platform": "eBay"})).status == 403
        env.guild.add(42, config.ROLE_LISTING_MGMT)
        r = await c.post(f"/items/{item['id']}/mark-sold", data={"csrf": csrf, "platform": "Other", "other": "Facebook"},
                         allow_redirects=False)
        assert r.status == 303
    run(env, scenario)
    env.flow.do_mark_sold.assert_awaited_once_with(item["id"], "Other: Facebook", 42)


def test_action_error_is_shown_not_crashed(env):
    from cogs.item_flow import ActionError
    item = _item(env.db, env.db.STATUS_AWAITING_LISTING)
    env.flow.do_mark_listed = AsyncMock(side_effect=ActionError("This item was already moved on"))

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_LISTING_MGMT)
        r = await c.post(f"/items/{item['id']}/mark-listed", data={"csrf": csrf}, allow_redirects=False)
        assert r.status == 303 and "already+moved+on" in r.headers["Location"]
    run(env, scenario)


# ----------------------------------------------------------- photos/inventory

def test_photos_only_from_the_items_own_files_inside_photo_dir(env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PHOTO_DIR", str(tmp_path / "photos"))
    (tmp_path / "photos" / "1").mkdir(parents=True)
    good = tmp_path / "photos" / "1" / "photo_0.jpg"
    good.write_bytes(_jpeg())
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    item = _item(env.db)
    with env.db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls = ? WHERE id = ?",
                     (f'["{good.as_posix()}", "{outside.as_posix()}"]', item["id"]))

    async def scenario(c):
        sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        assert (await c.get(f"/photo/{item['id']}/0")).status == 200
        assert (await c.get(f"/photo/{item['id']}/1")).status == 404  # outside PHOTO_DIR
        assert (await c.get(f"/photo/{item['id']}/9")).status == 404
    run(env, scenario)


def test_inventory_search_and_item_page(env):
    item = _item(env.db, env.db.STATUS_LISTED)

    async def scenario(c):
        sign_in(env, c, 42, config.ROLE_DATA_ENTRY)
        text = await (await c.get("/inventory?q=drill")).text()
        assert f"/items/{item['id']}" in text
        text = await (await c.get("/inventory?q=<script>")).text()
        assert "<script>" not in text.split("<main", 1)[1].split("</main>")[0].replace('<script src="/static/', "")
        assert (await c.get(f"/items/{item['id']}")).status == 200
    run(env, scenario)


# ----------------------------------------------------------------- finance

def test_finance_confirm_calls_the_shared_function(env, monkeypatch):
    import ebay_books
    monkeypatch.setattr(ebay_books, "confirm_account_charges", AsyncMock(return_value={
        "ok": True, "breakdown": {"totals": {"total": 21.95}}, "je_doc": "JE-9", "pallet_ids": []}))

    async def scenario(c):
        csrf = sign_in(env, c, 42, config.ROLE_FINANCE_MGMT)
        assert (await c.get("/finance")).status == 200
        assert (await c.get("/finance/pallets")).status == 200
        assert (await c.get("/finance/sales")).status == 200
        assert (await c.get("/finance/payouts")).status == 200
        r = await c.post("/finance/account/2026-09", data={"csrf": csrf}, allow_redirects=False)
        assert r.status == 303 and "21.95" in r.headers["Location"]
    run(env, scenario)
    ebay_books.confirm_account_charges.assert_awaited_once_with("2026-09", actor_id=42)
