"""
/finance log-sale end to end (cogs/finance.py): the command's validation,
the cost/COGS modal, the confirm/cancel step, and what happens once
confirmed - writing sale/sale_items, updating each item's sale_price/
platform, and pushing to QuickBooks (a Sales Receipt plus a COGS Journal
Entry). Also /finance retry-sale, for picking up after a QuickBooks
failure without losing or double-submitting anything.

QuickBooks itself is mocked at the quickbooks.create_sales_receipt/
create_journal_entry level - the HTTP-level behavior of those functions is
already covered by test_quickbooks.py.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import quickbooks as qb
import cogs.finance as finance_module
from cogs.finance import Finance, LogSaleCogsModal, LogSaleConfirmView


class _FakeRole:
    def __init__(self, name):
        self.name = name


_FINANCE_ROLE = _FakeRole(config.ROLE_FINANCE_MGMT)


@pytest.fixture(autouse=True)
def role_lookup(monkeypatch):
    monkeypatch.setattr(
        finance_module.runtime_settings, "resolve_role",
        lambda guild, name: _FINANCE_ROLE if name == config.ROLE_FINANCE_MGMT else None,
    )


class _FakeMessage:
    def __init__(self):
        self.edited_with = None

    async def edit(self, **kwargs):
        self.edited_with = kwargs


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.sent_embed = None
        self.sent_view = None
        self.modal = None

    async def send_message(self, content=None, embed=None, view=None, **kwargs):
        self.content = content
        self.sent_embed = embed
        self.sent_view = view

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, ephemeral=True, thinking=True):
        pass

    async def edit_message(self, content=None, embed=None, view=None, **kwargs):
        self.content = content
        self.sent_embed = embed
        self.sent_view = view


class _FakeFollowup:
    def __init__(self):
        self.content = None

    async def send(self, content=None, **kwargs):
        self.content = content


def _interaction(roles=(_FINANCE_ROLE,), message=None, client=None):
    return SimpleNamespace(
        guild=object(),
        user=SimpleNamespace(id=1, roles=list(roles)),
        response=_FakeResponse(),
        followup=_FakeFollowup(),
        message=message or _FakeMessage(),
        client=client or SimpleNamespace(),
    )


class _FakeAuditChannel:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append(args)


@pytest.fixture
def cog():
    return Finance.__new__(Finance)


@pytest.fixture(autouse=True)
def no_op_finance_refresh(monkeypatch):
    monkeypatch.setattr(finance_module.finance_utils, "refresh_finance_message", AsyncMock())


# --------------------------------------------------------------- log-sale command


def test_requires_finance_management_role(fresh_db, cog):
    interaction = _interaction(roles=[])
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 10.0))
    assert "Finance Management" in interaction.response.content


def test_rejects_non_positive_price(fresh_db, cog):
    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 0.0))
    assert "positive" in interaction.response.content


def test_rejects_bad_item_references(fresh_db, cog):
    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Nonexistent#1", 10.0, "eBay"))
    assert "Couldn't read" in interaction.response.content


def test_rejects_an_item_already_in_a_logged_sale(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 10.0, 3.0, 3.0)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 10.0, "eBay"))
    assert "already part of a logged sale" in interaction.response.content


def test_infers_platform_from_a_shared_mark_as_sold_value(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.record_sale_platform(item_id, "Facebook Marketplace", actor_id=1)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 10.0))

    assert isinstance(interaction.response.modal, LogSaleCogsModal)
    assert interaction.response.modal.platform == "Facebook Marketplace"


def test_requires_explicit_platform_when_items_disagree(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item1 = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item2 = fresh_db.create_item(pallet_id, "Gadget", [], 1)
    fresh_db.record_sale_platform(item1, "eBay", actor_id=1)
    fresh_db.record_sale_platform(item2, "In Person", actor_id=1)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(
        cog, interaction, f"Pallet A#{fresh_db.get_item(item1)['item_number']}, "
                            f"Pallet A#{fresh_db.get_item(item2)['item_number']}",
        20.0,
    ))
    assert "Pass `platform` explicitly" in interaction.response.content


def test_explicit_platform_overrides_and_opens_the_cogs_modal(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.create_item(pallet_id, "Widget", [], 1)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 10.0, "In Person"))

    modal = interaction.response.modal
    assert isinstance(modal, LogSaleCogsModal)
    assert modal.platform == "In Person"
    assert modal.entries.default == "cost 0.00, cogs 0.00"


def test_modal_pre_fills_one_line_per_item_for_a_bundle(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.create_item(pallet_id, "Gadget", [], 1)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1, Pallet A#2", 10.0, "eBay"))

    modal = interaction.response.modal
    assert "Pallet A#1: cost 0.00, cogs 0.00" in modal.entries.default
    assert "Pallet A#2: cost 0.00, cogs 0.00" in modal.entries.default


def test_modal_prefills_manifest_cost_for_a_matched_single_item(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    charge_id = fresh_db.create_manual_invoice_charge(100.0, submitted_by=1)
    lot_id = fresh_db.upsert_manifest_lot(charge_id, 100.0, 200.0, "m.csv", 1, [
        {"sku": "A1", "product": "Widget", "retail_price": 150.0},
    ])
    lines = fresh_db.get_manifest_lines(lot_id)
    fresh_db.match_manifest_line(lines[0]["id"], item_id)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 10.0, "eBay"))

    # cost = (invoice_amount / total_retail_value) * item retail price = (100/200)*150 = 75.00
    assert interaction.response.modal.entries.default == "cost 75.00, cogs 75.00"


def test_modal_prefills_manifest_cost_per_item_in_a_bundle(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    matched_item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.create_item(pallet_id, "Gadget", [], 1)
    charge_id = fresh_db.create_manual_invoice_charge(100.0, submitted_by=1)
    lot_id = fresh_db.upsert_manifest_lot(charge_id, 100.0, 200.0, "m.csv", 1, [
        {"sku": "A1", "product": "Widget", "retail_price": 150.0},
    ])
    lines = fresh_db.get_manifest_lines(lot_id)
    fresh_db.match_manifest_line(lines[0]["id"], matched_item_id)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1, Pallet A#2", 10.0, "eBay"))

    default = interaction.response.modal.entries.default
    assert "Pallet A#1: cost 75.00, cogs 75.00" in default
    assert "Pallet A#2: cost 0.00, cogs 0.00" in default


def test_modal_flags_manifest_unmatched_item_instead_of_a_plain_blank(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    fresh_db.mark_item_manifest_unmatched(item_id)

    interaction = _interaction()
    asyncio.run(Finance.log_sale.callback(cog, interaction, "Pallet A#1", 10.0, "eBay"))

    default = interaction.response.modal.entries.default
    assert "no manifest match" in default
    assert "cost 0.00, cogs 0.00" in default


# ---------------------------------------------------------------- cogs modal


def test_modal_on_submit_rejects_malformed_entry_without_showing_confirmation(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    modal = LogSaleCogsModal([item], ["Pallet A#1"], "eBay", 10.0, False, "2026-09-30")
    modal.entries._value = "garbage"

    interaction = _interaction()
    asyncio.run(modal.on_submit(interaction))

    assert "⚠️" in interaction.response.content
    assert interaction.response.sent_embed is None


def test_modal_on_submit_shows_confirmation_for_valid_entry(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    modal = LogSaleCogsModal([item], ["Pallet A#1"], "eBay", 10.0, False, "2026-09-30")
    modal.entries._value = "cost 4.00, cogs 4.00"

    interaction = _interaction()
    asyncio.run(modal.on_submit(interaction))

    assert interaction.response.sent_embed is not None
    assert isinstance(interaction.response.sent_view, LogSaleConfirmView)


# ------------------------------------------------------------ confirm/cancel


def test_cancel_button_does_not_write_anything(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")

    interaction = _interaction()
    asyncio.run(view.cancel.callback(interaction))

    assert interaction.response.content == "Cancelled - nothing was logged."
    assert fresh_db.get_item_sale(item["id"]) is None


def test_confirm_writes_sale_and_updates_item_price(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"}))
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")
    message = _FakeMessage()
    interaction = _interaction(message=message)
    asyncio.run(view.confirm.callback(interaction))

    assert message.edited_with == {"view": None}
    sale = fresh_db.get_item_sale(item["id"])
    assert sale is not None
    updated_item = fresh_db.get_item(item["id"])
    assert updated_item["sale_price"] == 10.0
    assert updated_item["sale_platform"] == "eBay"
    assert "1001" in interaction.followup.content
    assert "JE-1" in interaction.followup.content


def test_confirm_posts_to_the_audit_log_on_success(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    fresh_db.set_shared_channel("finance-audit-log", 777)
    audit_channel = _FakeAuditChannel()
    client = SimpleNamespace(get_channel=lambda cid: audit_channel if cid == 777 else None)

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"}))
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")
    interaction = _interaction(client=client)
    asyncio.run(view.confirm.callback(interaction))

    assert len(audit_channel.sent) == 1
    assert "1001" in audit_channel.sent[0][0]
    assert "JE-1" in audit_channel.sent[0][0]


def test_confirm_does_not_post_to_the_audit_log_on_quickbooks_failure(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    fresh_db.set_shared_channel("finance-audit-log", 777)
    audit_channel = _FakeAuditChannel()
    client = SimpleNamespace(get_channel=lambda cid: audit_channel if cid == 777 else None)

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(side_effect=qb.QuickBooksError("boom")))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")
    interaction = _interaction(client=client)
    asyncio.run(view.confirm.callback(interaction))

    assert audit_channel.sent == []


def test_confirm_splits_price_evenly_across_a_bundle(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item1 = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    item2 = fresh_db.get_item(fresh_db.create_item(pallet_id, "Gadget", [], 1))

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"}))
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    parsed = {item1["id"]: (3.0, 3.0), item2["id"]: (4.0, 4.0)}
    view = LogSaleConfirmView([item1, item2], parsed, "eBay", 10.0, False, "2026-09-30")
    interaction = _interaction()
    asyncio.run(view.confirm.callback(interaction))

    assert fresh_db.get_item(item1["id"])["sale_price"] == 5.0
    assert fresh_db.get_item(item2["id"])["sale_price"] == 5.0


def test_confirm_routes_ebay_to_the_ebay_sales_account_non_taxable(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))

    receipt_mock = AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"})
    monkeypatch.setattr(qb, "create_sales_receipt", receipt_mock)
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")
    asyncio.run(view.confirm.callback(_interaction()))

    kwargs = receipt_mock.call_args.kwargs
    assert kwargs["income_account_id"] == config.QUICKBOOKS_EBAY_SALES_ACCOUNT_ID
    assert kwargs["taxable"] is False


def test_confirm_routes_facebook_to_the_sales_account_taxable(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))

    receipt_mock = AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"})
    monkeypatch.setattr(qb, "create_sales_receipt", receipt_mock)
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "Facebook Marketplace", 10.0, False, "2026-09-30")
    asyncio.run(view.confirm.callback(_interaction()))

    kwargs = receipt_mock.call_args.kwargs
    assert kwargs["income_account_id"] == config.QUICKBOOKS_SALES_INCOME_ACCOUNT_ID
    assert kwargs["taxable"] is True
    assert kwargs["deposit_account_id"] == config.QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID


def test_confirm_already_deposited_uses_the_bank_account(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))

    receipt_mock = AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"})
    monkeypatch.setattr(qb, "create_sales_receipt", receipt_mock)
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "In Person", 10.0, True, "2026-09-30")
    asyncio.run(view.confirm.callback(_interaction()))

    assert receipt_mock.call_args.kwargs["deposit_account_id"] == config.QUICKBOOKS_BANK_ACCOUNT_ID


def test_confirm_blocks_if_item_was_logged_elsewhere_while_pending(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    other_sale = fresh_db.create_sale("eBay", "2026-09-30", 5.0, False, created_by=1)
    fresh_db.add_sale_item(other_sale, item["id"], 5.0, 1.0, 1.0)

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")
    interaction = _interaction()
    asyncio.run(view.confirm.callback(interaction))

    assert "logged in another sale" in interaction.followup.content


def test_confirm_saves_sale_even_when_quickbooks_fails(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(side_effect=qb.QuickBooksError("boom")))

    view = LogSaleConfirmView([item], {item["id"]: (4.0, 4.0)}, "eBay", 10.0, False, "2026-09-30")
    interaction = _interaction()
    asyncio.run(view.confirm.callback(interaction))

    assert "retry-sale" in interaction.followup.content
    sale = fresh_db.get_item_sale(item["id"])
    assert sale is not None
    assert fresh_db.get_sale(sale["sale_id"])["cogs_logged_at"] is None


# --------------------------------------------------------------- retry-sale


def test_retry_sale_requires_role(fresh_db, cog):
    interaction = _interaction(roles=[])
    asyncio.run(Finance.retry_sale.callback(cog, interaction, 1))
    assert "Finance Management" in interaction.response.content


def test_retry_sale_unknown_id(fresh_db, cog):
    interaction = _interaction()
    asyncio.run(Finance.retry_sale.callback(cog, interaction, 999))
    assert "No sale #999" in interaction.response.content


def test_retry_sale_already_logged(fresh_db, cog):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item["id"], 10.0, 4.0, 4.0)
    fresh_db.mark_sale_logged(sale_id, "je-1")

    interaction = _interaction()
    asyncio.run(Finance.retry_sale.callback(cog, interaction, sale_id))
    assert "already fully logged" in interaction.response.content


def test_retry_sale_skips_an_already_created_receipt(fresh_db, cog, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item["id"], 10.0, 4.0, 4.0)
    fresh_db.set_sale_receipt_id(sale_id, "sr-already-made")

    receipt_mock = AsyncMock()
    monkeypatch.setattr(qb, "create_sales_receipt", receipt_mock)
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    interaction = _interaction()
    asyncio.run(Finance.retry_sale.callback(cog, interaction, sale_id))

    receipt_mock.assert_not_awaited()
    assert "JE-1" in interaction.followup.content
    assert fresh_db.get_sale(sale_id)["cogs_logged_at"] is not None


def test_retry_sale_posts_to_the_audit_log_on_success(fresh_db, cog, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item["id"], 10.0, 4.0, 4.0)
    fresh_db.set_shared_channel("finance-audit-log", 777)
    audit_channel = _FakeAuditChannel()
    client = SimpleNamespace(get_channel=lambda cid: audit_channel if cid == 777 else None)

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(return_value={"id": "sr-1", "doc_number": "1001"}))
    monkeypatch.setattr(qb, "create_journal_entry", AsyncMock(return_value={"id": "je-1", "doc_number": "JE-1"}))

    interaction = _interaction(client=client)
    asyncio.run(Finance.retry_sale.callback(cog, interaction, sale_id))

    assert len(audit_channel.sent) == 1
    assert "retry-sale" in audit_channel.sent[0][0]


def test_retry_sale_reports_a_continued_failure(fresh_db, cog, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item = fresh_db.get_item(fresh_db.create_item(pallet_id, "Widget", [], 1))
    sale_id = fresh_db.create_sale("eBay", "2026-09-30", 10.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item["id"], 10.0, 4.0, 4.0)

    monkeypatch.setattr(qb, "create_sales_receipt", AsyncMock(side_effect=qb.QuickBooksError("still down")))

    interaction = _interaction()
    asyncio.run(Finance.retry_sale.callback(cog, interaction, sale_id))
    assert "Still failing" in interaction.followup.content
