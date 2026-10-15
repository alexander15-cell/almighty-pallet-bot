"""
/finance refund now posts a QuickBooks Journal Entry (and refuses eBay
items, whose refunds come from the eBay refund card), plus the new
#ebay-orders cards render and post once.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import cogs.ebay as ebay_cog
import cogs.finance as finance_module
import ebay_books
import quickbooks
from cogs.finance import Finance
from tests.test_ebay_books import _set_payout, _store_payout, order, qb  # noqa: F401 (fixtures)
from tests.test_ebay_fee_expenses import _store_txn


@pytest.fixture(autouse=True)
def roles(monkeypatch):
    role = object()
    monkeypatch.setattr(finance_module.runtime_settings, "resolve_role", lambda guild, name: role)
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: role)
    return role


def _interaction(role, category_id=42):
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(category_id=category_id),
        user=SimpleNamespace(id=42, roles=[role]),
        guild=object(),
    )


@pytest.fixture
def cog(monkeypatch):
    c = Finance.__new__(Finance)
    c.bot = SimpleNamespace()
    monkeypatch.setattr(finance_module.finance_utils, "refresh_finance_message", AsyncMock())
    monkeypatch.setattr(finance_module.finance_utils, "post_to_finance_audit_log", AsyncMock())
    return c


def _in_person_item(db, status=None):
    pallet_id = db.create_pallet("Yard", category_id=42, created_by=1)
    item_id = db.create_item(pallet_id, "Lamp", [], 1)
    db.record_item_sale(item_id, 30.0, "In Person", actor_id=1)
    return pallet_id, db.get_item(item_id)


def test_refund_posts_journal_entry_from_the_bank(fresh_db, cog, roles, qb):
    pallet_id, item = _in_person_item(fresh_db)
    event = _interaction(roles)
    asyncio.run(Finance.refund.callback(cog, event, item["item_number"], 10.0, "broken", None))

    lines = sorted((l["JournalEntryLineDetail"]["PostingType"], l["JournalEntryLineDetail"]["AccountRef"]["value"],
                    l["Amount"]) for l in qb[-1]["json"]["Line"])
    assert lines == [("Credit", "120", 10.0), ("Debit", config.QUICKBOOKS_SALES_INCOME_ACCOUNT_ID, 10.0)]
    assert fresh_db.get_pallet_financials(pallet_id)["refunds_total"] == 10.0
    assert "Journal Entry" in event.followup.send.await_args.args[0]


def test_refund_from_undeposited_cash(fresh_db, cog, roles, qb):
    _, item = _in_person_item(fresh_db)
    choice = SimpleNamespace(value="undeposited")
    asyncio.run(Finance.refund.callback(cog, _interaction(roles), item["item_number"], 4.0, "haggle", choice))
    assert any(l["JournalEntryLineDetail"]["AccountRef"]["value"] == "24" for l in qb[-1]["json"]["Line"])


def test_refund_not_sent_to_quickbooks_when_asked(fresh_db, cog, roles, qb):
    pallet_id, item = _in_person_item(fresh_db)
    choice = SimpleNamespace(value="none")
    asyncio.run(Finance.refund.callback(cog, _interaction(roles), item["item_number"], 4.0, "x", choice))
    assert qb == [] and fresh_db.get_pallet_financials(pallet_id)["refunds_total"] == 4.0


def test_quickbooks_failure_records_nothing(fresh_db, cog, roles, qb, monkeypatch):
    pallet_id, item = _in_person_item(fresh_db)

    async def failing(*a, **k):
        raise quickbooks.QuickBooksError("down")
    monkeypatch.setattr(quickbooks, "_request", failing)
    event = _interaction(roles)
    asyncio.run(Finance.refund.callback(cog, event, item["item_number"], 4.0, "x", None))
    assert fresh_db.get_pallet_financials(pallet_id)["refunds_total"] == 0
    assert "nothing was recorded" in event.followup.send.await_args.args[0]


def test_ebay_item_refund_points_to_the_refund_card(fresh_db, cog, roles, qb, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    pallet_id = fresh_db.create_pallet("E", category_id=42, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Drill", [], 1)
    fresh_db.record_item_sale(item_id, 30.0, "eBay", actor_id=1)
    event = _interaction(roles)
    asyncio.run(Finance.refund.callback(cog, event, fresh_db.get_item(item_id)["item_number"], 5.0, "x", None))
    assert "eBay refund" in event.response.send_message.await_args.args[0]
    assert qb == [] and fresh_db.get_pallet_financials(pallet_id)["refunds_total"] == 0


# ------------------------------------------------------------- the cards

class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, view=None):
        self.sent.append((embed, view))


@pytest.fixture
def channel(monkeypatch, fresh_db):
    ch = _Channel()
    monkeypatch.setattr(fresh_db, "get_shared_channel_id", lambda name: 9 if name == "ebay-orders" else None)
    return ch


def _bot(ch):
    return SimpleNamespace(get_channel=lambda cid: ch)


def test_book_cards_post_refund_account_and_payout_cards_once(fresh_db, order, qb, channel, monkeypatch):
    monkeypatch.setattr(config, "EBAY_PAYOUT_CARDS", True)
    _store_txn(fresh_db, "T-REF", "REFUND", 5.00, "DEBIT", order_id="O-1")
    _store_txn(fresh_db, "T-STORE", "NON_SALE_CHARGE", 21.95, "DEBIT", fee_type="STORE_SUBSCRIPTION_FEE",
               date="2026-03-02T00:00:00.000Z")
    _store_payout(fresh_db, "P-1", 1.0)

    posted = asyncio.run(ebay_cog._run_book_cards(_bot(channel), since_date="2026-01-01"))
    assert posted == {"refund": 1, "account": 1, "payout": 1}
    titles = [e.title for e, _ in channel.sent]
    assert titles[0] == "↩️ eBay refund to log"
    assert titles[1] == "🧾 eBay account charges - March 2026"
    assert titles[2].startswith("🏦 eBay payout")
    assert all(v is not None for _, v in channel.sent)

    again = asyncio.run(ebay_cog._run_book_cards(_bot(channel), since_date="2026-01-01"))
    assert again == {"refund": 0, "account": 0, "payout": 0}


def test_payout_card_lists_what_its_waiting_on(fresh_db, order):
    _store_payout(fresh_db, "P-1", 17.65)
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])
    embed = ebay_cog.build_payout_card_embed(ebay_books.payout_breakdown("P-1"))
    fields = {f.name: f.value for f in embed.fields}
    assert "fees aren't logged" in fields["Waiting on"]
    assert "Old National (…1234)" in embed.description


def test_payout_button_refuses_until_ready(fresh_db, order, qb, roles):
    _store_payout(fresh_db, "P-1", 17.65)
    _set_payout(fresh_db, "P-1", ["T-SALE", "T-LBL"])
    event = _interaction(roles)
    asyncio.run(ebay_cog.EbayPayoutConfirmButton("P-1").callback(event))
    assert "Not ready" in event.followup.send.await_args.args[0]
    assert fresh_db.get_ebay_payout_deposit("P-1") is None
