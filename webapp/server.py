"""
The web dashboard's aiohttp app: routes from each page module, plus the
three things every request goes through -

  1. security headers (no framing, strict content policy, no sniffing),
  2. sign-in: anything but /login, /oauth/callback, /static and /health
     needs a valid session whose Discord user still holds a bot role,
  3. CSRF: every POST must carry the session's token (form field "csrf" or
     X-CSRF-Token header) and, if the browser sends an Origin, it must be
     this site's own.
"""
import logging
import secrets
from pathlib import Path

from aiohttp import web

import config
from webapp import auth
from webapp.layout import bare_page, esc

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_PREFIXES = ("/login", "/oauth/callback", "/static/", "/health")
MULTIPART_PATHS = {"/intake"}  # POSTs that check their own CSRF token (see require_sign_in)
BOT_KEY = web.AppKey("bot", object)


def bot_of(request):
    return request.app[BOT_KEY]


def user_of(request) -> auth.WebUser:
    return request["user"]


def require(request, *role_names):
    """Raises 403 unless the signed-in user holds one of role_names (or is
    a Pallet Admin) - the same rule as the Discord buttons."""
    if not user_of(request).has(*role_names):
        names = " or ".join(role_names)
        raise web.HTTPForbidden(text=bare_page("No access", f"<h1>No access</h1><p>You need the <b>{esc(names)}</b> "
                                               "role in Discord for this page.</p><p><a href='/'>Home</a></p>"),
                                content_type="text/html")


def html_response(text: str, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/html")


@web.middleware
async def security_headers(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; frame-ancestors 'none'; form-action 'self' https://discord.com; base-uri 'self'",
    )
    if config.WEB_BASE_URL.startswith("https://"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


@web.middleware
async def require_sign_in(request, handler):
    if request.path.startswith(PUBLIC_PREFIXES):
        return await handler(request)
    session = auth.load_session(request.cookies.get(auth.SESSION_COOKIE))
    user = await auth.user_for_session(bot_of(request), session) if session else None
    if user is None:
        if request.method == "GET" and not request.path.startswith("/api/"):
            raise web.HTTPFound("/login")
        raise web.HTTPUnauthorized(text="Signed out - reload the page and sign in again.")
    request["user"] = user
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != config.WEB_BASE_URL.rstrip("/"):
            raise web.HTTPForbidden(text="Cross-site request refused.")
        supplied = request.headers.get("X-CSRF-Token")
        if not supplied and request.content_type == "application/x-www-form-urlencoded":
            supplied = (await request.post()).get("csrf")
        elif not supplied and request.content_type == "multipart/form-data" and request.path in MULTIPART_PATHS:
            # Photo uploads: the handler streams the body and checks the
            # token itself (pipeline.intake_submit), so big uploads aren't
            # read twice. Only these exact paths may do that.
            return await handler(request)
        if not auth.check_csrf(user, supplied):
            raise web.HTTPForbidden(text="This form expired - go back, reload the page and try again.")
    return await handler(request)


# ------------------------------------------------------------------ sign-in

async def login(request):
    if not (config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET):
        return html_response(bare_page("Not set up", "<h1>Web sign-in isn't set up</h1><p>Add "
                                       "<code>DISCORD_CLIENT_ID</code> and <code>DISCORD_CLIENT_SECRET</code> to "
                                       "<code>.env</code> and restart the bot.</p>"), status=503)
    if request.query.get("go") != "1":
        return html_response(bare_page("Sign in", "<h1>4 Guys 1 Pallet</h1><p class='muted'>Sign in with the "
                                       "Discord account you use in the pallet server.</p>"
                                       "<p><a class='btn primary big' href='/login?go=1'>Sign in with Discord</a></p>"))
    state = auth.new_state()
    resp = web.HTTPFound(auth.authorize_url(state))
    resp.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True, samesite="Lax",
                    secure=config.WEB_BASE_URL.startswith("https://"))
    raise resp


async def oauth_callback(request):
    state = request.cookies.get(auth.STATE_COOKIE)
    if not state or not secrets.compare_digest(state, request.query.get("state", "")):
        return html_response(bare_page("Sign-in failed", "<h1>Sign-in expired</h1><p><a href='/login'>Try again</a></p>"),
                             status=400)
    if request.query.get("error") or not request.query.get("code"):
        return html_response(bare_page("Sign-in cancelled", "<h1>Sign-in cancelled</h1><p><a href='/login'>Try again</a></p>"),
                             status=400)
    try:
        discord_user = await auth.exchange_code(request.query["code"])
    except PermissionError as e:
        return html_response(bare_page("Sign-in failed", f"<h1>Sign-in failed</h1><p>{esc(e)}</p>"
                                       "<p><a href='/login'>Try again</a></p>"), status=400)
    member = await auth.member_for(bot_of(request), int(discord_user["id"]))
    if not auth.role_names_for(member):
        return html_response(bare_page("No access", "<h1>No access</h1><p>That Discord account isn't in the pallet "
                                       "server, or doesn't have any of the bot's roles yet. Ask a Pallet Admin."
                                       "</p>"), status=403)
    token = auth.create_session(discord_user)
    resp = web.HTTPFound("/")
    resp.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                    secure=config.WEB_BASE_URL.startswith("https://"))
    resp.del_cookie(auth.STATE_COOKIE)
    log.info("Web dashboard sign-in: Discord user %s", discord_user["id"])
    raise resp


async def logout(request):
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = web.HTTPFound("/login")
    resp.del_cookie(auth.SESSION_COOKIE)
    raise resp


async def health(request):
    return web.json_response({"ok": True})


def create_app(bot) -> web.Application:
    from webapp import finance, home, inventory, pipeline  # page modules register their routes

    app = web.Application(middlewares=[security_headers, require_sign_in],
                          client_max_size=config.WEB_MAX_UPLOAD_MB * 1024 * 1024 * 10)
    app[BOT_KEY] = bot
    app.router.add_get("/login", login)
    app.router.add_get("/oauth/callback", oauth_callback)
    app.router.add_post("/logout", logout)
    app.router.add_get("/health", health)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    for module in (home, inventory, finance, pipeline):
        module.add_routes(app)
    return app
