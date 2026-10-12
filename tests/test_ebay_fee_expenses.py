"""
eBay fee expenses (ebay_fee_expenses.py + the "💸 eBay fees" cards in
cogs/ebay.py + quickbooks.create_balanced_journal_entry): booking what eBay
keeps from each sale - selling fees, eBay shipping labels, ad fees - as a
QuickBooks Journal Entry (debit expenses, credit Undeposited Funds) and
onto the pallet's cost.

QuickBooks is faked at quickbooks._request (the payload it would send is
checked exactly), eBay at refresh_order_transactions - no network calls.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import ebay_api
import ebay_fee_expenses as fees
import ebay_finances
import quickbooks
import cogs.ebay as ebay_cog

PAST = "2025-01-01T00:00:00+00:00"
ORDER_DATE = "2026-03-01T12:00:00.000Z"


# ----------------------------------------------------------------- helpers

def _amt(v):
    return {"value": f"{v:.2f}", "currency": "USD"}


def _store_order(db, order_id, lines, date=ORDER_DATE):
    """lines: [(line_id, sku, listing_id, price)]"""
    db.upsert_ebay_order_breakdown(ebay_finances.parse_order_breakdown({
        "orderId": order_id, "creationDate": date,
        "pricingSummary": {"priceSubtotal": _amt(sum(l[3] for l in lines)), "total": _amt(sum(l[3] for l in lines))},
        "lineItems": [{"lineItemId": lid, "sku": sku, "legacyItemId": listing, "lineItemCost": _amt(price)}
                      for lid, sku, listing, price in lines],
    }))


def _store_txn(db, txn_id, ttype, amount, booking, order_id=None, fee=None, refs=None, fee_type=None,
               date=ORDER_DATE):
    raw = {"transactionId": txn_id, "transactionType": ttype, "amount": _amt(amount), "bookingEntry": booking,
           "transactionDate": date}
    if order_id:
        raw["orderId"] = order_id
    if fee is not None:
        raw["totalFeeAmount"] = _amt(fee)
    if refs:
        raw["references"] = [{"referenceType": t, "referenceId": i} for t, i in refs]
    if fee_type:
        raw["feeType"] = fee_type
    db.upsert_ebay_fin_transaction(ebay_finances.parse_transaction(raw))


def _item(db, pallet_id, logged_sale=True, status=None):
    item_id = db.create_item(pallet_id, "thing", [], 1)
    with db.get_conn() as conn:  # test-only: created before the eBay order
        conn.execute("UPDATE items SET created_at = ? WHERE id = ?", (PAST, item_id))
    if logged_sale:
        sale_id = db.create_sale("eBay", "2026-03-01", 25.0, False, created_by=1)
        db.add_sale_item(sale_id, item_id, 25.0)
        db.record_item_sale(item_id, 25.0, "eBay", actor_id=1)
    if status:
        db.update_status(item_id, status, actor_id=1)
    return db.get_item(item_id)


def _sku(item):
    return f"pallet-{item['pallet_id']}-item-{item['item_number']}"


@pytest.fixture
def one_order(fresh_db):
    """Pallet cost $10; one item sold for $25 (logged); eBay kept $3.25 in
    fees, $4.10 for a label, and $1.00 promoted-listing ad fee."""
    pallet_id = fresh_db.create_pallet("Pallets 1-6", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 10.0, actor_id=1)
    item = _item(fresh_db, pallet_id, status=fresh_db.STATUS_SHIPPED)
    _store_order(fresh_db, "O-1", [("L-1", _sku(item), "1100", 20.0)])
    _store_txn(fresh_db, "T-SALE", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25)
    _store_txn(fresh_db, "T-LBL", "SHIPPING_LABEL", 4.10, "DEBIT", order_id="O-1")
    _store_txn(fresh_db, "T-AD", "NON_SALE_CHARGE", 1.00, "DEBIT", refs=[("ITEM_ID", "1100")], fee_type="AD_FEE")
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")  # not a fee - excluded
    return pallet_id, item


@pytest.fixture
def qb(monkeypatch):
    """QuickBooks connected with an eBay fees account configured; records
    every request, returns a Journal Entry."""
    calls = []

    async def fake_request(method, path, *, params=None, json_body=None):
        calls.append({"method": method, "path": path, "json": json_body})
        return {"JournalEntry": {"Id": "77", "DocNumber": "JE-77"}}
    monkeypatch.setattr(quickbooks, "_request", fake_request)
    monkeypatch.setattr(quickbooks, "is_connected", lambda: True)
    monkeypatch.setattr(config, "QUICKBOOKS_ENABLED", True)
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID", "501")
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID", "502")
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_ADS_ACCOUNT_ID", "")
    monkeypatch.setattr(config, "QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID", "24")
    monkeypatch.setattr(fees.ebay_finances, "refresh_order_transactions", AsyncMock(return_value=0))
    return calls


def _lines(je_call):
    return [(l["JournalEntryLineDetail"]["PostingType"], l["JournalEntryLineDetail"]["AccountRef"]["value"], l["Amount"])
            for l in je_call["json"]["Line"]]


# ---------------------------------------------------- QuickBooks Journal Entry

def test_balanced_journal_entry_debits_expenses_and_credits_the_offset(qb):
    asyncio.run(quickbooks.create_balanced_journal_entry(
        [{"account_id": "501", "amount": 3.25, "description": "fees"},
         {"account_id": "502", "amount": 4.10, "description": "label"},
         {"account_id": "503", "amount": 0, "description": "dropped"}],
        "24", "2026-03-01", "memo",
    ))
    assert qb[0]["path"] == "journalentry"
    assert _lines(qb[0]) == [("Debit", "501", 3.25), ("Debit", "502", 4.10), ("Credit", "24", 7.35)]


def test_balanced_journal_entry_turns_negative_lines_into_credits(qb):
    # A voided label credited back (-4.10) on the same entry as $3.25 fees.
    asyncio.run(quickbooks.create_balanced_journal_entry(
        [{"account_id": "501", "amount": 3.25}, {"account_id": "502", "amount": -4.10}], "24", "2026-03-01", "m",
    ))
    assert _lines(qb[0]) == [("Debit", "501", 3.25), ("Credit", "502", 4.10), ("Debit", "24", 0.85)]


def test_balanced_journal_entry_refuses_an_empty_entry(qb):
    with pytest.raises(quickbooks.QuickBooksError):
        asyncio.run(quickbooks.create_balanced_journal_entry([{"account_id": "501", "amount": 0}], "24", "d", "m"))
    assert qb == []


# ------------------------------------------------------------- what counts

def test_breakdown_collects_sale_fees_labels_and_listing_ad_fees_but_not_refunds(fresh_db, one_order):
    breakdown = fees.order_fee_breakdown("O-1")

    assert breakdown["totals"] == {"fees": 3.25, "shipping": 4.10, "ads": 1.00, "total": 8.35}
    assert {c["transaction_id"] for c in breakdown["charges"]} == {"T-SALE", "T-LBL", "T-AD"}
    assert breakdown["sale_logged"] is True


def test_voided_label_credit_and_insertion_fees(fresh_db, one_order):
    _store_txn(fresh_db, "T-VOID", "SHIPPING_LABEL", 4.10, "CREDIT", order_id="O-1")
    _store_txn(fresh_db, "T-INS", "NON_SALE_CHARGE", 0.35, "DEBIT", refs=[("ITEM_ID", "1100")],
               fee_type="INSERTION_FEE")
    totals = fees.order_fee_breakdown("O-1")["totals"]
    assert totals["shipping"] == 0.0 and totals["fees"] == 3.60 and totals["ads"] == 1.00


def test_sale_not_logged_yet_is_flagged(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, logged_sale=False)
    _store_order(fresh_db, "O-2", [("L-2", _sku(item), None, 10.0)])
    _store_txn(fresh_db, "T-2", "SALE", 8.0, "CREDIT", order_id="O-2", fee=2.0)
    assert fees.order_fee_breakdown("O-2")["sale_logged"] is False


# --------------------------------------------------------------- splitting

def test_allocation_splits_order_charges_by_price_and_keeps_others_off_our_pallets(fresh_db):
    p1 = fresh_db.create_pallet("P1", category_id=1, created_by=1)
    p2 = fresh_db.create_pallet("P2", category_id=2, created_by=1)
    a, b = _item(fresh_db, p1), _item(fresh_db, p2)
    _store_order(fresh_db, "O-3", [("L-a", _sku(a), None, 30.0), ("L-b", _sku(b), None, 10.0),
                                   ("L-x", "not-ours", None, 10.0)])
    _store_txn(fresh_db, "T-3", "SALE", 40.0, "CREDIT", order_id="O-3", fee=5.00)

    rows = fees.allocate_to_items(fees.order_fee_breakdown("O-3"))

    by_item = {r["item_id"]: r for r in rows}
    assert by_item[a["id"]]["amount"] == 3.00 and by_item[a["id"]]["pallet_id"] == p1
    assert by_item[b["id"]]["amount"] == 1.00 and by_item[b["id"]]["pallet_id"] == p2
    assert all(r["cost_type"] == "ebay_fees" for r in rows)
    assert len(rows) == 2  # the non-tracked line's $1.00 goes on no pallet


def test_allocation_rounding_always_sums_exactly(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    items = [_item(fresh_db, pallet_id) for _ in range(3)]
    _store_order(fresh_db, "O-4", [(f"L-{i}", _sku(it), None, 10.0) for i, it in enumerate(items)])
    _store_txn(fresh_db, "T-4", "SALE", 28.0, "CREDIT", order_id="O-4", fee=1.00)

    rows = fees.allocate_to_items(fees.order_fee_breakdown("O-4"))
    assert round(sum(r["amount"] for r in rows), 2) == 1.00


# -------------------------------------------------------------- confirming

def test_confirm_books_one_journal_entry_and_the_pallet_cost(fresh_db, one_order, qb):
    pallet_id, item = one_order
    profit_before = fresh_db.get_pallet_financials(pallet_id)["profit_so_far"]

    result = asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))

    assert result["ok"] and result["je_doc"] == "JE-77" and result["pallet_ids"] == [pallet_id]
    assert len(qb) == 1
    # fees + ads -> fees account 501 (no separate ads account), label -> 502, credit Undeposited Funds 24
    assert _lines(qb[0]) == [("Debit", "501", 3.25), ("Debit", "502", 4.10), ("Debit", "501", 1.00),
                             ("Credit", "24", 8.35)]
    assert qb[0]["json"]["TxnDate"] == "2026-03-01" and "O-1" in qb[0]["json"]["PrivateNote"]
    assert fresh_db.get_pallet_cost_breakdown(pallet_id) == {"ebay_fees": 3.25, "shipping": 4.10, "ebay_ads": 1.00}
    assert fresh_db.get_pallet_financials(pallet_id)["profit_so_far"] == pytest.approx(profit_before - 8.35)
    assert fresh_db.get_item(item["id"])["sale_price"] == 25.0  # the sale itself is untouched


def test_the_same_charges_can_never_be_booked_twice(fresh_db, one_order, qb):
    asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    again = asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    assert again["ok"] is False and "already logged" in again["message"]
    assert len(qb) == 1


def test_a_late_charge_books_on_its_own(fresh_db, one_order, qb):
    pallet_id, _ = one_order
    asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    _store_txn(fresh_db, "T-LBL2", "SHIPPING_LABEL", 2.00, "DEBIT", order_id="O-1")

    asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))

    assert _lines(qb[1]) == [("Debit", "502", 2.00), ("Credit", "24", 2.00)]
    assert fresh_db.get_pallet_cost_breakdown(pallet_id)["shipping"] == 6.10


def test_nothing_is_posted_without_a_fees_account(fresh_db, one_order, qb, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID", "")
    result = asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    assert result["ok"] is False and "QUICKBOOKS_EBAY_FEES_ACCOUNT_ID" in result["message"]
    assert qb == [] and fresh_db.get_posted_ebay_fee_keys() == set()


def test_nothing_is_posted_without_quickbooks(fresh_db, one_order, qb, monkeypatch):
    monkeypatch.setattr(quickbooks, "is_connected", lambda: False)
    assert asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))["ok"] is False
    assert qb == []


def test_refused_until_the_sale_is_logged(fresh_db, qb):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, logged_sale=False)
    _store_order(fresh_db, "O-5", [("L-5", _sku(item), None, 10.0)])
    _store_txn(fresh_db, "T-5", "SALE", 8.0, "CREDIT", order_id="O-5", fee=2.0)

    result = asyncio.run(fees.confirm_order_fees("O-5", actor_id=9))
    assert result["ok"] is False and "Confirm & log sale" in result["message"] and qb == []


def test_quickbooks_failure_records_nothing(fresh_db, one_order, qb, monkeypatch):
    pallet_id, _ = one_order

    async def failing(method, path, **kwargs):
        raise quickbooks.QuickBooksError("QuickBooks API request failed (400): bad account")
    monkeypatch.setattr(quickbooks, "_request", failing)

    result = asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    assert result["ok"] is False and "nothing was recorded" in result["message"]
    assert fresh_db.get_posted_ebay_fee_keys() == set()
    assert fresh_db.get_pallet_cost_breakdown(pallet_id) == {}


def test_a_failed_ledger_save_after_quickbooks_warns_not_to_click_again(fresh_db, one_order, qb, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")
    monkeypatch.setattr(fees.db, "record_ebay_fee_posting", broken)
    result = asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    assert result["ok"] is False and "was created" in result["message"] and "Don't click again" in result["message"]


def test_ebay_refresh_failure_still_books_from_the_last_sync(fresh_db, one_order, qb, monkeypatch):
    monkeypatch.setattr(fees.ebay_finances, "refresh_order_transactions",
                        AsyncMock(side_effect=ebay_api.EbayAPIError("403")))
    assert asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))["ok"] is True


def test_net_report_does_not_count_booked_fees_twice(fresh_db, one_order, qb):
    pallet_id, _ = one_order
    before = ebay_finances.build_pallet_report(pallet_id)["profit_with_ebay_net"]
    asyncio.run(fees.confirm_order_fees("O-1", actor_id=9))
    assert ebay_finances.build_pallet_report(pallet_id)["profit_with_ebay_net"] == before


# ------------------------------------------------------------------- cards

class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


def test_card_waits_for_shipping_or_the_delay(fresh_db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _item(fresh_db, pallet_id, status=fresh_db.STATUS_SOLD)
    _store_order(fresh_db, "O-6", [("L-6", _sku(item), None, 10.0)])
    _store_txn(fresh_db, "T-6", "SALE", 8.0, "CREDIT", order_id="O-6", fee=2.0)
    order_dt = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    breakdown = fees.order_fee_breakdown("O-6")

    assert fees.is_card_due(breakdown, order_dt + timedelta(days=1)) is False
    assert fees.is_card_due(breakdown, order_dt + timedelta(days=config.EBAY_FEE_CARD_DELAY_DAYS)) is True
    fresh_db.update_status(item["id"], fresh_db.STATUS_SHIPPED, actor_id=1)
    assert fees.is_card_due(fees.order_fee_breakdown("O-6"), order_dt + timedelta(days=1)) is True


def test_cards_post_once_then_again_only_for_new_charges(fresh_db, one_order):
    fresh_db.set_shared_channel("ebay-orders", 9)
    channel = _FakeChannel()
    bot = _FakeBot(channel)

    assert asyncio.run(ebay_cog._run_fee_cards(bot, since_date="2026-01-01", sync=False)) == 1
    assert asyncio.run(ebay_cog._run_fee_cards(bot, since_date="2026-01-01", sync=False)) == 0
    _store_txn(fresh_db, "T-LBL2", "SHIPPING_LABEL", 2.00, "DEBIT", order_id="O-1")
    assert asyncio.run(ebay_cog._run_fee_cards(bot, since_date="2026-01-01", sync=False)) == 1

    embed = channel.sent[0]["embed"]
    assert embed.title == "💸 eBay fees to log" and "O-1" in embed.description
    assert "$8.35" in embed.fields[1].value
    button = channel.sent[0]["view"].children[0]
    assert button.item.custom_id == "ebay_fees_confirm:O-1"


def test_orders_before_the_start_date_get_no_automatic_card(fresh_db, one_order):
    fresh_db.set_shared_channel("ebay-orders", 9)
    assert asyncio.run(ebay_cog._run_fee_cards(_FakeBot(_FakeChannel()), since_date="2026-06-01", sync=False)) == 0


def test_start_date_is_fixed_the_first_time_it_is_asked(fresh_db):
    assert fresh_db.get_ebay_fee_card_start_date("2026-10-12") == "2026-10-12"
    assert fresh_db.get_ebay_fee_card_start_date("2027-01-01") == "2026-10-12"


def test_confirm_button_requires_finance_role(fresh_db, one_order, monkeypatch):
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: object())
    event = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), user=SimpleNamespace(id=1, roles=[]), guild=object(),
    )
    button = ebay_cog.EbayFeeConfirmButton.__new__(ebay_cog.EbayFeeConfirmButton)
    button.order_id = "O-1"
    asyncio.run(ebay_cog.EbayFeeConfirmButton.callback(button, event))
    assert "Finance Management" in event.response.send_message.call_args.args[0]


def test_confirm_button_success_updates_card_pallet_and_audit_log(fresh_db, one_order, qb, monkeypatch):
    pallet_id, _ = one_order
    role = object()
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: role)
    refresh = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(ebay_cog.finance_utils, "refresh_finance_message", refresh)
    monkeypatch.setattr(ebay_cog.finance_utils, "post_to_finance_audit_log", audit)
    event = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), user=SimpleNamespace(id=1, roles=[role]), guild=object(),
        message=SimpleNamespace(edit=AsyncMock()), client=object(),
    )
    button = ebay_cog.EbayFeeConfirmButton.__new__(ebay_cog.EbayFeeConfirmButton)
    button.order_id = "O-1"

    asyncio.run(ebay_cog.EbayFeeConfirmButton.callback(button, event))

    event.message.edit.assert_awaited_once_with(view=None)
    refresh.assert_awaited_once_with(event.client, pallet_id)
    assert "JE-77" in audit.call_args.args[1] and "$8.35" in audit.call_args.args[1]
    assert "Logged $8.35" in event.followup.send.call_args.args[0]
