"""
Phone intake, Queue Review and listing on the web - every action calls the
same ItemFlow function its Discord button does (ingest_data_entry,
do_approve, do_reject, do_list_on_ebay, ...), so the Discord cards move
exactly as if someone had clicked there.
"""
import asyncio
import io
import logging

from aiohttp import web
from PIL import Image, UnidentifiedImageError

import config
import database as db
import ebay_api
import ebay_aspects
import ebay_taxonomy
from webapp import auth
from webapp.inventory import item_tile
from webapp.layout import badge, csrf_input, esc, page, post_button, qs
from webapp.server import bot_of, html_response, require, user_of

log = logging.getLogger(__name__)

MAX_PHOTOS = 10
_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "HEIF": ".heic", "MPO": ".jpg"}
_background = set()  # keeps background AI-review tasks alive until they finish


def _flow(request):
    cog = bot_of(request).get_cog("ItemFlow")
    if cog is None:
        raise web.HTTPServiceUnavailable(text="The bot is still starting up - try again in a moment.")
    return cog


def _back(item_id: int, msg: str):
    raise web.HTTPSeeOther(f"/items/{item_id}?{qs(msg=msg)}")


def _strip_md(text: str) -> str:
    """Discord-formatted replies (**bold**, <#channel>) read fine as plain text."""
    import re
    return re.sub(r"<#\d+>", "its Discord channel", (text or "").replace("**", ""))


def _run_in_background(coro, what: str):
    task = asyncio.create_task(coro)
    _background.add(task)

    def done(t):
        _background.discard(t)
        if not t.cancelled() and t.exception():
            log.error("Web dashboard background job failed (%s)", what, exc_info=t.exception())
    task.add_done_callback(done)
    return task


# ================================================================== intake

def _intake_form(user, pallets, redo_item=None) -> str:
    if redo_item:
        pallet_field = (f'<input type="hidden" name="redo_item" value="{redo_item["id"]}">'
                        f'<p>Resubmitting <b>{esc(db.get_pallet(redo_item["pallet_id"])["name"])} '
                        f'#{redo_item["item_number"]}</b> - new photos replace the old ones.</p>')
        qty_field = ""
    else:
        opts = "".join(f'<option value="{p["id"]}">{esc(p["name"])}</option>' for p in pallets)
        pallet_field = f'<label class="req">Pallet</label><select name="pallet_id" required>{opts}</select>'
        qty_field = ('<label>How many identical items?</label>'
                     '<input type="number" name="quantity" min="1" max="25" value="1" inputmode="numeric">')
    return f"""<form id="intake-form" method="post" action="/intake" enctype="multipart/form-data" class="card">
{csrf_input(user)}{pallet_field}
<label class="req">Photos</label>
<div class="camera-row">
  <label class="btn primary"><input type="file" name="photos" accept="image/*" capture="environment" multiple data-photo-input>📷 Take photo</label>
  <label class="btn"><input type="file" name="photos" accept="image/*" multiple data-photo-input>Choose photos</label>
</div>
<div class="photos" id="photo-preview"></div>
<p class="muted small">Up to {MAX_PHOTOS} photos. First one is the cover. Photos are shrunk on your phone before upload.</p>
{qty_field}
<label>Note</label>
<textarea name="note" placeholder="e.g. cordless drill, has case, untested"></textarea>
<button class="btn ok big" id="intake-submit">Log item ➜</button>
</form>"""


async def intake_page(request):
    user = user_of(request)
    require(request, config.ROLE_DATA_ENTRY)
    pallets = [p for p in db.get_all_pallets(include_archived=False)]
    if not pallets:
        body = "<h1>Add items</h1><p>No active pallets - start one in Discord first.</p>"
        return html_response(page(user, "Intake", body, active="intake", phone=True))
    rejected = db.web_search_items([db.STATUS_REJECTED], limit=20)
    redo = ""
    if rejected:
        redo = ("<h2>Sent back for redo</h2><div class='grid'>" +
                "".join(item_tile(i, href=f"/intake/redo/{i['id']}") for i in rejected) + "</div>")
    body = (f"<h1>Add items</h1>{_intake_form(user, pallets)}{redo}"
            "<p class='muted small'>Same as posting in a pallet's #data-entry channel: the item gets its "
            "number right away, then goes through AI review into Queue Review.</p>")
    return html_response(page(user, "Intake", body, active="intake", phone=True, flash=request.query.get("msg"),
                              scripts=("intake.js",)))


async def intake_redo_page(request):
    user = user_of(request)
    require(request, config.ROLE_DATA_ENTRY)
    item = db.get_item(int(request.match_info["item_id"]))
    if not item or item["status"] != db.STATUS_REJECTED:
        raise web.HTTPSeeOther("/intake?" + qs(msg="That item isn't waiting for a redo anymore."))
    body = f"<h1>Redo item</h1>{_intake_form(user, [], redo_item=item)}"
    return html_response(page(user, "Redo", body, active="intake", phone=True, scripts=("intake.js",)))


def _normalize_photo(data: bytes):
    """(extension, bytes) for a real image, or None. Re-checks the file is an
    image Pillow can open - never trusts the browser's name or type."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
            fmt = img.format
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None
    return (_EXT.get(fmt, ".jpg"), data)


async def intake_submit(request):
    user = user_of(request)
    require(request, config.ROLE_DATA_ENTRY)
    fields, photos = {}, []
    limit = config.WEB_MAX_UPLOAD_MB * 1024 * 1024
    reader = await request.multipart()
    async for part in reader:
        if part.name == "photos" and part.filename:
            data = await part.read(decode=False)
            if not data:
                continue
            if len(data) > limit:
                raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(data))
            if len(photos) >= MAX_PHOTOS:
                continue
            normalized = _normalize_photo(bytes(data))
            if normalized is None:
                raise web.HTTPSeeOther("/intake?" + qs(msg="One of those files isn't a photo - nothing was logged."))
            photos.append(normalized)
        elif part.name in ("csrf", "pallet_id", "quantity", "note", "redo_item"):
            fields[part.name] = (await part.text())[:2000]
    if not auth.check_csrf(user, fields.get("csrf")):
        raise web.HTTPForbidden(text="This form expired - go back, reload the page and try again.")
    if not photos:
        raise web.HTTPSeeOther("/intake?" + qs(msg="Add at least one photo."))

    note = (fields.get("note") or "").strip()
    resubmit_item = None
    if fields.get("redo_item", "").isdigit():
        resubmit_item = db.get_item(int(fields["redo_item"]))
        if not resubmit_item or resubmit_item["status"] != db.STATUS_REJECTED:
            raise web.HTTPSeeOther("/intake?" + qs(msg="That item isn't waiting for a redo anymore."))
        pallet_id, quantity = resubmit_item["pallet_id"], 1
    else:
        pallet = db.get_pallet(int(fields["pallet_id"])) if fields.get("pallet_id", "").isdigit() else None
        if not pallet or pallet.get("archived"):
            raise web.HTTPSeeOther("/intake?" + qs(msg="Pick an active pallet."))
        pallet_id = pallet["id"]
        try:
            quantity = max(1, min(int(fields.get("quantity") or 1), 25))
        except ValueError:
            quantity = 1

    flow = _flow(request)
    numbers_ready = asyncio.get_running_loop().create_future()

    async def got_numbers(item_ids):
        if not numbers_ready.done():
            numbers_ready.set_result(item_ids)

    task = _run_in_background(
        flow.ingest_data_entry(pallet_id, note, photos, user.discord_id, quantity=quantity,
                               resubmit_item=resubmit_item, before_announce=got_numbers),
        "phone intake")
    done, _ = await asyncio.wait({numbers_ready, task}, timeout=180, return_when=asyncio.FIRST_COMPLETED)
    if numbers_ready in done:
        numbers = [db.get_item(i)["item_number"] for i in numbers_ready.result()]
        text = ", ".join(f"#{n}" for n in numbers)
        msg = (f"✅ Item {text} logged - write that number on the box. AI review is running; it'll show up in "
               "Queue Review shortly." if len(numbers) == 1 else
               f"✅ Items {text} logged - write the matching number on each box.")
    elif task in done and task.exception():
        msg = f"Something went wrong logging that item: {task.exception()}"
    else:
        msg = "Still saving - check the pallet's #data-entry channel for the item number."
    raise web.HTTPSeeOther("/intake?" + qs(msg=msg))


# ============================================================ lists

def _list_page(user, title, active, items, empty, tabs="", flash=None):
    grid = "".join(item_tile(i) for i in items) or f"<p class='muted'>{esc(empty)}</p>"
    return page(user, title, f"<h1>{esc(title)}</h1>{tabs}<div class='grid'>{grid}</div>", active=active, flash=flash)


async def review_list(request):
    user = user_of(request)
    require(request, config.ROLE_QUEUE_REVIEW)
    items = db.web_search_items([db.STATUS_QUEUE_REVIEW], limit=200)
    return html_response(_list_page(user, "Queue review", "review", list(reversed(items)),
                                    "Nothing waiting for review.", flash=request.query.get("msg")))


LISTING_TABS = [("awaiting", "Ready to list", db.STATUS_AWAITING_LISTING), ("listed", "Listed", db.STATUS_LISTED),
                ("sold", "Sold - to ship", db.STATUS_SOLD)]


async def listing_list(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    tab = request.query.get("tab", "awaiting")
    status = dict((k, s) for k, _, s in LISTING_TABS).get(tab, db.STATUS_AWAITING_LISTING)
    counts = db.web_status_counts()
    tabs = "<div class='tabs'>" + "".join(
        f'<a href="/listing?tab={k}" class="{"on" if s == status else ""}">{esc(label)}'
        f'<span class="count">{counts.get(s, 0)}</span></a>' for k, label, s in LISTING_TABS) + "</div>"
    items = db.web_search_items([status], limit=200)
    return html_response(_list_page(user, "Listing", "listing", items, "Nothing here right now.", tabs=tabs,
                                    flash=request.query.get("msg")))


# ======================================================= item page actions

def _listing_defaults(item: dict, category_id: str) -> dict:
    """The same pre-fill order as Discord's approve form (EbayListingModal):
    saved listing data, else Automated Review's estimates, else blank."""
    existing = db.get_ebay_listing_data(item["id"]) or {}
    specifics = ""
    if existing.get("item_specifics"):
        specifics = "\n".join(f"{k}: {v}" for k, v in existing["item_specifics"].items())
    else:
        suggestion = db.get_item_aspect_suggestion(item["id"])
        if suggestion and str(suggestion["category_id"]) == str(category_id) and suggestion["specifics"]:
            specifics = ebay_aspects.format_specifics(suggestion["specifics"])
    price = existing.get("price", item.get("ai_suggested_price"))
    weight = existing.get("weight_lb") if existing.get("weight_lb") is not None else item.get("ai_suggested_weight_lb")
    dims_keys = ("length_in", "width_in", "height_in")
    if all(existing.get(k) is not None for k in dims_keys):
        dims = " x ".join(f"{existing[k]:g}" for k in dims_keys)
    elif all(item.get(f"ai_suggested_{k}") is not None for k in dims_keys):
        dims = " x ".join(f"{item[f'ai_suggested_{k}']:g}" for k in dims_keys)
    else:
        dims = ""
    return {
        "title": existing.get("ebay_title") or (item.get("ai_title") or "")[:80],
        "price": f"{price:.2f}" if price is not None else "",
        "weight": f"{weight:g}" if weight is not None else "",
        "dims": dims, "specifics": specifics,
        "ai_estimates": not existing,
    }


def _listing_fields_html(d: dict) -> str:
    hint = " <span class='muted'>(AI estimate - check it)</span>" if d["ai_estimates"] else ""
    return f"""
<label class="req">eBay title (max 80)</label><input type="text" name="title" maxlength="80" value="{esc(d['title'])}" required>
<div class="row3">
  <div><label class="req">Price{hint}</label><div class="money"><input type="number" name="price" step="0.01" min="0" inputmode="decimal" value="{esc(d['price'])}" required></div></div>
  <div><label class="req">Weight (lb)</label><input type="text" name="weight" inputmode="decimal" value="{esc(d['weight'])}" required></div>
  <div><label class="req">Size L x W x H (in)</label><input type="text" name="dims" placeholder="12 x 8 x 4" value="{esc(d['dims'])}" required></div>
</div>
<label>Item specifics (one per line, Key: Value)</label><textarea name="specifics">{esc(d['specifics'])}</textarea>"""


def _approve_form(user, item: dict) -> str:
    suggested = item.get("ai_suggested_category") or ""
    conds = "".join(f'<option value="{esc(cid)}" {"selected" if cid == config.EBAY_DEFAULT_CONDITION_ID else ""}>'
                    f'{esc(label)}</option>' for cid, label in config.EBAY_CONDITIONS)
    durations = "".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in config.EBAY_AUCTION_DURATIONS)
    recent = sorted(db.get_ebay_category_counts().items(), key=lambda kv: -kv[1])[:8]
    recent_opts = "".join(f'<option value="{esc(cid)}">{esc(ebay_taxonomy.get_path(cid) or cid)}</option>'
                          for cid, _ in recent)
    return f"""<form method="post" action="/items/{item['id']}/approve" class="card" id="approve-form">{csrf_input(user)}
<h2 style="margin-top:0">Approve</h2>
<label class="req">Condition</label><select name="condition_id">{conds}</select>
<label class="req">eBay category</label>
<div class="with-btn"><input type="text" id="cat-search" placeholder="Search eBay categories" value="{esc(suggested)}">
<button type="button" class="btn" id="cat-search-btn">Search</button></div>
<select name="category_id" id="cat-select" required data-suggest="{esc(suggested)}">
  <option value="">- search above, or pick a recent one -</option>{recent_opts}</select>
<div class="row"><div><label>Format</label><select name="listing_format"><option value="FixedPrice">Fixed price</option><option value="Auction">Auction</option></select></div>
<div><label>Auction length</label><select name="auction_duration">{durations}</select></div></div>
{_listing_fields_html(_listing_defaults(item, ""))}
<button class="btn ok big">Approve ➜ Ready to list</button></form>"""


def item_actions_html(user, item: dict) -> str:
    """The action panel on an item's page, for its current stage and the
    viewer's roles - the web version of that stage's Discord buttons."""
    status, item_id, parts = item["status"], item["id"], []
    if status == db.STATUS_QUEUE_REVIEW and user.has(config.ROLE_QUEUE_REVIEW):
        desc = item.get("ai_description") or item.get("raw_description") or ""
        if item.get("ai_flags"):
            parts.append(f'<div class="rej"><b>AI flags:</b> {esc(item["ai_flags"])}</div>')
        parts.append(_approve_form(user, item))
        parts.append(f'<form method="post" action="/items/{item_id}/description" class="card">{csrf_input(user)}'
                     f'<label style="margin-top:0">Description</label><textarea name="description" rows="6">{esc(desc)}</textarea>'
                     f'<button class="btn">Save description</button></form>')
        reject = (f'<form method="post" action="/items/{item_id}/reject" class="card" data-confirm="Send this item back to Data Entry?">'
                  f'{csrf_input(user)}<label style="margin-top:0">Reject - reason (shown to whoever redoes it)</label>'
                  f'<input type="text" name="reason" maxlength="300" placeholder="e.g. need a photo of the label">'
                  f'<div class="actions"><button class="btn bad">Reject ⬅ Data Entry</button>')
        if config.AI_ENABLED:
            reject += post_button(user, f"/items/{item_id}/rereview", "Re-review (AI)")
        parts.append(reject + "</div></form>")
    elif status == db.STATUS_AWAITING_LISTING and user.has(config.ROLE_LISTING_MGMT):
        listing = db.get_ebay_listing_data(item_id)
        buttons = []
        if config.EBAY_ENABLED and listing:
            buttons.append(post_button(user, f"/items/{item_id}/list-ebay", "List on eBay", "btn ok",
                                       confirm="Create the live eBay listing now?"))
        buttons.append(post_button(user, f"/items/{item_id}/mark-listed", "Mark listed (other)", "btn",
                                   confirm="Mark this listed somewhere other than eBay?"))
        parts.append(f'<div class="card"><div class="actions" style="margin-top:0">{"".join(buttons)}</div></div>')
        if listing:
            parts.append(f'<form method="post" action="/items/{item_id}/details" class="card">{csrf_input(user)}'
                         f'<h2 style="margin-top:0">Edit details</h2>{_listing_fields_html(_listing_defaults(item, listing["category_id"]))}'
                         f'<button class="btn">Save details</button></form>')
    elif status == db.STATUS_LISTED and user.has(config.ROLE_LISTING_MGMT):
        from webapp.shop import payment_link_form
        parts.append(payment_link_form(user, item))
        parts.append(f'<form method="post" action="/items/{item_id}/mark-sold" class="card">{csrf_input(user)}'
                     '<h2 style="margin-top:0">Mark as sold</h2><label>Where did it sell?</label>'
                     '<select name="platform"><option>eBay</option><option>In Person</option><option value="Other">Other</option></select>'
                     '<label>If other - where?</label><input type="text" name="other" maxlength="80" placeholder="e.g. Facebook, Mercari">'
                     '<button class="btn ok">Mark sold 🎉</button></form>')
    elif status == db.STATUS_SOLD and user.has(config.ROLE_LISTING_MGMT):
        parts.append('<div class="card">' + post_button(user, f"/items/{item_id}/mark-shipped", "Mark shipped 📦", "btn ok")
                     + "</div>")
    elif status == db.STATUS_REJECTED and user.has(config.ROLE_DATA_ENTRY):
        parts.append(f'<div class="card"><a class="btn primary" href="/intake/redo/{item_id}">Redo with new photos</a></div>')

    if status in (db.STATUS_QUEUE_REVIEW, db.STATUS_AWAITING_LISTING, db.STATUS_LISTED, db.STATUS_SOLD) \
            and user.has(config.ROLE_QUEUE_REVIEW, config.ROLE_LISTING_MGMT):
        reasons = "".join(f'<option value="{esc(k)}">{esc(v)}</option>' for k, v in db.HOLD_REASON_LABELS.items())
        parts.append(f'<details class="card"><summary>Put on hold</summary><form method="post" action="/items/{item_id}/hold">'
                     f'{csrf_input(user)}<label>Reason</label><select name="reason">{reasons}</select>'
                     f'<label>Note (required for Other)</label><input type="text" name="note" maxlength="300">'
                     f'<button class="btn warn">Put on hold</button></form></details>')
    if status == db.STATUS_ON_HOLD and user.has(config.ROLE_LISTING_MGMT):
        parts.append('<div class="card">' + post_button(user, f"/items/{item_id}/resolve-hold", "Resolved - send it back",
                                                         "btn ok") + "</div>")
    return "".join(parts)


# ------------------------------------------------------------ POST handlers

def _parse_listing_fields(form) -> tuple:
    """The same validation as Discord's approve form. Returns (values, errors)."""
    from cogs.item_flow import _parse_dimensions, _parse_weight_lb
    errors = []
    title = (form.get("title") or "").strip()[:80]
    if not title:
        errors.append("Title is required.")
    price = None
    try:
        price = float(form.get("price") or "")
        if price < 0:
            errors.append("Price can't be negative.")
    except ValueError:
        errors.append("Price must be a number.")
    weight = dims = None
    try:
        weight = _parse_weight_lb((form.get("weight") or "").strip())
    except ValueError as e:
        errors.append(str(e))
    try:
        dims = _parse_dimensions((form.get("dims") or "").strip())
    except ValueError as e:
        errors.append(str(e))
    specifics = {}
    for line in (form.get("specifics") or "").splitlines():
        key, _, value = line.partition(":")
        if key.strip() and value.strip():
            specifics[key.strip()] = value.strip()
    return {"title": title, "price": price, "weight": weight, "dims": dims, "specifics": specifics}, errors


async def _item_and_form(request):
    item = db.get_item(int(request.match_info["item_id"]))
    if not item:
        raise web.HTTPNotFound(text="No such item.")
    return item, await request.post()


async def approve(request):
    user = user_of(request)
    require(request, config.ROLE_QUEUE_REVIEW)
    item, form = await _item_and_form(request)
    values, errors = _parse_listing_fields(form)
    condition_id = form.get("condition_id")
    category_id = (form.get("category_id") or "").strip()
    if condition_id not in config.EBAY_CONDITION_LABELS:
        errors.append("Pick a condition.")
    if not category_id.isdigit():
        errors.append("Pick an eBay category.")
    listing_format = "Auction" if form.get("listing_format") == "Auction" else "FixedPrice"
    duration = form.get("auction_duration") if listing_format == "Auction" else None
    if listing_format == "Auction" and duration not in dict(config.EBAY_AUCTION_DURATIONS):
        errors.append("Pick an auction length.")
    if errors:
        _back(item["id"], "Not approved: " + " ".join(errors))
    from cogs.item_flow import ActionError
    try:
        result = await _flow(request).do_approve(
            item["id"], user.discord_id, condition_id, category_id, values["title"], values["price"],
            values["specifics"], listing_format=listing_format, auction_duration=duration,
            weight_lb=values["weight"], length_in=values["dims"][0], width_in=values["dims"][1],
            height_in=values["dims"][2])
    except ActionError as e:
        _back(item["id"], str(e))
    _back(item["id"], _strip_md(result["message"]))


async def save_description(request):
    user = user_of(request)
    require(request, config.ROLE_QUEUE_REVIEW)
    item, form = await _item_and_form(request)
    text = (form.get("description") or "").strip()[:4000]
    from cogs.item_flow import ActionError
    try:
        await _flow(request).do_edit_description(item["id"], text, user.discord_id)
    except ActionError as e:
        _back(item["id"], str(e))
    _back(item["id"], "Description saved.")


async def reject(request):
    user = user_of(request)
    require(request, config.ROLE_QUEUE_REVIEW)
    item, form = await _item_and_form(request)
    from cogs.item_flow import ActionError
    try:
        await _flow(request).do_reject(item["id"], user.discord_id, reason=(form.get("reason") or "")[:300])
    except ActionError as e:
        _back(item["id"], str(e))
    raise web.HTTPSeeOther("/review?" + qs(msg=f"Item #{item['item_number']} sent back to Data Entry."))


async def rereview(request):
    user = user_of(request)
    require(request, config.ROLE_QUEUE_REVIEW)
    item, _ = await _item_and_form(request)
    from cogs.item_flow import ActionError
    flow = _flow(request)
    try:  # checks run right away; the AI pass itself carries on in the background
        flow._expect_status(item["id"], db.STATUS_QUEUE_REVIEW)
    except ActionError as e:
        _back(item["id"], str(e))
    _run_in_background(flow.do_rereview(item["id"], user.discord_id), "re-review")
    raise web.HTTPSeeOther("/review?" + qs(msg=f"Item #{item['item_number']} sent back for AI re-review."))


async def save_details(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    item, form = await _item_and_form(request)
    values, errors = _parse_listing_fields(form)
    if errors:
        _back(item["id"], "Not saved: " + " ".join(errors))
    from cogs.item_flow import ActionError
    try:
        note = await _flow(request).do_save_listing_details(
            item["id"], user.discord_id, values["title"], values["price"], values["specifics"], values["weight"],
            *values["dims"])
    except ActionError as e:
        _back(item["id"], str(e))
    _back(item["id"], _strip_md("Details saved." + note))


async def list_on_ebay(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    item, _ = await _item_and_form(request)
    from cogs.item_flow import ActionError
    try:
        result = await _flow(request).do_list_on_ebay(item["id"], user.discord_id)
    except ActionError as e:
        _back(item["id"], str(e))
    except NotImplementedError as e:
        _back(item["id"], str(e))
    except Exception as e:  # eBay's own message - same as the Discord button shows
        _back(item["id"], f"eBay listing failed: {e} - fix it with Edit details, then try again.")
    _back(item["id"], f"✅ Listed live on eBay (item {result.get('ebay_item_id') or '?'}).")


async def mark_listed(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    item, _ = await _item_and_form(request)
    from cogs.item_flow import ActionError
    try:
        await _flow(request).do_mark_listed(item["id"], user.discord_id)
    except ActionError as e:
        _back(item["id"], str(e))
    _back(item["id"], "Marked listed.")


async def mark_sold(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    item, form = await _item_and_form(request)
    platform = form.get("platform")
    if platform == "Other":
        other = (form.get("other") or "").strip()[:80]
        if not other:
            _back(item["id"], "Say where it sold (e.g. Facebook).")
        platform = f"Other: {other}"
    elif platform not in ("eBay", "In Person"):
        _back(item["id"], "Pick where it sold.")
    from cogs.item_flow import ActionError
    try:
        await _flow(request).do_mark_sold(item["id"], platform, user.discord_id)
    except ActionError as e:
        _back(item["id"], str(e))
    how = ("log it with Confirm & log sale on its eBay order (Finance)" if platform == "eBay"
           else "log the sale price with /finance log-sale in Discord")
    _back(item["id"], f"🎉 Marked sold via {platform}. Next: {how}.")


async def mark_shipped(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    item, _ = await _item_and_form(request)
    from cogs.item_flow import ActionError
    try:
        await _flow(request).do_mark_shipped(item["id"], user.discord_id)
    except ActionError as e:
        _back(item["id"], str(e))
    _back(item["id"], "Marked shipped.")


async def hold(request):
    user = user_of(request)
    require(request, config.ROLE_QUEUE_REVIEW, config.ROLE_LISTING_MGMT)
    item, form = await _item_and_form(request)
    reason, note = form.get("reason"), (form.get("note") or "").strip()[:300]
    if reason not in db.HOLD_REASON_LABELS:
        _back(item["id"], "Pick a hold reason.")
    if reason == db.HOLD_REASON_OTHER and not note:
        _back(item["id"], "Add a note saying why (required for Other).")
    ok = await _flow(request).place_item_on_hold(item, reason, note, user.discord_id)
    _back(item["id"], "Put on hold." if ok else "This item can't be put on hold from where it is.")


async def resolve_hold(request):
    user = user_of(request)
    require(request, config.ROLE_LISTING_MGMT)
    item, _ = await _item_and_form(request)
    if item["status"] != db.STATUS_ON_HOLD:
        _back(item["id"], "This item isn't on hold.")
    import finance_utils
    ok, _stage = await _flow(request).resolve_item_hold(item, user.discord_id)
    await finance_utils.refresh_finance_message(bot_of(request), item["pallet_id"])
    _back(item["id"], "Hold resolved - sent back." if ok else "Couldn't resolve this hold.")


async def category_search(request):
    """JSON: eBay category suggestions for the approve form's search box."""
    require(request, config.ROLE_QUEUE_REVIEW, config.ROLE_LISTING_MGMT)
    q = (request.query.get("q") or "").strip()[:100]
    if not q:
        return web.json_response([])
    try:
        results = await ebay_api.search_categories(q, limit=20)
    except Exception as e:  # eBay down / not configured - fall back to the local list
        log.info("eBay category search failed (%s) - using the local category list", e)
        results = ebay_taxonomy.search(q, limit=20) if hasattr(ebay_taxonomy, "search") else []
    return web.json_response([{"id": str(cid), "path": path} for cid, path in results])


def add_routes(app: web.Application):
    app.router.add_get("/intake", intake_page)
    app.router.add_post("/intake", intake_submit)
    app.router.add_get(r"/intake/redo/{item_id:\d+}", intake_redo_page)
    app.router.add_get("/review", review_list)
    app.router.add_get("/listing", listing_list)
    app.router.add_get("/api/categories", category_search)
    for action, handler in (("approve", approve), ("description", save_description), ("reject", reject),
                            ("rereview", rereview), ("details", save_details), ("list-ebay", list_on_ebay),
                            ("mark-listed", mark_listed), ("mark-sold", mark_sold), ("mark-shipped", mark_shipped),
                            ("hold", hold), ("resolve-hold", resolve_hold)):
        app.router.add_post(rf"/items/{{item_id:\d+}}/{action}", handler)
