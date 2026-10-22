"""eBay money cards book themselves when their numbers check out
(EBAY_AUTO_BOOK); anything else keeps its Confirm button, with the reason.
Each card is tried automatically only once."""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import cogs.ebay as ebay_cog
import ebay_books
from tests.test_ebay_books import _store_payout, order, qb  # noqa: F401 (fixtures)
from tests.test_ebay_fee_expenses import _store_txn
from tests.test_refund_and_book_cards import _bot, channel  # noqa: F401 (fixture)


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, view=None):
        self.sent.append((embed, view))


@pytest.fixture(autouse=True)
def auto(monkeypatch):
    monkeypatch.setattr(config, "EBAY_AUTO_BOOK", True)
    monkeypatch.setattr(config, "EBAY_AUTO_BOOK_MAX", 500.0)
    monkeypatch.setattr(ebay_cog.finance_utils, "refresh_finance_message", AsyncMock())
    monkeypatch.setattr(ebay_cog.finance_utils, "post_to_finance_audit_log", AsyncMock())


def _footer(embed):
    return embed.footer.text or ""


def test_refund_and_account_cards_book_themselves_once(fresh_db, order, qb, channel, monkeypatch):
    monkeypatch.setattr(config, "EBAY_PAYOUT_CARDS", False)
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    _store_txn(fresh_db, "T-STORE", "NON_SALE_CHARGE", 21.95, "DEBIT", fee_type="STORE_SUBSCRIPTION_FEE",
               date="2026-03-02T00:00:00.000Z")

    posted = asyncio.run(ebay_cog._run_book_cards(_bot(channel), since_date="2026-01-01"))

    assert posted == {"refund": 1, "account": 1, "payout": 0}
    for embed, view in channel.sent:
        assert view is None and _footer(embed).startswith("✅ Booked automatically")
    assert ebay_books.order_refund_breakdown("O-1")["charges"] == []  # booked
    again = asyncio.run(ebay_cog._run_book_cards(_bot(channel), since_date="2026-01-01"))
    assert again == {"refund": 0, "account": 0, "payout": 0}


def test_over_the_limit_waits_for_a_person(fresh_db, order, qb, channel, monkeypatch):
    monkeypatch.setattr(config, "EBAY_PAYOUT_CARDS", False)
    monkeypatch.setattr(config, "EBAY_AUTO_BOOK_MAX", 1.0)
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    asyncio.run(ebay_cog._run_book_cards(_bot(channel), since_date="2026-01-01"))
    embed, view = channel.sent[0]
    assert view is not None and "automatic limit" in {f.name: f.value for f in embed.fields}["Needs a person"]


def test_a_refused_auto_book_keeps_the_button_and_is_not_retried(fresh_db, monkeypatch):
    confirm = AsyncMock(return_value={"ok": False, "message": "Log this order's sale first"})
    ch = _Channel()
    monkeypatch.setattr(fresh_db, "get_shared_channel_id", lambda name: 9)
    bot = SimpleNamespace(get_channel=lambda cid: ch)
    breakdown = {"order_id": "O-9", "total": 4.0, "items": [], "charges": [{"label": "Refund", "amount": 4.0}]}
    monkeypatch.setattr(ebay_books, "confirm_order_refund", confirm)
    monkeypatch.setattr(ebay_books, "refund_card_keys", lambda b: ["O-9:T"])
    asyncio.run(ebay_cog.post_refund_card(bot, breakdown))
    asyncio.run(ebay_cog.post_refund_card(bot, breakdown))
    confirm.assert_awaited_once()  # tried once only
    (first, view1), (second, view2) = ch.sent
    assert view1 is not None and "sale first" in {f.name: f.value for f in first.fields}["Needs a person"]
    assert view2 is not None and "already tried" in {f.name: f.value for f in second.fields}["Needs a person"]


# ------------------------------------------------------------------- orders

def _order(**overrides):
    order = {"order_id": "27-1", "sale_date": "2026-10-20", "total_price": 22.0,
             "line_items": [{"sku": "pallet-1-item-96", "quantity": 1}], "payment_status": "PAID",
             "cancel_state": "NONE_REQUESTED"}
    order.update(overrides)
    return order


@pytest.fixture
def orders(fresh_db, monkeypatch):
    ch = _Channel()
    monkeypatch.setattr(fresh_db, "get_shared_channel_id", lambda name: 9)
    pallet_id = fresh_db.create_pallet("Pallets 1-6", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "lamp", [], 1))
    logged = AsyncMock(return_value={"ok": True, "message": "✅ Logged sale #7", "sale_id": 7})
    monkeypatch.setattr(ebay_cog, "log_ebay_order_sale", logged)
    return SimpleNamespace(ch=ch, bot=SimpleNamespace(get_channel=lambda cid: ch), item=item, logged=logged)


def test_paid_matched_order_is_logged_automatically(orders):
    asyncio.run(ebay_cog._handle_new_order(orders.bot, _order(), [orders.item]))
    orders.logged.assert_awaited_once()
    embed, view = orders.ch.sent[0]
    assert view is None and "Logged sale #7" in _footer(embed)


@pytest.mark.parametrize("overrides,why", [
    ({"payment_status": "PENDING"}, "isn't paid"),
    ({"cancel_state": "CANCEL_REQUESTED"}, "cancellation"),
    ({"total_price": 900.0}, "automatic limit"),
    ({"line_items": [{"sku": "pallet-1-item-96", "quantity": 1}, {"sku": "x", "quantity": 1}]}, "every line"),
])
def test_orders_that_dont_check_out_keep_the_button(orders, overrides, why):
    asyncio.run(ebay_cog._handle_new_order(orders.bot, _order(**overrides), [orders.item]))
    orders.logged.assert_not_awaited()
    embed, view = orders.ch.sent[0]
    assert view is not None and why in {f.name: f.value for f in embed.fields}["Needs a person"]


def test_auto_off_means_plain_cards(orders, monkeypatch):
    monkeypatch.setattr(config, "EBAY_AUTO_BOOK", False)
    asyncio.run(ebay_cog._handle_new_order(orders.bot, _order(), [orders.item]))
    embed, view = orders.ch.sent[0]
    assert view is not None and "Needs a person" not in {f.name for f in embed.fields}


# --------------------------------------------------------------------- fees

def test_fee_card_books_itself_when_the_confirm_accepts(fresh_db, monkeypatch):
    ch = _Channel()
    monkeypatch.setattr(fresh_db, "get_shared_channel_id", lambda name: 9)
    bot = SimpleNamespace(get_channel=lambda cid: ch)
    breakdown = {"order_id": "27-1", "items": [], "charges": [],
                 "totals": {"fees": 2.0, "shipping": 0.0, "ads": 0.0, "total": 2.0}}
    monkeypatch.setattr(ebay_cog.ebay_fee_expenses, "confirm_order_fees", AsyncMock(return_value={
        "ok": True, "breakdown": breakdown, "je_doc": "JE-5", "pallet_ids": []}))
    assert asyncio.run(ebay_cog._post_fee_card(bot, breakdown))
    embed, view = ch.sent[0]
    assert view is None and "Journal Entry JE-5" in _footer(embed)


def test_ready_payout_records_its_deposit_by_itself(fresh_db, monkeypatch):
    ch = _Channel()
    monkeypatch.setattr(fresh_db, "get_shared_channel_id", lambda name: 9)
    bot = SimpleNamespace(get_channel=lambda cid: ch)
    b = {"payout_id": "P-7", "amount": 40.0, "deposit": None, "ready": True, "payout": {}, "sales": [],
         "sales_total": 44.0, "booked_by_category": {}, "expected": 40.0, "problems": []}
    done = dict(b, deposit={"txn_id": "D-1"})
    confirm = AsyncMock(return_value={"ok": True, "breakdown": b, "txn_type": "Deposit", "doc": "D-1",
                                      "fallback": False})
    monkeypatch.setattr(ebay_books, "confirm_payout", confirm)
    monkeypatch.setattr(ebay_books, "payout_breakdown", lambda pid: done)
    assert asyncio.run(ebay_cog.post_payout_card(bot, b))
    embed, view = ch.sent[0]
    assert view is None and "Bank Deposit D-1" in _footer(embed)
    not_ready = dict(b, payout_id="P-8", ready=False, problems=["fees aren't logged"])
    asyncio.run(ebay_cog.post_payout_card(bot, not_ready))
    confirm.assert_awaited_once()  # a payout that isn't ready isn't attempted
    assert ch.sent[1][1] is not None
