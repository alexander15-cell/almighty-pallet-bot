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


# Small line icons for the nav (inline SVG - no script, so the content
# policy is fine with them).
_ICONS = {
    "home": '<path d="M3 11.5 12 4l9 7.5"/><path d="M5 10v10h14V10"/>',
    "intake": '<rect x="3" y="7" width="18" height="13" rx="2"/><circle cx="12" cy="13.5" r="3.5"/><path d="M8 7l1.5-3h5L16 7"/>',
    "review": '<path d="M9 11l2 2 4-4"/><rect x="4" y="3" width="16" height="18" rx="2"/>',
    "listing": '<path d="M20 12 12 20 4 12V4h8z"/><circle cx="8.5" cy="8.5" r="1.5"/>',
    "inventory": '<path d="M3 8l9-5 9 5-9 5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    "finance": '<path d="M12 3v18"/><path d="M16.5 7.5c0-1.7-2-3-4.5-3s-4.5 1.3-4.5 3S9 10.5 12 11s4.5 1.3 4.5 3.5-2 3-4.5 3-4.5-1.3-4.5-3"/>',
}


def _icon(key: str) -> str:
    return (f'<svg class="ic" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{_ICONS[key]}</svg>')


def _nav_links(user) -> list:
    links = [("home", "/", "Home", True),
             ("intake", "/intake", "Intake", user.has(config.ROLE_DATA_ENTRY)),
             ("review", "/review", "Review", user.has(config.ROLE_QUEUE_REVIEW)),
             ("listing", "/listing", "Listing", user.has(config.ROLE_LISTING_MGMT)),
             ("inventory", "/inventory", "Inventory", True),
             ("finance", "/finance", "Finance", user.has(config.ROLE_FINANCE_MGMT))]
    return [(key, href, label) for key, href, label, allowed in links if allowed]


def _nav(user, active: str) -> str:
    links = _nav_links(user)
    top = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>' for key, href, label in links)
    initial = esc((user.name or "?").strip()[:1].upper())
    return (f'<header class="topnav"><a class="brand" href="/" aria-label="Dashboard home">'
            f'<img src="/static/logo.svg" alt="4 Guys 1 Pallet Overstock" class="logo">'
            f'<span class="brand-tag">Dashboard</span></a>'
            f'<nav class="links" aria-label="Main">{top}</nav>'
            f'<div class="who"><span class="avatar" aria-hidden="true">{initial}</span>'
            f'<span class="name">{esc(user.name)}</span>'
            f'<form method="post" action="/logout" class="inline">{csrf_input(user)}'
            f'<button class="link">Sign out</button></form></div></header>')


def _tabbar(user, active: str) -> str:
    """The phone tab bar along the bottom (shown on narrow screens only)."""
    items = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{_icon(key)}<span>{label}</span></a>'
                    for key, href, label in _nav_links(user))
    return f'<nav class="tabbar" aria-label="Main">{items}</nav>'


_HEAD = ('<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
         '<meta name="theme-color" content="#315e35">'
         '<link rel="icon" href="/static/mark.svg" type="image/svg+xml">'
         '<link rel="apple-touch-icon" href="/static/mark.svg">'
         '<link rel="stylesheet" href="/static/app.css">')


def page(user, title: str, body: str, active: str = "", phone: bool = False, flash: str = None,
         scripts: tuple = ()) -> str:
    nav = _nav(user, active) if user else ""
    # Phone-first pages (intake) have their own sticky action bar, so the tab
    # bar stays out of their way.
    tabbar = _tabbar(user, active) if user and not phone else ""
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    script_tags = "".join(f'<script src="/static/{esc(s)}"></script>' for s in scripts)
    csrf_meta = f'<meta name="csrf" content="{esc(user.csrf)}">' if user else ""
    return f"""<!doctype html>
<html lang="en"><head>
{_HEAD}{csrf_meta}
<title>{esc(title)} · 4 Guys 1 Pallet</title>
</head><body class="{'has-tabbar' if tabbar else ''}">
{nav}
<main class="{'phone' if phone else 'wrap'}">{flash_html}{body}</main>
{tabbar}
<div id="toast" class="toast" hidden></div>
<script src="/static/common.js"></script>{script_tags}
</body></html>"""


def bare_page(title: str, body: str) -> str:
    """Sign-in / error pages - no nav, no session."""
    return f"""<!doctype html>
<html lang="en"><head>{_HEAD}
<title>{esc(title)} · 4 Guys 1 Pallet</title></head>
<body class="bare"><main class="wrap"><div class="card narrow center">
<img src="/static/logo.svg" alt="4 Guys 1 Pallet Overstock" class="bare-logo">{body}</div></main></body></html>"""
