"""
Inventory browser (everyone signed in), item pages, and item photos.

Photos are served from the item's own stored paths only (never a path from
the URL), preferring the durable R2 copy when there is one.
"""
import json
from pathlib import Path

from aiohttp import web

import config
import database as db
from webapp.layout import STATUS_LABELS, badge, esc, money, page, qs
from webapp.server import html_response, user_of

PAGE_SIZE = 60


def photo_list(item: dict) -> list:
    try:
        local = json.loads(item.get("photo_urls") or "[]")
    except (TypeError, ValueError):
        local = []
    try:
        public = json.loads(item.get("photo_public_urls") or "[]")
    except (TypeError, ValueError):
        public = []
    return [{"index": i, "public": public[i] if i < len(public) else None, "local": path}
            for i, path in enumerate(local)]


def photo_url(item: dict, index: int = 0):
    photos = photo_list(item)
    if index >= len(photos):
        return None
    return photos[index]["public"] or f"/photo/{item['id']}/{index}"


def item_title(item: dict) -> str:
    return (item.get("ebay_title") or item.get("ai_title") or item.get("raw_description") or "(no description)")[:120]


def item_tile(item: dict, href: str = None) -> str:
    url = photo_url(item)
    img = f'<img src="{esc(url)}" alt="" loading="lazy">' if url else "no photo"
    price = item.get("sale_price") if item.get("sale_price") is not None else item.get("list_price")
    return (f'<a class="tile" href="{esc(href or "/items/" + str(item["id"]))}"><div class="img">{img}</div>'
            f'<div class="body"><div class="t">{esc(item_title(item))}</div>'
            f'<div class="meta"><span>{esc(item.get("pallet_name", ""))} #{item["item_number"]}</span>'
            f'<span>{money(price) if price is not None else ""}</span></div>'
            f'<div class="meta">{badge(item["status"])}</div></div></a>')


async def inventory(request):
    user = user_of(request)
    status = request.query.get("status") or None
    pallet = request.query.get("pallet") or None
    q = (request.query.get("q") or "").strip() or None
    try:
        page_no = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page_no = 1
    statuses = [status] if status in STATUS_LABELS else None
    pallet_id = int(pallet) if pallet and pallet.isdigit() else None
    items = db.web_search_items(statuses, pallet_id, q, limit=PAGE_SIZE + 1, offset=(page_no - 1) * PAGE_SIZE)
    more = len(items) > PAGE_SIZE
    items = items[:PAGE_SIZE]
    counts = db.web_status_counts()

    tabs = [f'<a href="/inventory?{qs(pallet=pallet, q=q)}" class="{"on" if not status else ""}">All</a>']
    for key, label in STATUS_LABELS.items():
        if key == db.STATUS_DELETED or not counts.get(key):
            continue
        tabs.append(f'<a href="/inventory?{qs(status=key, pallet=pallet, q=q)}" class="{"on" if status == key else ""}">'
                    f'{esc(label)}<span class="count">{counts[key]}</span></a>')
    pallet_opts = "".join(
        f'<option value="{p["id"]}" {"selected" if pallet_id == p["id"] else ""}>{esc(p["name"])}</option>'
        for p in db.get_all_pallets(include_archived=True))
    search = (f'<form class="search3" method="get" action="/inventory">'
              f'<input type="hidden" name="status" value="{esc(status or "")}">'
              f'<input type="text" name="q" value="{esc(q or "")}" placeholder="Search title, note, or item #">'
              f'<select name="pallet"><option value="">All pallets</option>{pallet_opts}</select>'
              f'<button class="btn">Search</button></form>')
    grid = "".join(item_tile(i) for i in items) or "<p class='muted'>Nothing matches.</p>"
    nav = ""
    if page_no > 1 or more:
        prev_link = (f'<a class="btn small" href="/inventory?{qs(status=status, pallet=pallet, q=q, page=page_no - 1)}">‹ Newer</a>'
                     if page_no > 1 else "")
        next_link = (f'<a class="btn small" href="/inventory?{qs(status=status, pallet=pallet, q=q, page=page_no + 1)}">Older ›</a>'
                     if more else "")
        nav = f'<div class="actions">{prev_link}{next_link}</div>'
    body = f"<h1>Inventory</h1>{search}<div class='tabs'>{''.join(tabs)}</div><div class='grid'>{grid}</div>{nav}"
    return html_response(page(user, "Inventory", body, active="inventory"))


def item_photos_html(item: dict) -> str:
    photos = photo_list(item)
    if not photos:
        return "<p class='muted'>No photos.</p>"
    return '<div class="photos">' + "".join(
        f'<a class="photo" href="{esc(photo_url(item, p["index"]))}" target="_blank" rel="noopener">'
        f'<img src="{esc(photo_url(item, p["index"]))}" alt="" loading="lazy"><span class="n">{p["index"] + 1}</span></a>'
        for p in photos) + "</div>"


def item_facts_html(item: dict) -> str:
    listing = db.get_ebay_listing_data(item["id"]) or {}
    rows = [("Status", badge(item["status"])), ("Pallet", esc(db.get_pallet(item["pallet_id"])["name"])),
            ("Item #", str(item["item_number"]))]
    if listing:
        rows.append(("Listing title", esc(listing.get("ebay_title"))))
        rows.append(("List price", money(listing.get("price"))))
        rows.append(("Condition", esc(config.EBAY_CONDITION_LABELS.get(listing.get("condition_id"), listing.get("condition_id")))))
        if listing.get("ebay_item_id"):
            rows.append(("eBay listing", f'<a href="https://www.ebay.com/itm/{esc(listing["ebay_item_id"])}" '
                                         f'target="_blank" rel="noopener">{esc(listing["ebay_item_id"])}</a>'))
    if item.get("sale_platform"):
        rows.append(("Sold on", esc(item["sale_platform"])))
    if item.get("sale_price") is not None:
        rows.append(("Sale price", money(item["sale_price"])))
    sale = db.get_sale_for_item(item["id"])
    if sale:
        receipt = sale.get("quickbooks_sales_receipt_id")
        rows.append(("Logged sale", f"#{sale['id']} · " + (f"Sales Receipt {esc(receipt)}" if receipt else "⚠️ not in QuickBooks yet")))
    hold = db.get_open_hold(item["id"]) if item["status"] == db.STATUS_ON_HOLD else None
    if hold:
        rows.append(("On hold", esc(db.HOLD_REASON_LABELS.get(hold.get("reason"), hold.get("reason"))) +
                     (f" - {esc(hold['note'])}" if hold.get("note") else "")))
    return "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def item_history_html(item_id: int) -> str:
    events = db.get_item_events(item_id)
    if not events:
        return ""
    lis = "".join(
        f'<li><span class="when">{esc((e["timestamp"] or "")[:16].replace("T", " "))}</span> '
        f'{esc(STATUS_LABELS.get(e["to_status"], e["to_status"]))}{" - " + esc(e["note"]) if e.get("note") else ""}</li>'
        for e in events)
    return f'<h2>History</h2><ul class="list events">{lis}</ul>'


async def item_page(request):
    user = user_of(request)
    item = db.get_item(int(request.match_info["item_id"]))
    if not item or item["status"] == db.STATUS_DELETED:
        raise web.HTTPNotFound(text="No such item.")
    from webapp import pipeline  # action buttons for whatever stage it's in
    desc = item.get("ai_description") or item.get("raw_description") or ""
    body = (f'<p><a href="/inventory">‹ Inventory</a></p><h1>{esc(item_title(item))}</h1>'
            f'<div class="two"><div>{item_photos_html(item)}<h2>Description</h2>'
            f'<p class="pre">{esc(desc) or "<span class=muted>none</span>"}</p></div>'
            f'<div><div class="card">{item_facts_html(item)}</div>{pipeline.item_actions_html(user, item)}</div></div>'
            f'{item_history_html(item["id"])}')
    return html_response(page(user, f"Item #{item['item_number']}", body, active="inventory",
                              flash=request.query.get("msg")))


async def photo(request):
    """A locally stored item photo - only paths recorded on the item itself,
    and only inside PHOTO_DIR."""
    item = db.get_item(int(request.match_info["item_id"]))
    index = int(request.match_info["index"])
    photos = photo_list(item) if item else []
    if index >= len(photos):
        raise web.HTTPNotFound()
    path = Path(photos[index]["local"]).resolve()
    root = Path(config.PHOTO_DIR).resolve()
    if root not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


def add_routes(app: web.Application):
    app.router.add_get("/inventory", inventory)
    app.router.add_get(r"/items/{item_id:\d+}", item_page)
    app.router.add_get(r"/photo/{item_id:\d+}/{index:\d+}", photo)
