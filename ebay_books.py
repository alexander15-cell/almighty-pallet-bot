"""
The rest of the eBay money trail in QuickBooks, after the sale (Confirm &
log sale) and its fees (ebay_fee_expenses.py):

  - refunds & adjustments on an order (REFUND, DISPUTE, CREDIT, ADJUSTMENT,
    ...): a "↩️ eBay refund" card books them against the refunds/sales
    income account, credit Undeposited Funds, and lowers the pallet's net
    revenue (finance_transactions 'refund').
  - account-level charges not tied to any order (store subscription,
    listing fees for items that never sold, ...): one "🧾 eBay account
    charges" card per month, debit the eBay fee/ad accounts, credit
    Undeposited Funds.
  - payouts: once every sale, fee and refund in a payout is booked,
    Undeposited Funds holds exactly what eBay sent. A "🏦 eBay payout" card
    then records the QuickBooks Bank Deposit into the bank account, so the
    bank feed's deposit has something to match.

All three write to the same ebay_fee_postings ledger as the fee cards
(keyed on eBay's own transaction ID), so no eBay transaction can be booked
twice by any card. Nothing reaches QuickBooks without a Confirm click, and
every click re-checks the numbers first.

pallet_profit() is a read-only report for /finance pallet-profit.
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

import config
import database as db
import ebay_api
import ebay_fee_expenses
import ebay_finances
import quickbooks

log = logging.getLogger(__name__)

# Order-linked transaction types the fee cards already book.
_FEE_TYPES = {"SALE", "SHIPPING_LABEL", "NON_SALE_CHARGE"}
# Money movement, not income or cost - never booked.
_NOT_A_COST = ebay_finances._TYPES_NOT_A_COST

_locks = defaultdict(asyncio.Lock)


def _cost(txn: dict) -> float:
    return ebay_fee_expenses._cost(txn)


def _order_ids(txn: dict) -> set:
    ids = {r["id"] for r in txn.get("references") or [] if r["type"] == "ORDER_ID"}
    if txn.get("order_id"):
        ids.add(txn["order_id"])
    return ids


def refunds_account(ebay: bool = True) -> str:
    if config.QUICKBOOKS_REFUNDS_ACCOUNT_ID:
        return config.QUICKBOOKS_REFUNDS_ACCOUNT_ID
    return config.QUICKBOOKS_EBAY_SALES_ACCOUNT_ID if ebay else config.QUICKBOOKS_SALES_INCOME_ACCOUNT_ID


def _label(txn: dict) -> str:
    kind = (txn.get("fee_type") or txn["transaction_type"]).replace("_", " ").title()
    memo = (txn.get("transaction_memo") or "").strip()
    return f"{kind} - {memo}" if memo else kind


# =================================================================== refunds

def order_refund_breakdown(order_id: str) -> dict:
    """Unbooked refunds/adjustments on one eBay order, plus its items and
    whether its sale is logged (from the fee breakdown). charges carry a
    signed 'amount' = what it cost the seller (a credit back is negative)."""
    base = ebay_fee_expenses.order_fee_breakdown(order_id)
    posted = db.get_posted_ebay_fee_keys()
    charges = []
    for txn in db.get_all_ebay_fin_transactions():
        ttype = txn["transaction_type"]
        if ttype in _FEE_TYPES or ttype in _NOT_A_COST or order_id not in _order_ids(txn):
            continue
        if (txn["transaction_id"], ttype) in posted:
            continue
        amount = round(_cost(txn), 2)
        if not amount:
            continue
        charges.append({
            "transaction_id": txn["transaction_id"], "transaction_type": ttype, "category": "refunds",
            "amount": amount, "date": (txn.get("transaction_date") or "")[:10], "label": _label(txn),
        })
    return {
        "order_id": order_id,
        "lines": base["lines"],
        "items": base["items"],
        "sale_logged": base["sale_logged"],
        "charges": charges,
        "total": round(sum(c["amount"] for c in charges), 2),
    }


def orders_needing_refund_cards(since_date: str) -> list:
    out = []
    for order_id in db.get_ebay_order_ids_since(since_date):
        breakdown = order_refund_breakdown(order_id)
        if not breakdown["charges"] or not breakdown["sale_logged"]:
            continue
        keys = [f"{order_id}:{c['transaction_id']}:{c['transaction_type']}" for c in breakdown["charges"]]
        if any(not db.is_ebay_book_carded("refund", k) for k in keys):
            out.append(breakdown)
    return out


def refund_card_keys(breakdown: dict) -> list:
    return [f"{breakdown['order_id']}:{c['transaction_id']}:{c['transaction_type']}" for c in breakdown["charges"]]


def _refund_shares(breakdown: dict) -> list:
    """finance_transactions 'refund' rows: the total split across the
    order's tracked items by item price (cents-exact)."""
    lines = [l for l in breakdown["lines"] if l.get("item")]
    if not lines:
        return []
    weights = [max((l.get("line_item_cost") or 0.0), 0.0) for l in lines]
    if not sum(weights):
        weights = [1.0] * len(lines)
    portions = ebay_finances._shares(weights)
    total_cents = round(breakdown["total"] * 100)
    shares, used = [], 0
    for i, (line, portion) in enumerate(zip(lines, portions)):
        cents = total_cents - used if i == len(lines) - 1 else round(total_cents * portion)
        used += cents
        if cents:
            shares.append({"pallet_id": line["item"]["pallet_id"], "item_id": line["item"]["id"],
                           "amount": cents / 100, "note": f"eBay refund/adjustment - order {breakdown['order_id']}"})
    return shares


async def confirm_order_refund(order_id: str, actor_id: int) -> dict:
    problem = ebay_fee_expenses.setup_problem()
    if problem:
        return {"ok": False, "message": problem}
    async with _locks[f"order:{order_id}"]:
        try:
            await ebay_finances.refresh_order_transactions(order_id)
        except ebay_api.EbayAPIError as e:
            log.warning("Couldn't refresh eBay transactions for order %s (%s) - using last sync", order_id, e)
        breakdown = order_refund_breakdown(order_id)
        if not breakdown["charges"]:
            return {"ok": False, "message": "Every refund/adjustment on this order is already logged - nothing new."}
        if not breakdown["sale_logged"]:
            return {"ok": False, "message": "Log this order's sale first (its **Confirm & log sale** card), then the refund."}
        memo = f"eBay refund/adjustment, order {order_id}"
        lines = [{"account_id": refunds_account(ebay=True), "amount": breakdown["total"], "description": memo}]
        date = max((c["date"] for c in breakdown["charges"] if c["date"]),
                   default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        try:
            je = await quickbooks.create_balanced_journal_entry(
                lines, config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, date, memo)
        except quickbooks.QuickBooksError as e:
            return {"ok": False, "message": f"QuickBooks didn't accept the Journal Entry - nothing was recorded: {e}"}
        try:
            db.record_ebay_refund_posting(order_id, breakdown["charges"], je["id"], actor_id, _refund_shares(breakdown))
        except Exception as e:
            log.exception("eBay refund Journal Entry %s posted but the ledger write failed", je.get("id"))
            return {"ok": False, "message": _ledger_failed(je, e)}
        return {"ok": True, "breakdown": breakdown, "je_doc": je.get("doc_number") or je.get("id"),
                "pallet_ids": sorted({i["pallet_id"] for i in breakdown["items"]})}


def _ledger_failed(qb_txn: dict, error) -> str:
    return (f"⚠️ QuickBooks entry `{qb_txn.get('doc_number') or qb_txn.get('id')}` **was created**, but saving the "
            f"record here failed ({error}). **Don't click again** - that would book it twice. Ask a Pallet Admin "
            "to check the bot's log.")


# =========================================================== account charges

def _order_listing_ids() -> set:
    """eBay listing IDs that appear on any synced order - charges on these
    belong to that order's fee card, not the account card."""
    ids = set()
    for order_id in db.get_ebay_order_ids_since("0000-00-00"):
        for line in db.get_ebay_order_lines_for_order(order_id):
            if line.get("legacy_item_id"):
                ids.add(str(line["legacy_item_id"]))
    return ids


def account_charges(month: str = None) -> list:
    """Unbooked eBay charges/credits not tied to any order (or to a listing
    that sold) - store subscription, insertion fees, account credits... -
    optionally just one YYYY-MM. Each: transaction_id/type, category
    ('fees'/'ads'/'shipping'), signed amount, date, label, listing_id."""
    posted = db.get_posted_ebay_fee_keys()
    sold_listings = _order_listing_ids()
    out = []
    for txn in db.get_all_ebay_fin_transactions():
        ttype = txn["transaction_type"]
        if ttype in ("SALE", "REFUND") or ttype in _NOT_A_COST or _order_ids(txn):
            continue
        listing = next((r["id"] for r in txn.get("references") or [] if r["type"] == "ITEM_ID"), None)
        if listing and listing in sold_listings:
            continue
        if (txn["transaction_id"], ttype) in posted:
            continue
        date = (txn.get("transaction_date") or "")[:10]
        if month and not date.startswith(month):
            continue
        amount = round(_cost(txn), 2)
        if not amount:
            continue
        if ttype == "SHIPPING_LABEL":
            category = "shipping"
        elif "AD" in (txn.get("fee_type") or "").upper().split("_"):
            category = "ads"
        else:
            category = "fees"
        out.append({"transaction_id": txn["transaction_id"], "transaction_type": ttype, "category": category,
                    "amount": amount, "date": date, "label": _label(txn), "listing_id": listing})
    return out


def account_charge_months(before_month: str = None) -> list:
    """YYYY-MM months that have unbooked account charges, oldest first;
    before_month excludes that month and later (the auto card waits for a
    month to finish)."""
    months = sorted({c["date"][:7] for c in account_charges() if c["date"]})
    return [m for m in months if not before_month or m < before_month]


def account_breakdown(month: str) -> dict:
    charges = account_charges(month)
    totals = {cat: round(sum(c["amount"] for c in charges if c["category"] == cat), 2)
              for cat in ebay_fee_expenses.CATEGORY_LABELS}
    totals["total"] = round(sum(totals.values()), 2)
    return {"month": month, "charges": charges, "totals": totals}


def account_card_keys(breakdown: dict) -> list:
    return [f"{breakdown['month']}:{c['transaction_id']}:{c['transaction_type']}" for c in breakdown["charges"]]


def _account_pallet_costs(breakdown: dict) -> list:
    """A listing fee for a tracked item (that hasn't sold) still goes on
    that item's pallet; everything else is business overhead, no pallet."""
    rows = []
    for c in breakdown["charges"]:
        item = db.get_item_by_ebay_listing_id(c["listing_id"]) if c.get("listing_id") else None
        if item:
            rows.append({"pallet_id": item["pallet_id"], "item_id": item["id"],
                         "cost_type": ebay_fee_expenses.COST_TYPES[c["category"]], "amount": c["amount"],
                         "description": f"{c['label']} - eBay listing {c['listing_id']}"})
    return rows


async def confirm_account_charges(month: str, actor_id: int) -> dict:
    problem = ebay_fee_expenses.setup_problem()
    if problem:
        return {"ok": False, "message": problem}
    async with _locks[f"account:{month}"]:
        breakdown = account_breakdown(month)
        if not breakdown["charges"]:
            return {"ok": False, "message": f"Every eBay account charge for {month} is already logged - nothing new."}
        memo = f"eBay account charges, {month}"
        lines = [{"account_id": ebay_fee_expenses.account_for(cat), "amount": breakdown["totals"][cat],
                  "description": f"{ebay_fee_expenses.CATEGORY_LABELS[cat]} - {memo}"}
                 for cat in ebay_fee_expenses.CATEGORY_LABELS if breakdown["totals"][cat]]
        date = max(c["date"] for c in breakdown["charges"])
        try:
            je = await quickbooks.create_balanced_journal_entry(
                lines, config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, date, memo)
        except quickbooks.QuickBooksError as e:
            return {"ok": False, "message": f"QuickBooks didn't accept the Journal Entry - nothing was recorded: {e}"}
        try:
            db.record_ebay_fee_posting(f"account:{month}", breakdown["charges"], je["id"], actor_id,
                                       _account_pallet_costs(breakdown))
        except Exception as e:
            log.exception("eBay account-charge Journal Entry %s posted but the ledger write failed", je.get("id"))
            return {"ok": False, "message": _ledger_failed(je, e)}
        return {"ok": True, "breakdown": breakdown, "je_doc": je.get("doc_number") or je.get("id"),
                "pallet_ids": sorted({r["pallet_id"] for r in _account_pallet_costs(breakdown)})}


# =================================================================== payouts

def payout_breakdown(payout_id: str) -> dict:
    """
    Everything in one eBay payout, checked against what's booked:
      sales     - [{'sale_id', 'receipt_id', 'amount', 'order_id'}] logged
                  sales whose eBay SALE is in this payout (linked on the
                  Deposit)
      booked    - total already credited to Undeposited Funds for this
                  payout's fees/labels/refunds (ebay_fee_postings)
      expected  - sales total minus booked: what Undeposited Funds holds
                  for this payout
      problems  - plain-English reasons it can't be deposited yet
      ready     - no problems and expected matches the payout to the cent
    """
    payout = db.get_ebay_payout(payout_id)
    deposit = db.get_ebay_payout_deposit(payout_id)
    postings = db.get_ebay_fee_postings_by_key()
    problems, sales, seen_sales = [], [], set()
    booked = 0.0
    counts = defaultdict(float)
    for txn in db.get_ebay_fin_transactions_for_payout(payout_id):
        ttype, key = txn["transaction_type"], (txn["transaction_id"], txn["transaction_type"])
        if ttype in _NOT_A_COST:
            problems.append(f"eBay {ttype.lower()} of ${txn.get('amount') or 0:.2f} in this payout isn't something "
                            "the bot books - record it by hand in QuickBooks, then deposit this payout by hand.")
            continue
        if ttype == "SALE":
            order_id = txn.get("order_id") or next(iter(_order_ids(txn)), None)
            fee = round(txn.get("total_fee_amount") or 0.0, 2)
            if fee:
                if key in postings:
                    booked += postings[key]["amount"]
                    counts["fees"] += postings[key]["amount"]
                else:
                    problems.append(f"Order `{order_id}`: its eBay fees aren't logged yet - confirm its 💸 fee card.")
            items = ebay_fee_expenses.order_fee_breakdown(order_id)["items"] if order_id else []
            order_sales = {s["id"]: s for s in (db.get_sale_for_item(i["id"]) for i in items) if s}
            if not order_sales:
                problems.append(f"Order `{order_id}`: its sale isn't logged yet - confirm its **Confirm & log sale** card.")
                continue
            for sale in order_sales.values():
                if sale["id"] in seen_sales:
                    continue
                seen_sales.add(sale["id"])
                if not sale.get("quickbooks_sales_receipt_id"):
                    problems.append(f"Order `{order_id}`: sale #{sale['id']} isn't in QuickBooks yet - "
                                    f"`/finance retry-sale sale_id:{sale['id']}`.")
                    continue
                sales.append({"sale_id": sale["id"], "receipt_id": sale["quickbooks_sales_receipt_id"],
                              "amount": round(sale["total_price"], 2), "order_id": order_id})
            continue
        amount = round(_cost(txn), 2)
        if not amount:
            continue
        if key in postings:
            booked += postings[key]["amount"]
            counts[postings[key]["category"]] += postings[key]["amount"]
        else:
            where = next(iter(_order_ids(txn)), None)
            month = (txn.get("transaction_date") or "")[:7]
            hint = (f"order `{where}`'s card" if where
                    else f"the 🧾 eBay account charges card for {month} (`/ebay account-charges month:{month}`)")
            problems.append(f"{_label(txn)} ${amount:.2f} isn't logged yet - confirm {hint}.")
    sales_total = round(sum(s["amount"] for s in sales), 2)
    booked = round(booked, 2)
    expected = round(sales_total - booked, 2)
    amount = round((payout or {}).get("amount") or 0.0, 2)
    if payout is None:
        problems.append("This payout hasn't been synced - run `/ebay sync-finances`.")
    elif not problems and abs(expected - amount) > 0.01:
        problems.append(f"The logged sales minus booked fees come to ${expected:.2f}, but eBay paid ${amount:.2f} - "
                        "something in this payout is booked differently. Check `/ebay net-report` before depositing.")
    return {
        "payout_id": payout_id, "payout": payout, "deposit": deposit, "sales": sales, "sales_total": sales_total,
        "booked": booked, "booked_by_category": {k: round(v, 2) for k, v in counts.items()},
        "expected": expected, "amount": amount, "problems": problems,
        "ready": payout is not None and not problems and deposit is None,
    }


def payouts_needing_cards(since_date: str) -> list:
    """Payouts that reached the bank on/after since_date with no card yet."""
    return [p for p in db.list_ebay_payouts(since_date, limit=200)
            if p.get("payout_status") == "SUCCEEDED" and not db.get_ebay_payout_deposit(p["payout_id"])
            and not db.is_ebay_book_carded("payout", p["payout_id"])]


def payout_setup_problem() -> str:
    problem = ebay_fee_expenses.setup_problem()
    if problem:
        return problem
    if not config.QUICKBOOKS_BANK_ACCOUNT_ID:
        return "`QUICKBOOKS_BANK_ACCOUNT_ID` isn't set - the bank account to deposit into. Find it with `/finance list-accounts`."
    return ""


async def confirm_payout(payout_id: str, actor_id: int) -> dict:
    """
    Records the payout in QuickBooks: a Bank Deposit into the bank account
    linking each sale's Sales Receipt, with the already-booked fees taken
    back out of Undeposited Funds so the deposit equals the payout. If
    QuickBooks won't take that Deposit, falls back to a Journal Entry
    (debit bank, credit Undeposited Funds) for the same amount - balances
    end up identical, but the Sales Receipts then still show as "waiting"
    on QuickBooks' Bank Deposit screen (the reply says so).
    """
    problem = payout_setup_problem()
    if problem:
        return {"ok": False, "message": problem}
    async with _locks[f"payout:{payout_id}"]:
        if db.get_ebay_payout_deposit(payout_id):
            return {"ok": False, "message": "This payout is already recorded in QuickBooks."}
        b = payout_breakdown(payout_id)
        if not b["ready"]:
            return {"ok": False, "message": "Not ready to deposit yet:\n" + "\n".join(f"• {p}" for p in b["problems"])}
        payout = b["payout"]
        date = (payout.get("payout_date") or "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        memo = f"eBay payout {payout_id}"
        fallback = False
        try:
            qb_txn = await quickbooks.create_deposit(
                config.QUICKBOOKS_BANK_ACCOUNT_ID, date, memo,
                [{"id": s["receipt_id"], "amount": s["amount"]} for s in b["sales"]],
                config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, b["booked"])
            txn_type = "Deposit"
        except quickbooks.QuickBooksError as e:
            log.warning("QuickBooks refused the Deposit for eBay payout %s (%s) - falling back to a Journal Entry",
                        payout_id, e)
            try:
                qb_txn = await quickbooks.create_balanced_journal_entry(
                    [{"account_id": config.QUICKBOOKS_BANK_ACCOUNT_ID, "amount": b["amount"], "description": memo}],
                    config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, date, memo)
            except quickbooks.QuickBooksError as e2:
                return {"ok": False, "message": f"QuickBooks didn't accept the deposit - nothing was recorded: {e2}"}
            txn_type, fallback = "JournalEntry", True
        try:
            db.record_ebay_payout_deposit(payout_id, qb_txn["id"], txn_type, b["amount"],
                                          config.QUICKBOOKS_BANK_ACCOUNT_ID, actor_id)
        except Exception as e:
            log.exception("eBay payout %s %s posted but the ledger write failed", payout_id, txn_type)
            return {"ok": False, "message": _ledger_failed(qb_txn, e)}
        return {"ok": True, "breakdown": b, "txn_type": txn_type, "doc": qb_txn.get("doc_number") or qb_txn.get("id"),
                "fallback": fallback}


# ============================================================ pallet profit

def _days(start: str, end: str):
    s, e = ebay_finances._parse_dt(start), ebay_finances._parse_dt(end)
    if not s or not e:
        return None
    if s.tzinfo is None:
        s = s.replace(tzinfo=timezone.utc)
    if e.tzinfo is None:
        e = e.replace(tzinfo=timezone.utc)
    return max((e - s).total_seconds() / 86400, 0)


def pallet_profit(pallet_id: int) -> dict:
    """
    The real profit picture for one pallet - read-only, built from the
    same numbers as the live card (get_pallet_financials) plus a breakdown:
    what the pallet cost, what eBay kept, refunds, what's left to sell.
    """
    fin = db.get_pallet_financials(pallet_id)
    inputs = db.get_pallet_profit_inputs(pallet_id)
    costs = inputs["costs_by_type"]
    items = inputs["items"]
    sold_statuses = {db.STATUS_SOLD, db.STATUS_SHIPPED}
    sold = [i for i in items if i["status"] in sold_statuses or i["sale_price"] is not None]
    unsold = [i for i in items if i not in sold]
    days = [d for d in (_days(i["created_at"], i["sale_recorded_at"]) for i in sold) if d is not None]
    ebay_costs = round(sum(costs.get(k, 0.0) for k in ("ebay_fees", "ebay_ads")), 2)
    shipping = round(costs.get("shipping", 0.0), 2)
    purchase = round((fin["manual_pallet_cost"] or 0.0) + sum(
        v for k, v in costs.items() if k not in ("ebay_fees", "ebay_ads", "shipping")), 2)
    revenue = round(fin["revenue_so_far"], 2)
    refunds = round(fin["refunds_total"], 2)
    other_expenses = round(fin["expenses_total"], 2)
    profit = fin["profit_so_far"]
    unsold_value = round(sum(i["list_price"] or 0.0 for i in unsold), 2)
    unpriced = sum(1 for i in unsold if not i["list_price"])
    total_items = len(items)
    # Projection: if the rest sells at its listed price and eBay keeps the
    # same share it has so far (fees + labels as a % of revenue).
    keep_rate = ((ebay_costs + shipping) / revenue) if revenue else 0.0
    projected = (round(profit + unsold_value * (1 - keep_rate), 2) if profit is not None else None)
    return {
        "pallet": fin["pallet"],
        "purchase_cost": purchase, "ebay_costs": ebay_costs, "shipping": shipping,
        "total_cost": round(fin["cost"], 2) if fin["cost"] is not None else None,
        "revenue": revenue, "refunds": refunds, "other_expenses": other_expenses,
        "net_revenue": round(fin["net_revenue"], 2),
        "profit": round(profit, 2) if profit is not None else None,
        "roi_pct": (profit / purchase * 100) if (profit is not None and purchase) else None,
        "items_total": total_items, "items_sold": len(sold), "items_unsold": len(unsold),
        "sell_through_pct": (len(sold) / total_items * 100) if total_items else None,
        "avg_days_to_sell": (sum(days) / len(days)) if days else None,
        "avg_sale": (revenue / len(sold)) if sold else None,
        "unsold_list_value": unsold_value, "unpriced_unsold": unpriced,
        "ebay_keep_pct": keep_rate * 100 if revenue else None,
        "projected_profit": projected,
    }
