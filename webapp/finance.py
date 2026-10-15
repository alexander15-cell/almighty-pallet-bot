"""
Finance pages (Finance Management): what needs confirming, pallet profit,
logged sales, and eBay payouts. Every Confirm here calls the same function
as the matching #ebay-orders card's button (log_ebay_order_sale,
confirm_order_fees, confirm_order_refund, confirm_account_charges,
confirm_payout), so whichever side is clicked first books it and the other
just says it's already logged.
"""
import logging
from datetime import datetime, timezone

from aiohttp import web

import config
import database as db
import ebay_books
import ebay_fee_expenses
import finance_utils
from webapp.layout import esc, money, page, post_button, qs
from webapp.server import bot_of, html_response, require, user_of

log = logging.getLogger(__name__)


def _since() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return config.EBAY_FEE_CARDS_START_DATE or db.get_ebay_fee_card_start_date(today)


def _items_text(items: list) -> str:
    return ", ".join(f"{esc((db.get_pallet(i['pallet_id']) or {}).get('name', '?'))} #{i['item_number']}" for i in items)


def pending_actions() -> dict:
    """Everything waiting on a Finance confirm, from the synced eBay data."""
    since = _since()
    sales, fees, refunds = [], [], []
    for order_id in db.get_ebay_order_ids_since(since):
        b = ebay_fee_expenses.order_fee_breakdown(order_id)
        if not b["items"]:
            continue
        if not b["sale_logged"]:
            if not any(db.get_item_sale(i["id"]) for i in b["items"]):
                sales.append(b)
            continue
        if b["charges"]:
            fees.append(b)
        r = ebay_books.order_refund_breakdown(order_id)
        if r["charges"]:
            refunds.append(r)
    accounts = [ebay_books.account_breakdown(m) for m in ebay_books.account_charge_months()]
    payouts = [ebay_books.payout_breakdown(p["payout_id"]) for p in db.list_ebay_payouts(since, limit=30)
               if p.get("payout_status") == "SUCCEEDED" and not db.get_ebay_payout_deposit(p["payout_id"])]
    return {"sales": sales, "fees": fees, "refunds": refunds, "accounts": [a for a in accounts if a["charges"]],
            "payouts": payouts}


def _section(title: str, rows: list, empty: str) -> str:
    body = "".join(rows) if rows else f"<p class='muted'>{esc(empty)}</p>"
    return f"<h2>{esc(title)}</h2><div class='card'><ul class='list'>{body}</ul></div>"


async def finance_home(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    p = pending_actions()
    sale_rows = [f"<li><div class='t'><b>eBay order {esc(b['order_id'])}</b>{_items_text(b['items'])}</div>"
                 + post_button(user, f"/finance/sale/{b['order_id']}", "Confirm & log sale", "btn ok small",
                               confirm="Log this eBay sale to QuickBooks?") + "</li>" for b in p["sales"]]
    fee_rows = [f"<li><div class='t'><b>Fees for {esc(b['order_id'])}</b>{_items_text(b['items'])} · "
                f"fees {money(b['totals']['fees'])}, labels {money(b['totals']['shipping'])}, ads {money(b['totals']['ads'])}</div>"
                f"<b>{money(b['totals']['total'])}</b>"
                + post_button(user, f"/finance/fees/{b['order_id']}", "Confirm & log fees", "btn ok small") + "</li>"
                for b in p["fees"]]
    refund_rows = [f"<li><div class='t'><b>Refund on {esc(b['order_id'])}</b>{_items_text(b['items'])} · "
                   + esc(", ".join(c["label"] for c in b["charges"])) + f"</div><b>{money(b['total'])}</b>"
                   + post_button(user, f"/finance/refund/{b['order_id']}", "Confirm & log refund", "btn bad small") + "</li>"
                   for b in p["refunds"]]
    account_rows = [f"<li><div class='t'><b>eBay account charges {esc(a['month'])}</b>"
                    + esc(", ".join(f"{c['label']} {money(c['amount'])}" for c in a["charges"][:6])) +
                    f"</div><b>{money(a['totals']['total'])}</b>"
                    + post_button(user, f"/finance/account/{a['month']}", "Confirm & log charges", "btn ok small") + "</li>"
                    for a in p["accounts"]]
    payout_rows = []
    for b in p["payouts"]:
        date = ((b["payout"] or {}).get("payout_date") or "")[:10]
        if b["ready"]:
            action = post_button(user, f"/finance/payout/{b['payout_id']}", "Record deposit", "btn ok small",
                                 confirm=f"Record the {money(b['amount'])} payout as a Bank Deposit?")
            detail = f"{len(b['sales'])} sale(s), {money(b['booked'])} fees/refunds - all logged"
        else:
            action = ""
            detail = "Waiting on: " + esc(" · ".join(b["problems"][:3])) + (" …" if len(b["problems"]) > 3 else "")
        payout_rows.append(f"<li><div class='t'><b>Payout {money(b['amount'])} · {esc(date)}</b>{detail}</div>{action}</li>")
    body = ("<h1>Finance</h1><div class='tabs'><a class='on' href='/finance'>To confirm</a>"
            "<a href='/finance/pallets'>Pallet profit</a><a href='/finance/sales'>Sales</a>"
            "<a href='/finance/payouts'>Payouts</a></div>"
            "<p class='muted'>Built from the last eBay sync (hourly). Confirming here or on the Discord card is "
            "the same thing - the other one will just say it's already logged.</p>"
            + _section("eBay sales to log", sale_rows, "Every eBay sale is logged.")
            + _section("eBay fees to log", fee_rows, "No eBay fees waiting.")
            + _section("Refunds to log", refund_rows, "No refunds waiting.")
            + _section("eBay account charges", account_rows, "No account charges waiting.")
            + _section("Payouts to deposit", payout_rows, "No payouts waiting."))
    return html_response(page(user, "Finance", body, active="finance", flash=request.query.get("msg")))


def _done(msg: str):
    raise web.HTTPSeeOther("/finance?" + qs(msg=msg.replace("**", "").replace("`", "")))


async def _refresh(bot, pallet_ids):
    for pallet_id in pallet_ids:
        await finance_utils.refresh_finance_message(bot, pallet_id)


async def confirm_sale(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    from cogs.ebay import log_ebay_order_sale
    result = await log_ebay_order_sale(bot_of(request), request.match_info["order_id"], user.discord_id)
    _done(result["message"])


async def confirm_fees(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    order_id = request.match_info["order_id"]
    result = await ebay_fee_expenses.confirm_order_fees(order_id, actor_id=user.discord_id)
    if not result["ok"]:
        _done(result["message"])
    await _refresh(bot_of(request), result["pallet_ids"])
    t = result["breakdown"]["totals"]
    await finance_utils.post_to_finance_audit_log(
        bot_of(request), f"💸 **eBay fees** for order `{order_id}` - {money(t['total'])} (Journal Entry "
                         f"`{result['je_doc']}`; credited Undeposited Funds) - via the web dashboard")
    _done(f"✅ Logged {money(t['total'])} of eBay fees for {order_id} (Journal Entry {result['je_doc']}).")


async def confirm_refund(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    order_id = request.match_info["order_id"]
    result = await ebay_books.confirm_order_refund(order_id, actor_id=user.discord_id)
    if not result["ok"]:
        _done(result["message"])
    await _refresh(bot_of(request), result["pallet_ids"])
    total = result["breakdown"]["total"]
    await finance_utils.post_to_finance_audit_log(
        bot_of(request), f"↩️ **eBay refund** for order `{order_id}` - {money(total)} (Journal Entry "
                         f"`{result['je_doc']}`) - via the web dashboard")
    _done(f"✅ Logged the {money(total)} refund for {order_id} (Journal Entry {result['je_doc']}).")


async def confirm_account(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    month = request.match_info["month"]
    result = await ebay_books.confirm_account_charges(month, actor_id=user.discord_id)
    if not result["ok"]:
        _done(result["message"])
    await _refresh(bot_of(request), result["pallet_ids"])
    total = result["breakdown"]["totals"]["total"]
    await finance_utils.post_to_finance_audit_log(
        bot_of(request), f"🧾 **eBay account charges** for {month} - {money(total)} (Journal Entry "
                         f"`{result['je_doc']}`) - via the web dashboard")
    _done(f"✅ Logged {money(total)} of eBay account charges for {month}.")


async def confirm_payout(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    payout_id = request.match_info["payout_id"]
    result = await ebay_books.confirm_payout(payout_id, actor_id=user.discord_id)
    if not result["ok"]:
        _done(result["message"])
    kind = "Bank Deposit" if result["txn_type"] == "Deposit" else "Journal Entry"
    await finance_utils.post_to_finance_audit_log(
        bot_of(request), f"🏦 **eBay payout** `{payout_id}` - {money(result['breakdown']['amount'])} recorded "
                         f"({kind} `{result['doc']}`) - via the web dashboard")
    extra = (" QuickBooks wouldn't take a linked deposit, so it's a Journal Entry - don't deposit those Sales "
             "Receipts again on QuickBooks' Bank Deposit screen." if result["fallback"] else "")
    _done(f"✅ Recorded the {money(result['breakdown']['amount'])} payout as {kind} {result['doc']}. "
          f"Now Match it in QuickBooks → Banking.{extra}")


async def pallets_page(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    rows = sorted((ebay_books.pallet_profit(p["id"]) for p in db.get_all_pallets(include_archived=True)),
                  key=lambda r: (r["roi_pct"] is None, -(r["roi_pct"] or 0)))
    trs = "".join(
        f"<tr><td><a href='/finance/pallets/{r['pallet']['id']}'>{esc(r['pallet']['name'])}</a></td>"
        f"<td class='num'>{money(r['total_cost'])}</td><td class='num'>{money(r['net_revenue'])}</td>"
        f"<td class='num'>{money(r['profit'])}</td>"
        f"<td class='num'>{'%+.0f%%' % r['roi_pct'] if r['roi_pct'] is not None else '-'}</td>"
        f"<td class='num'>{r['items_sold']}/{r['items_total']}</td></tr>" for r in rows)
    body = ("<h1>Pallet profit</h1><div class='tabs'><a href='/finance'>To confirm</a>"
            "<a class='on' href='/finance/pallets'>Pallet profit</a><a href='/finance/sales'>Sales</a>"
            "<a href='/finance/payouts'>Payouts</a></div><div class='card scroll'><table><tr><th>Pallet</th>"
            "<th class='num'>Cost</th><th class='num'>Net sales</th><th class='num'>Profit</th>"
            f"<th class='num'>Return</th><th class='num'>Sold</th></tr>{trs}</table></div>")
    return html_response(page(user, "Pallet profit", body, active="finance"))


async def pallet_detail(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    pallet = db.get_pallet(int(request.match_info["pallet_id"]))
    if not pallet:
        raise web.HTTPNotFound()
    r = ebay_books.pallet_profit(pallet["id"])

    def row(k, v):
        return f"<tr><th>{esc(k)}</th><td class='num'>{v}</td></tr>"
    money_in = (row("Sales", money(r["revenue"])) + row("Refunds", money(-r["refunds"])) +
                (row("Other adjustments", money(-r["other_expenses"])) if r["other_expenses"] else "") +
                row("Net", f"<b>{money(r['net_revenue'])}</b>"))
    money_out = (row("Pallet purchase", money(r["purchase_cost"])) + row("eBay fees & ads", money(r["ebay_costs"])) +
                 row("Shipping labels", money(r["shipping"])) + row("Total", f"<b>{money(r['total_cost'])}</b>"))
    selling = (row("Sold", f"{r['items_sold']} of {r['items_total']}"
                   + (f" ({r['sell_through_pct']:.0f}%)" if r["sell_through_pct"] is not None else "")) +
               row("Average sale", money(r["avg_sale"])) +
               row("Average days to sell", f"{r['avg_days_to_sell']:.0f}" if r["avg_days_to_sell"] is not None else "-") +
               row("eBay fees + labels", f"{r['ebay_keep_pct']:.0f}% of sales" if r["ebay_keep_pct"] is not None else "-") +
               row("Unsold, at list price", money(r["unsold_list_value"])) +
               row("If the rest sells at list", money(r["projected_profit"])))
    result = ("No cost recorded yet." if r["profit"] is None else
              f"{'Profit' if r['profit'] >= 0 else 'Loss'} so far: <b>{money(r['profit'])}</b>"
              + (f" ({r['roi_pct']:+.0f}% on the purchase price)" if r["roi_pct"] is not None else ""))
    body = (f"<p><a href='/finance/pallets'>‹ All pallets</a></p><h1>{esc(pallet['name'])}</h1>"
            f"<div class='card'><p style='margin:0'>{result}</p></div>"
            f"<div class='row'><div class='card'><h2 style='margin-top:0'>Money in</h2><table>{money_in}</table></div>"
            f"<div class='card'><h2 style='margin-top:0'>Money out</h2><table>{money_out}</table></div></div>"
            f"<div class='card'><h2 style='margin-top:0'>Selling</h2><table>{selling}</table></div>"
            f"<p><a class='btn' href='/inventory?{qs(pallet=pallet['id'])}'>See this pallet's items</a></p>")
    return html_response(page(user, pallet["name"], body, active="finance"))


async def sales_page(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    platform = (request.query.get("platform") or "").strip() or None
    rows = db.list_logged_sales(limit=100, platform=platform)
    trs = []
    for s in rows:
        items = ", ".join(f"{esc(i['pallet_name'])} #{i['item_number']}" for i in s["items"]) or "(no items)"
        if s["voided"]:
            state = "<span class='muted'>reversed</span>"
        elif s.get("quickbooks_sales_receipt_id"):
            state = f"Receipt {esc(s['quickbooks_sales_receipt_id'])}"
        else:
            state = f"⚠️ not in QuickBooks - <code>/finance retry-sale sale_id:{s['id']}</code>"
        trs.append(f"<tr{' class=muted' if s['voided'] else ''}><td>#{s['id']}</td><td>{esc(s['sale_date'])}</td>"
                   f"<td>{esc(s['platform'])}</td><td class='num'>{money(s['total_price'])}</td>"
                   f"<td>{items}</td><td>{state}</td></tr>")
    filters = "".join(f"<a href='/finance/sales?{qs(platform=p)}' class='{'on' if (platform or '') == p else ''}'>"
                      f"{esc(p or 'All')}</a>" for p in ("", "eBay", "In Person", "Other"))
    body = ("<h1>Logged sales</h1><div class='tabs'><a href='/finance'>To confirm</a><a href='/finance/pallets'>"
            "Pallet profit</a><a class='on' href='/finance/sales'>Sales</a><a href='/finance/payouts'>Payouts</a></div>"
            f"<div class='tabs'>{filters}</div><div class='card scroll'><table><tr><th>Sale</th><th>Date</th>"
            "<th>Platform</th><th class='num'>Total</th><th>Items</th><th>QuickBooks</th></tr>"
            f"{''.join(trs) or '<tr><td colspan=6 class=muted>No sales logged yet.</td></tr>'}</table></div>"
            "<p class='muted'>To undo one: void its Sales Receipt in QuickBooks, then <code>/finance reverse-sale</code> "
            "in Discord.</p>")
    return html_response(page(user, "Sales", body, active="finance"))


async def payouts_page(request):
    user = user_of(request)
    require(request, config.ROLE_FINANCE_MGMT)
    trs = []
    for p in db.list_ebay_payouts(limit=30):
        b = ebay_books.payout_breakdown(p["payout_id"])
        if b["deposit"]:
            state = f"✅ {esc(b['deposit']['quickbooks_txn_type'])} {esc(b['deposit']['quickbooks_txn_id'])}"
        elif p.get("payout_status") != "SUCCEEDED":
            state = f"⏳ {esc((p.get('payout_status') or 'pending').lower())}"
        elif b["ready"]:
            state = post_button(user, f"/finance/payout/{p['payout_id']}", "Record deposit", "btn ok small")
        else:
            state = "⚠️ " + esc(b["problems"][0]) if b["problems"] else "-"
        trs.append(f"<tr><td>{esc((p.get('payout_date') or '')[:10])}</td><td class='num'>{money(p.get('amount'))}</td>"
                   f"<td>{esc(p.get('instrument_nickname') or '')}</td><td>{state}</td></tr>")
    body = ("<h1>eBay payouts</h1><div class='tabs'><a href='/finance'>To confirm</a><a href='/finance/pallets'>"
            "Pallet profit</a><a href='/finance/sales'>Sales</a><a class='on' href='/finance/payouts'>Payouts</a></div>"
            "<div class='card scroll'><table><tr><th>Date</th><th class='num'>Amount</th><th>To</th><th>QuickBooks</th></tr>"
            f"{''.join(trs) or '<tr><td colspan=4 class=muted>No payouts synced yet.</td></tr>'}</table></div>")
    return html_response(page(user, "Payouts", body, active="finance"))


def add_routes(app: web.Application):
    app.router.add_get("/finance", finance_home)
    app.router.add_get("/finance/pallets", pallets_page)
    app.router.add_get(r"/finance/pallets/{pallet_id:\d+}", pallet_detail)
    app.router.add_get("/finance/sales", sales_page)
    app.router.add_get("/finance/payouts", payouts_page)
    app.router.add_post("/finance/sale/{order_id}", confirm_sale)
    app.router.add_post("/finance/fees/{order_id}", confirm_fees)
    app.router.add_post("/finance/refund/{order_id}", confirm_refund)
    app.router.add_post(r"/finance/account/{month:\d{4}-\d{2}}", confirm_account)
    app.router.add_post("/finance/payout/{payout_id}", confirm_payout)
