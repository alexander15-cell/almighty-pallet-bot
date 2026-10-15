"""The home page: where everything stands, and shortcuts by role."""
from aiohttp import web

import config
import database as db
from webapp.layout import STATUS_LABELS, esc, page
from webapp.server import html_response, user_of

PIPELINE = [
    (db.STATUS_DATA_ENTRY, None), (db.STATUS_AUTOMATED_REVIEW, None),
    (db.STATUS_QUEUE_REVIEW, "/review"), (db.STATUS_AWAITING_LISTING, "/listing"),
    (db.STATUS_LISTED, "/listing?tab=listed"), (db.STATUS_SOLD, "/listing?tab=sold"),
    (db.STATUS_SHIPPED, None), (db.STATUS_ON_HOLD, None), (db.STATUS_REJECTED, None),
]


async def home(request):
    user = user_of(request)
    counts = db.web_status_counts()
    tiles = "".join(
        f'<a class="stat" href="{href or "/inventory?status=" + status}"><b>{counts.get(status, 0)}</b>'
        f'<span>{esc(STATUS_LABELS[status])}</span></a>'
        for status, href in PIPELINE
    )
    shortcuts = []
    if user.has(config.ROLE_DATA_ENTRY):
        shortcuts.append('<a class="btn primary big" href="/intake">📷 Add items</a>')
    if user.has(config.ROLE_QUEUE_REVIEW):
        shortcuts.append(f'<a class="btn big" href="/review">Review queue ({counts.get(db.STATUS_QUEUE_REVIEW, 0)})</a>')
    if user.has(config.ROLE_LISTING_MGMT):
        shortcuts.append(f'<a class="btn big" href="/listing">Ready to list ({counts.get(db.STATUS_AWAITING_LISTING, 0)})</a>')
    if user.has(config.ROLE_FINANCE_MGMT):
        shortcuts.append('<a class="btn big" href="/finance">Finance</a>')
    body = (f"<h1>Hi {esc(user.name)}</h1>"
            f'<div class="shortcuts">{"".join(shortcuts)}</div>'
            f"<h2>Where everything is</h2><div class=\"stats\">{tiles}</div>"
            "<p class='muted'>Everything here is the same data as Discord - do each step in whichever one you "
            "prefer, and the other updates too.</p>")
    return html_response(page(user, "Home", body, active="home"))


def add_routes(app: web.Application):
    app.router.add_get("/", home)
