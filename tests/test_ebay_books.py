"""
ebay_books.py: eBay refunds, account-level charges, payout deposits, and
the pallet-profit report. Same rules as the fee cards - nothing reaches
QuickBooks without a confirm, every confirm re-checks, and no eBay
transaction can be booked twice by any card.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import ebay_books
import ebay_fee_expenses as fees
import ebay_finances
import quickbooks
from tests.test_ebay_fee_expenses import _item, _sku, _store_order, _store_txn, _amt


def _store_payout(db, payout_id, amount, status="SUCCEEDED", date="2026-03-05T10:00:00.000Z"):
    db.upsert_ebay_payout(ebay_finances.parse_payout({
        "payoutId": payout_id, "payoutStatus": status, "amount": _amt(amount), "payoutDate": date,
        "transactionCount": 3, "payoutInstrument": {"nickname": "Old National", "accountLastFourDigits": "1234",
                                                    "routingNumber": "should-not-be-stored"},
    }))


def _set_payout(db, payout_id, txn_ids):
    with db.get_conn() as conn:  # test-only: which payout each transaction went out in
        for tid in txn_ids:
            conn.execute("UPDATE ebay_fin_transactions SET payout_id = ? WHERE transaction_id = ?", (payout_id, tid))


@pytest.fixture
def qb(monkeypatch):
    calls = []

    async def fake_request(method, path, *, params=None, json_body=None):
        calls.append({"method": method, "path": path, "json": json_body})
        if path == "deposit":
            return {"Deposit": {"Id": "900", "DocNumber": "DEP-900"}}
        return {"JournalEntry": {"Id": str(70 + len(calls)), "DocNumber": f"JE-{70 + len(calls)}"}}
    monkeypatch.setattr(quickbooks, "_request", fake_request)
    monkeypatch.setattr(quickbooks, "is_connected", lambda: True)
    for key, value in {"QUICKBOOKS_ENABLED": True, "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID": "501",
                       "QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID": "502", "QUICKBOOKS_EBAY_ADS_ACCOUNT_ID": "",
                       "QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID": "24", "QUICKBOOKS_BANK_ACCOUNT_ID": "120",
                       "QUICKBOOKS_REFUNDS_ACCOUNT_ID": "", "QUICKBOOKS_EBAY_SALES_ACCOUNT_ID": "98"}.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(ebay_finances, "refresh_order_transactions", AsyncMock(return_value=0))
    return calls


def _je_lines(call):
    return sorted((l["JournalEntryLineDetail"]["PostingType"], l["JournalEntryLineDetail"]["AccountRef"]["value"],
                   l["Amount"]) for l in call["json"]["Line"])


@pytest.fixture
def order(fresh_db):
    """Pallet cost $10. One item sold on eBay for $25 (Sales Receipt SR-5).
    eBay: sale $25 with $3.25 fees, a $4.10 label, and later a $5 refund."""
    pallet_id = fresh_db.create_pallet("Pallets 1-6", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 10.0, actor_id=1)
    item = _item(fresh_db, pallet_id, status=fresh_db.STATUS_SHIPPED)
    sale = fresh_db.get_sale_for_item(item["id"])
    fresh_db.set_sale_receipt_id(sale["id"], "SR-5")
    _store_order(fresh_db, "O-1", [("L-1", _sku(item), "1100", 25.0)])
    _store_txn(fresh_db, "T-SALE", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25)
    _store_txn(fresh_db, "T-LBL", "SHIPPING_LABEL", 4.10, "DEBIT", order_id="O-1")
    return pallet_id, item, sale


# =================================================================== refunds

def test_refund_breakdown_picks_up_refunds_and_credits_but_not_fees(fresh_db, order):
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    _store_txn(fresh_db, "T-CR", "CREDIT", 1.25, "CREDIT", refs=[("ORDER_ID", "O-1")])
    b = ebay_books.order_refund_breakdown("O-1")
    assert {(c["transaction_id"], c["amount"]) for c in b["charges"]} == {("T-REF", 5.0), ("T-CR", -1.25)}
    assert b["total"] == 3.75 and b["sale_logged"]


def test_confirm_refund_books_journal_entry_and_lowers_pallet_revenue(fresh_db, order, qb):
    pallet_id, item, _ = order
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    before = fresh_db.get_pallet_financials(pallet_id)["net_revenue"]

    result = asyncio.run(ebay_books.confirm_order_refund("O-1", actor_id=7))

    assert result["ok"], result
    assert _je_lines(qb[-1]) == [("Credit", "24", 5.0), ("Debit", "98", 5.0)]  # refund vs eBay Sales
    assert fresh_db.get_pallet_financials(pallet_id)["net_revenue"] == pytest.approx(before - 5.0)
    again = asyncio.run(ebay_books.confirm_order_refund("O-1", actor_id=7))
    assert not again["ok"] and "already logged" in again["message"]
    assert len(qb) == 1


def test_refunds_account_setting_is_used(fresh_db, order, qb, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_REFUNDS_ACCOUNT_ID", "61")
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    asyncio.run(ebay_books.confirm_order_refund("O-1", actor_id=7))
    assert ("Debit", "61", 5.0) in _je_lines(qb[-1])


def test_refund_waits_for_the_sale(fresh_db, qb):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, logged_sale=False)
    _store_order(fresh_db, "O-2", [("L-2", _sku(item), "1200", 10.0)])
    _store_txn(fresh_db, "T-REF2", "REFUND", 2.0, "DEBIT", order_id="O-2")
    result = asyncio.run(ebay_books.confirm_order_refund("O-2", actor_id=1))
    assert not result["ok"] and "sale first" in result["message"] and qb == []
    assert ebay_books.orders_needing_refund_cards("2000-01-01") == []


def test_refund_cards_are_posted_once(fresh_db, order):
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    due = ebay_books.orders_needing_refund_cards("2000-01-01")
    assert [b["order_id"] for b in due] == ["O-1"]
    fresh_db.mark_ebay_book_carded("refund", ebay_books.refund_card_keys(due[0]))
    assert ebay_books.orders_needing_refund_cards("2000-01-01") == []


def test_fee_cards_still_ignore_refunds(fresh_db, order):
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    assert "T-REF" not in {c["transaction_id"] for c in fees.order_fee_breakdown("O-1")["charges"]}


# =========================================================== account charges

def test_account_charges_are_only_the_ones_not_tied_to_an_order(fresh_db, order):
    _store_txn(fresh_db, "T-STORE", "NON_SALE_CHARGE", 21.95, "DEBIT", fee_type="STORE_SUBSCRIPTION_FEE",
               date="2026-03-02T00:00:00.000Z")
    _store_txn(fresh_db, "T-INS", "NON_SALE_CHARGE", 0.35, "DEBIT", fee_type="INSERTION_FEE",
               refs=[("ITEM_ID", "9999")], date="2026-03-03T00:00:00.000Z")  # a listing that never sold
    _store_txn(fresh_db, "T-AD", "NON_SALE_CHARGE", 1.00, "DEBIT", fee_type="AD_FEE",
               refs=[("ITEM_ID", "1100")])  # listing that sold on O-1 -> the order's fee card
    _store_txn(fresh_db, "T-XFER", "TRANSFER", 50.0, "DEBIT")  # money movement, never booked
    charges = ebay_books.account_charges("2026-03")
    assert {c["transaction_id"] for c in charges} == {"T-STORE", "T-INS"}
    assert ebay_books.account_charge_months() == ["2026-03"]
    assert ebay_books.account_charge_months(before_month="2026-03") == []


def test_confirm_account_charges_books_once_and_puts_listing_fees_on_the_pallet(fresh_db, qb):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, logged_sale=False)
    fresh_db.save_ebay_listing_data(item["id"], "Lamp", "123", "1000", 15.0, {})
    fresh_db.set_ebay_item_id(item["id"], "7777")
    _store_txn(fresh_db, "T-STORE", "NON_SALE_CHARGE", 21.95, "DEBIT", fee_type="STORE_SUBSCRIPTION_FEE",
               date="2026-09-01T00:00:00.000Z")
    _store_txn(fresh_db, "T-INS", "NON_SALE_CHARGE", 0.35, "DEBIT", fee_type="INSERTION_FEE",
               refs=[("ITEM_ID", "7777")], date="2026-09-03T00:00:00.000Z")

    result = asyncio.run(ebay_books.confirm_account_charges("2026-09", actor_id=1))

    assert result["ok"], result
    assert _je_lines(qb[-1]) == [("Credit", "24", 22.3), ("Debit", "501", 22.3)]
    assert fresh_db.get_pallet_cost_breakdown(pallet_id).get("ebay_fees") == pytest.approx(0.35)
    assert ebay_books.account_charges("2026-09") == []
    assert not asyncio.run(ebay_books.confirm_account_charges("2026-09", actor_id=1))["ok"]


def test_an_account_charge_booked_first_is_skipped_by_a_later_order_card(fresh_db, order, qb):
    # T-INS references a listing that hasn't sold -> account card books it.
    _store_txn(fresh_db, "T-INS", "NON_SALE_CHARGE", 0.35, "DEBIT", fee_type="INSERTION_FEE",
               refs=[("ITEM_ID", "8888")], date="2026-03-03T00:00:00.000Z")
    assert asyncio.run(ebay_books.confirm_account_charges("2026-03", actor_id=1))["ok"]
    # ... the listing then sells on a new order: its fee card must not book T-INS again.
    _store_order(fresh_db, "O-9", [("L-9", "nope", "8888", 5.0)])
    assert "T-INS" not in {c["transaction_id"] for c in fees.order_fee_breakdown("O-9")["charges"]}


# =================================================================== payouts

def _book_fees(fresh_db, qb):
    assert asyncio.run(fees.confirm_order_fees("O-1", actor_id=1))["ok"]


def test_payout_is_ready_once_everything_in_it_is_booked(fresh_db, order, qb):
    _store_payout(fresh_db, "P-1", 25.0 - 3.25 - 4.10)  # 17.65
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])

    b = ebay_books.payout_breakdown("P-1")
    assert not b["ready"] and any("fees aren't logged" in p for p in b["problems"])

    _book_fees(fresh_db, qb)
    b = ebay_books.payout_breakdown("P-1")
    assert b["ready"], b["problems"]
    assert b["sales"] == [{"sale_id": order[2]["id"], "receipt_id": "SR-5", "amount": 25.0, "order_id": "O-1"}]
    assert b["booked"] == 7.35 and b["expected"] == 17.65


def test_confirm_payout_records_a_linked_bank_deposit(fresh_db, order, qb):
    _store_payout(fresh_db, "P-1", 17.65)
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])
    _book_fees(fresh_db, qb)

    result = asyncio.run(ebay_books.confirm_payout("P-1", actor_id=3))

    assert result["ok"] and result["txn_type"] == "Deposit" and not result["fallback"]
    dep = qb[-1]
    assert dep["path"] == "deposit"
    assert dep["json"]["DepositToAccountRef"] == {"value": "120"}
    assert dep["json"]["TxnDate"] == "2026-03-05"
    lines = dep["json"]["Line"]
    assert lines[0]["LinkedTxn"] == [{"TxnId": "SR-5", "TxnType": "SalesReceipt", "TxnLineId": "0"}]
    assert lines[0]["Amount"] == 25.0
    assert lines[1]["Amount"] == -7.35 and lines[1]["DepositLineDetail"]["AccountRef"] == {"value": "24"}
    assert round(sum(l["Amount"] for l in lines), 2) == 17.65  # the deposit equals the payout
    assert fresh_db.get_ebay_payout_deposit("P-1")["quickbooks_txn_id"] == "900"
    assert not asyncio.run(ebay_books.confirm_payout("P-1", actor_id=3))["ok"]  # never twice


def test_deposit_refused_falls_back_to_a_journal_entry(fresh_db, order, qb, monkeypatch):
    _store_payout(fresh_db, "P-1", 17.65)
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])
    _book_fees(fresh_db, qb)
    real = quickbooks._request

    async def no_deposits(method, path, **kwargs):
        if path == "deposit":
            raise quickbooks.QuickBooksError("Invalid account for deposit line")
        return await real(method, path, **kwargs)
    monkeypatch.setattr(quickbooks, "_request", no_deposits)

    result = asyncio.run(ebay_books.confirm_payout("P-1", actor_id=3))

    assert result["ok"] and result["fallback"] and result["txn_type"] == "JournalEntry"
    assert _je_lines(qb[-1]) == [("Credit", "24", 17.65), ("Debit", "120", 17.65)]


def test_payout_that_does_not_add_up_is_blocked(fresh_db, order, qb):
    _store_payout(fresh_db, "P-1", 20.00)  # eBay says 20.00, the books say 17.65
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])
    _book_fees(fresh_db, qb)
    b = ebay_books.payout_breakdown("P-1")
    assert not b["ready"] and "eBay paid $20.00" in b["problems"][0]
    assert not asyncio.run(ebay_books.confirm_payout("P-1", actor_id=1))["ok"]


def test_payout_with_unlogged_sale_or_receipt_is_blocked(fresh_db, order, qb):
    _, _, sale = order
    with fresh_db.get_conn() as conn:  # test-only: QuickBooks failed when this sale was logged
        conn.execute("UPDATE sales SET quickbooks_sales_receipt_id = NULL WHERE id = ?", (sale["id"],))
    _store_payout(fresh_db, "P-1", 17.65)
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])
    _book_fees(fresh_db, qb)
    b = ebay_books.payout_breakdown("P-1")
    assert any(f"retry-sale sale_id:{sale['id']}" in p for p in b["problems"])


def test_payout_includes_refunds_and_account_charges(fresh_db, order, qb):
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    _store_txn(fresh_db, "T-STORE", "NON_SALE_CHARGE", 2.65, "DEBIT", fee_type="STORE_SUBSCRIPTION_FEE",
               date="2026-03-02T00:00:00.000Z")
    _store_payout(fresh_db, "P-1", 25.0 - 3.25 - 4.10 - 5.00 - 2.65)  # 10.00
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL", "T-REF", "T-STORE"])
    _book_fees(fresh_db, qb)
    problems = ebay_books.payout_breakdown("P-1")["problems"]
    assert any("order `O-1`'s card" in p for p in problems)
    assert any("account charges card" in p for p in problems)

    assert asyncio.run(ebay_books.confirm_order_refund("O-1", actor_id=1))["ok"]
    assert asyncio.run(ebay_books.confirm_account_charges("2026-03", actor_id=1))["ok"]
    b = ebay_books.payout_breakdown("P-1")
    assert b["ready"], b["problems"]
    assert b["booked_by_category"] == {"fees": 5.9, "shipping": 4.1, "refunds": 5.0}


def test_only_succeeded_payouts_get_cards_and_only_once(fresh_db, order):
    _store_payout(fresh_db, "P-1", 17.65)
    _store_payout(fresh_db, "P-2", 3.00, status="INITIATED")
    _store_payout(fresh_db, "P-OLD", 9.00, date="2026-01-01T00:00:00.000Z")
    assert [p["payout_id"] for p in ebay_books.payouts_needing_cards("2026-02-01")] == ["P-1"]
    fresh_db.mark_ebay_book_carded("payout", ["P-1"])
    assert ebay_books.payouts_needing_cards("2026-02-01") == []


def test_payout_bank_details_are_trimmed(fresh_db):
    _store_payout(fresh_db, "P-1", 1.0)
    p = fresh_db.get_ebay_payout("P-1")
    assert p["instrument_nickname"] == "Old National" and p["instrument_last4"] == "1234"
    assert "routing" not in str(p).lower() and "should-not-be-stored" not in str(p)


def test_no_bank_account_no_deposit(fresh_db, order, qb, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_BANK_ACCOUNT_ID", "")
    result = asyncio.run(ebay_books.confirm_payout("P-1", actor_id=1))
    assert not result["ok"] and "QUICKBOOKS_BANK_ACCOUNT_ID" in result["message"]


# ============================================================ pallet profit

def test_pallet_profit_breaks_down_costs_and_selling(fresh_db, order, qb):
    pallet_id, item, _ = order
    unsold = _item(fresh_db, pallet_id, logged_sale=False, status=fresh_db.STATUS_LISTED)
    fresh_db.save_ebay_listing_data(unsold["id"], "Lamp", "123", "1000", 40.0, {})
    _book_fees(fresh_db, qb)
    with fresh_db.get_conn() as conn:  # test-only: sold 10 days after it was logged
        conn.execute("UPDATE items SET created_at = '2026-03-01T00:00:00+00:00', "
                     "sale_recorded_at = '2026-03-11T00:00:00+00:00' WHERE id = ?", (item["id"],))

    r = ebay_books.pallet_profit(pallet_id)

    assert r["purchase_cost"] == 10.0
    assert r["ebay_costs"] == 3.25 and r["shipping"] == 4.10
    assert r["total_cost"] == pytest.approx(17.35)
    assert r["revenue"] == 25.0 and r["profit"] == pytest.approx(7.65)
    assert r["roi_pct"] == pytest.approx(76.5)
    assert (r["items_total"], r["items_sold"], r["items_unsold"]) == (2, 1, 1)
    assert r["sell_through_pct"] == 50.0 and r["avg_days_to_sell"] == pytest.approx(10.0)
    assert r["unsold_list_value"] == 40.0
    # eBay kept 7.35 of 25 (29.4%) so far -> the $40 left nets about $28.24 more.
    assert r["projected_profit"] == pytest.approx(7.65 + 40 * (1 - 7.35 / 25), abs=0.01)


def test_pallet_profit_with_no_cost(fresh_db):
    pallet_id = fresh_db.create_pallet("Empty", category_id=1, created_by=1)
    r = ebay_books.pallet_profit(pallet_id)
    assert r["profit"] is None and r["roi_pct"] is None and r["items_total"] == 0


def test_pallet_profit_embed_and_ranking_render(fresh_db, order, qb):
    from cogs.finance import build_pallet_profit_embed, format_pallet_ranking
    pallet_id = order[0]
    _book_fees(fresh_db, qb)
    embed = build_pallet_profit_embed(ebay_books.pallet_profit(pallet_id))
    fields = {f.name: f.value for f in embed.fields}
    assert "Profit so far: $7.65" in fields["Result"]
    assert "Shipping labels: $4.10" in fields["Money out"]
    empty = fresh_db.create_pallet("Empty", category_id=2, created_by=1)
    text = format_pallet_ranking([ebay_books.pallet_profit(pallet_id), ebay_books.pallet_profit(empty)])
    assert text.index("Pallets 1-6") < text.index("Empty") and "no cost" in text
