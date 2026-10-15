"""HTML page shell and small formatting helpers. Everything user-supplied
goes through esc() - there's no template engine doing it automatically."""
import html
import urllib.parse

import config
import database as db

STATUS_LABELS = {
    db.STATUS_DATA_ENTRY: "Data entry", db.STATUS_AUTOMATED_REVIEW: "AI review", db.STATUS_QUEUE_REVIEW: "Queue review",
    db.STATUS_AWAITING_LISTING: "Awaiting listing", db.STATUS_LISTED: "Listed", db.STATUS_SOLD: "Sold",
    db.STATUS_SHIPPED: "Shipped", db.STATUS_ON_HOLD: "On hold", db.STATUS_REJECTED: "Rejected",
    db.STATUS_DELETED: "Deleted",
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(value) -> str:
    if value is None:
        return "-"
    value = round(value, 2) + 0.0  # no "-$0.00"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def badge(status: str) -> str:
    return f'<span class="badge s-{esc(status)}">{esc(STATUS_LABELS.get(status, status))}</span>'


def qs(**params) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})


def csrf_input(user) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(user.csrf)}">'


def post_button(user, action: str, label: str, cls: str = "btn", confirm: str = None, fields: dict = None) -> str:
    """A one-button form - every state-changing action on this site is a
    POST carrying the session's CSRF token."""
    extra = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in (fields or {}).items())
    # No inline JS (the content policy forbids it) - common.js asks for
    # confirmation on any form with data-confirm.
    confirm_attr = f' data-confirm="{esc(confirm)}"' if confirm else ""
    return (f'<form method="post" action="{esc(action)}" class="inline"{confirm_attr}>{csrf_input(user)}{extra}'
            f'<button class="{esc(cls)}">{esc(label)}</button></form>')


def _nav(user, active: str) -> str:
    links = [("home", "/", "Home", True)]
    links.append(("intake", "/intake", "📷 Intake", user.has(config.ROLE_DATA_ENTRY)))
    links.append(("review", "/review", "Queue review", user.has(config.ROLE_QUEUE_REVIEW)))
    links.append(("listing", "/listing", "Listing", user.has(config.ROLE_LISTING_MGMT)))
    links.append(("inventory", "/inventory", "Inventory", True))
    links.append(("finance", "/finance", "Finance", user.has(config.ROLE_FINANCE_MGMT)))
    items = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
                    for key, href, label, allowed in links if allowed)
    return (f'<nav class="topnav"><a class="brand" href="/">4G1P</a><div class="links">{items}</div>'
            f'<div class="who">{esc(user.name)} '
            f'<form method="post" action="/logout" class="inline">{csrf_input(user)}'
            f'<button class="link">sign out</button></form></div></nav>')


def page(user, title: str, body: str, active: str = "", phone: bool = False, flash: str = None,
         scripts: tuple = ()) -> str:
    nav = _nav(user, active) if user else ""
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    script_tags = "".join(f'<script src="/static/{esc(s)}"></script>' for s in scripts)
    csrf_meta = f'<meta name="csrf" content="{esc(user.csrf)}">' if user else ""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#1f2937">{csrf_meta}
<title>{esc(title)} · 4G1P</title>
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/app.css">
</head><body>
{nav}
<main class="{'phone' if phone else 'wrap'}">{flash_html}{body}</main>
<div id="toast" class="toast" hidden></div>
<script src="/static/common.js"></script>{script_tags}
</body></html>"""


def bare_page(title: str, body: str) -> str:
    """Sign-in / error pages - no nav, no session."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 4G1P</title><link rel="stylesheet" href="/static/app.css"></head>
<body><main class="wrap"><div class="card narrow center">{body}</div></main></body></html>"""
