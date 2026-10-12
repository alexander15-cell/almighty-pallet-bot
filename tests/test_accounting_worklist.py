"""
The #accounting worklist: ItemFlow._post_to_accounting_channel posts how to
log each sold item for its platform (no "COGS" - the business is on cash
basis, a sale is only ever a Sales Receipt), remembers the post, and
finance_utils.update_accounting_entries edits it to "✅ logged" (or "⚠️"
with the retry command) when the sale is booked via /finance log-sale,
/finance retry-sale, or the #ebay-orders Confirm & log sale button.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import finance_utils
import quickbooks as qb
import cogs.ebay as ebay_module
import cogs.finance as finance_module
from cogs.finance import Finance, LogSaleConfirmView
from cogs.item_flow import ItemFlow

ACCOUNTING, ORDERS = 700, 701


class _FakeMessage:
    def __init__(self, msg_id, content):
        self.id = msg_id
        self.content = content
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.content = kwargs.get("content", self.content)


class _FakeChannel:
    def __init__(self):
        self.messages = {}
        self._next = 9000

    async def send(self, content=None, **kwargs):
        self._next += 1
        msg = _FakeMessage(self._next, content)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise OSError("not found")  # stands in for discord.NotFound (same error tuple)
        return self.messages[message_id]


class _FakeBot:
    def __init__(self):
        self.accounting = _FakeChannel()

    def get_channel(self, channel_id):
        return self.accounting if channel_id == ACCOUNTING else None


_FINANCE_ROLE = SimpleNamespace(name=config.ROLE_FINANCE_MGMT)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(finance_module.runtime_settings, "resolve_role",
                        lambda guild, name: _FINANCE_ROLE if name == config.ROLE_FINANCE_MGMT else None)
    monkeypatch.setattr(ebay_module.runtime_settings, "resolve_role",
                        lambda guild, name: _FINANCE_ROLE if name == config.ROLE_FINANCE_MGMT else None)
    monkeypatch.setattr(finance_utils, "refresh_finance_message", AsyncMock())
    monkeypatch.setattr(finance_utils, "post_to_finance_audit_log", AsyncMock())


@pytest.fixture
def setup(fresh_db):
    fresh_db.set_shared_channel("accounting", ACCOUNTING)
    fresh_db.set_shared_channel("ebay-orders", ORDERS)
    bot = _FakeBot()
    pallet_id = fresh_db.create_pallet("Pallets 1 -6", category_id=1, created_by=1)
    return bot, pallet_id


def _sold_item(db, bot, pallet_id, platform="eBay", title="Hampton Bay String Lights"):
    item_id = db.create_item(pallet_id, "lights", [], 1)
    db.save_ai_review(item_id, title=title, description="d", flags="")
    asyncio.run(ItemFlow(bot)._post_to_accounting_channel(item_id, platform))
    return db.get_item(item_id)


def _post_for(db, bot, item):
    entry = db.get_accounting_entry(item["id"])
    return bot.accounting.messages[entry["message_id"]]


def _interaction(client):
    return SimpleNamespace(
        guild=object(), user=SimpleNamespace(id=1, roles=[_FINANCE_ROLE]),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), message=SimpleNamespace(edit=AsyncMock()), client=client,
    )


# ------------------------------------------------------------------ wording

def test_ebay_post_points_at_the_ebay_orders_card_not_cogs(fresh_db, setup):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id, "eBay")
    text = _post_for(fresh_db, bot, item).content
    assert text == (f"💵 **Pallets 1 -6** item #{item['item_number']} sold via **eBay** - \"Hampton Bay String "
                    f"Lights\" - log the sale with **Confirm & log sale** on its <#{ORDERS}> card.")
    assert "COGS" not in text


def test_other_platform_post_points_at_log_sale(fresh_db, setup):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id, "Facebook Marketplace")
    text = _post_for(fresh_db, bot, item).content
    assert text.endswith("log the sale price with `/finance log-sale`.") and "COGS" not in text


def test_logged_and_failed_wording(fresh_db, setup):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    assert finance_utils.accounting_entry_text(item, "eBay", sale_id=12, receipt_doc="1001").startswith("✅ ")
    assert "logged as sale #12 (Sales Receipt `1001`)" in finance_utils.accounting_entry_text(
        item, "eBay", sale_id=12, receipt_doc="1001")
    failed = finance_utils.accounting_entry_text(item, "eBay", sale_id=12, error="QB down")
    assert failed.startswith("⚠️ ") and "/finance retry-sale sale_id:12" in failed


def test_no_user_facing_message_mentions_cogs_anymore():
    source = (Path(__file__).resolve().parent.parent / "cogs" / "item_flow.py").read_text(encoding="utf-8")
    assert "needs COGS" not in source and "log COGS" not in source


# ------------------------------------------------- remembering + ticking off

def test_post_is_remembered_and_a_resale_replaces_it(fresh_db, setup):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    first = fresh_db.get_accounting_entry(item["id"])["message_id"]
    asyncio.run(ItemFlow(bot)._post_to_accounting_channel(item["id"], "eBay"))
    assert fresh_db.get_accounting_entry(item["id"])["message_id"] != first


def test_log_sale_ticks_off_every_item_in_a_bundle(fresh_db, setup, monkeypatch):
    bot, pallet_id = setup
    a = _sold_item(fresh_db, bot, pallet_id, title="Track Light")
    b = _sold_item(fresh_db, bot, pallet_id, title="Timer")
    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"}))

    view = LogSaleConfirmView([a, b], "eBay", 30.0, False, "2026-10-12")
    asyncio.run(view.confirm.callback(_interaction(bot)))

    sale_id = fresh_db.get_item_sale(a["id"])["sale_id"]
    for item, title in ((a, "Track Light"), (b, "Timer")):
        post = _post_for(fresh_db, bot, item)
        assert post.content == (f"✅ **Pallets 1 -6** item #{item['item_number']} sold via **eBay** - \"{title}\" "
                                f"- logged as sale #{sale_id} (Sales Receipt `1001`).")
        assert fresh_db.get_accounting_entry(item["id"])["logged_at"] is not None


def test_quickbooks_failure_shows_the_retry_then_retry_ticks_it_off(fresh_db, setup, monkeypatch):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(side_effect=qb.QuickBooksError("down")))
    asyncio.run(LogSaleConfirmView([item], "eBay", 16.76, False, "2026-10-12").confirm.callback(_interaction(bot)))

    sale_id = fresh_db.get_item_sale(item["id"])["sale_id"]
    post = _post_for(fresh_db, bot, item)
    assert post.content.startswith("⚠️ ") and f"retry-sale sale_id:{sale_id}" in post.content
    assert fresh_db.get_accounting_entry(item["id"])["logged_at"] is None

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-2", "doc_number": "1002"}))
    asyncio.run(Finance.retry_sale.callback(Finance.__new__(Finance), _interaction(bot), sale_id))

    assert post.content.startswith("✅ ") and "Sales Receipt `1002`" in post.content
    assert fresh_db.get_accounting_entry(item["id"])["logged_at"] is not None


def test_ebay_order_card_button_ticks_it_off(fresh_db, setup, monkeypatch):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    order = {"order_id": "03-15297-63099", "sale_date": "2026-10-12", "total_price": 16.76,
             "line_items": [{"sku": f"pallet-{pallet_id}-item-{item['item_number']}", "quantity": 1}]}
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", AsyncMock(return_value=order))
    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1003"}))
    button = ebay_module.EbayOrderConfirmButton.__new__(ebay_module.EbayOrderConfirmButton)
    button.order_id = order["order_id"]

    asyncio.run(ebay_module.EbayOrderConfirmButton.callback(button, _interaction(bot)))

    assert _post_for(fresh_db, bot, item).content.startswith("✅ ")
    assert "Sales Receipt `1003`" in _post_for(fresh_db, bot, item).content


def test_items_sold_before_this_existed_are_simply_skipped(fresh_db, setup, monkeypatch):
    bot, pallet_id = setup
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "old sale", [], 1))  # no #accounting row
    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1"}))
    asyncio.run(LogSaleConfirmView([item], "eBay", 5.0, False, "2026-10-12").confirm.callback(_interaction(bot)))
    assert fresh_db.get_item_sale(item["id"]) is not None  # the sale itself still logged fine


def test_a_deleted_post_never_breaks_logging(fresh_db, setup, monkeypatch):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    bot.accounting.messages.clear()  # someone deleted the #accounting post
    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1"}))
    interaction = _interaction(bot)

    asyncio.run(LogSaleConfirmView([item], "eBay", 5.0, False, "2026-10-12").confirm.callback(interaction))

    assert fresh_db.get_item_sale(item["id"]) is not None
    assert "Logged sale" in interaction.followup.send.call_args.args[0]


def test_preserve_history_mode_never_edits_old_posts(fresh_db, setup, monkeypatch):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    post = _post_for(fresh_db, bot, item)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", True)
    sale_id = fresh_db.create_sale("eBay", "2026-10-12", 5.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item["id"], 5.0)

    asyncio.run(finance_utils.update_accounting_entries(bot, sale_id, receipt_doc="1"))
    assert post.edits == []


def test_worklist_row_never_attaches_to_a_reissued_item_id(fresh_db, setup):
    bot, pallet_id = setup
    item = _sold_item(fresh_db, bot, pallet_id)
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET created_at = ? WHERE id = ?", ("2099-01-01T00:00:00+00:00", item["id"]))
    assert fresh_db.get_accounting_entry(item["id"]) is None
