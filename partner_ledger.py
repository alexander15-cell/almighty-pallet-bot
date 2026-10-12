"""
Partner ledger - who has put money into the business, who fronted business
expenses out of pocket and is owed for them, what's been paid back, and
each partner's equal share of the profit (/partners commands, cogs/
partners.py).

A RECORD ONLY: the bot never moves money, and nothing here is posted to
QuickBooks - same boundary as the rest of the bot (see README, "QuickBooks
integration is read/allocate/report only"). Paying someone happens outside
the bot; recording it here keeps everyone's balance straight.

Entry types (database.partner_ledger):
  contribution  - partner put money into the business
  fronted       - partner paid a business expense personally; the business
                  owes it back. The expense itself is still recorded the
                  normal way (/finance setprice, #submit-invoices, /finance
                  expense) - this only records WHO paid it.
  reimbursement - the business paid a partner back for fronted costs
  payout        - the business paid a partner part of the profit

The split is EQUAL between active partners. Profit to date comes from the
bot's own pallet numbers - every pallet's net revenue (sales minus refunds
and expenses) minus its cost basis - so it's only as complete as what's
been recorded (a pallet with sales but no cost entered is flagged, since it
would overstate profit).

Per partner:
  equity       = contributed + profit share - payouts taken
  owed to them = fronted - reimbursed
  behind by    = (most payouts any active partner has taken) - theirs,
                 i.e. what they'd need to catch up for payouts to be equal
"""
from datetime import datetime, timezone

import database as db

ENTRY_TYPES = {
    "contribution": "Money put in",
    "fronted": "Expense paid personally",
    "reimbursement": "Reimbursed",
    "payout": "Profit payout",
}


def parse_entry_date(text: str) -> str:
    """YYYY-MM-DD from optional user input (default today); raises
    ValueError with a readable message."""
    if not text or not text.strip():
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("date must look like 2026-10-12")


def business_profit() -> dict:
    """Profit to date from the bot's own numbers, across every pallet
    (archived ones too): {'net_revenue', 'cost', 'profit',
    'pallets_missing_cost': [names of pallets with sales but no cost]}."""
    net_revenue = cost = 0.0
    missing = []
    for pallet in db.get_all_pallets(include_archived=True):
        fin = db.get_pallet_financials(pallet["id"])
        net_revenue += fin["net_revenue"] or 0.0
        if fin["cost"] is None:
            if fin["revenue_so_far"]:
                missing.append(pallet["name"])
        else:
            cost += fin["cost"]
    return {
        "net_revenue": round(net_revenue, 2),
        "cost": round(cost, 2),
        "profit": round(net_revenue - cost, 2),
        "pallets_missing_cost": missing,
    }


def partner_totals(partner_id: int) -> dict:
    totals = {t: 0.0 for t in ENTRY_TYPES}
    for entry in db.get_partner_ledger(partner_id):
        totals[entry["entry_type"]] += entry["amount"]
    return {t: round(v, 2) for t, v in totals.items()}


def owed_to_partner(partner_id: int) -> float:
    totals = partner_totals(partner_id)
    return round(totals["fronted"] - totals["reimbursement"], 2)


def summary() -> dict:
    """Everything /partners summary shows. Pure read."""
    profit = business_profit()
    partners = db.get_partners(active_only=True)
    share = round(profit["profit"] / len(partners), 2) if partners else 0.0
    rows = []
    for partner in partners:
        totals = partner_totals(partner["id"])
        rows.append({
            "partner": partner,
            "contributed": totals["contribution"],
            "fronted": totals["fronted"],
            "reimbursed": totals["reimbursement"],
            "owed": round(totals["fronted"] - totals["reimbursement"], 2),
            "payouts": totals["payout"],
            "profit_share": share,
            "equity": round(totals["contribution"] + share - totals["payout"], 2),
        })
    most_paid = max((r["payouts"] for r in rows), default=0.0)
    for row in rows:
        row["behind"] = round(most_paid - row["payouts"], 2)
    return {
        "profit": profit,
        "share": share,
        "partners": rows,
        "total_owed": round(sum(r["owed"] for r in rows), 2),
        "total_contributed": round(sum(r["contributed"] for r in rows), 2),
        "total_payouts": round(sum(r["payouts"] for r in rows), 2),
    }
