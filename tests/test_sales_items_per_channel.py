"""
One QuickBooks Product/Service per sales channel (eBay / In Person / Other),
so the P&L splits Sales by channel. QuickBooks books a Sales Receipt line to
its Item's income account, so the Item is what actually decides the line.
Blank settings must keep the old behavior exactly.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import finance_utils
import quickbooks as qb


@pytest.fixture
def channel_items(monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_SALES_ITEM_ID", "21")
    monkeypatch.setattr(config, "QUICKBOOKS_IN_PERSON_SALES_ITEM_ID", "22")
    monkeypatch.setattr(config, "QUICKBOOKS_OTHER_SALES_ITEM_ID", "23")


@pytest.fixture
def no_channel_items(monkeypatch):
    for name in ("QUICKBOOKS_EBAY_SALES_ITEM_ID", "QUICKBOOKS_IN_PERSON_SALES_ITEM_ID",
                 "QUICKBOOKS_OTHER_SALES_ITEM_ID"):
        monkeypatch.setattr(config, name, "")


@pytest.mark.parametrize("platform,channel", [
    ("eBay", "ebay"), ("EBAY ", "ebay"), ("In Person", "in_person"), ("in person", "in_person"),
    ("Other: Mercari", "other"), ("Facebook Marketplace", "other"), ("", "other"), (None, "other"),
])
def test_sales_channel(platform, channel):
    assert finance_utils.sales_channel(platform) == channel


@pytest.mark.parametrize("platform,item_id", [("eBay", "21"), ("In Person", "22"), ("Other: Mercari", "23")])
def test_configured_item_decides_and_no_account_override_is_sent(channel_items, platform, item_id):
    assert finance_utils.sales_item_for_platform(platform) == (item_id, None)


def test_blank_settings_keep_the_old_item_and_account_override(no_channel_items):
    assert finance_utils.sales_item_for_platform("eBay") == (
        config.QUICKBOOKS_CASH_SALES_ITEM_ID, config.QUICKBOOKS_EBAY_SALES_ACCOUNT_ID)
    assert finance_utils.sales_item_for_platform("In Person") == (
        config.QUICKBOOKS_CASH_SALES_ITEM_ID, config.QUICKBOOKS_SALES_INCOME_ACCOUNT_ID)
    assert finance_utils.sales_item_for_platform("Other: Mercari") == (
        config.QUICKBOOKS_CASH_SALES_ITEM_ID, config.QUICKBOOKS_SALES_INCOME_ACCOUNT_ID)


def test_only_one_channel_configured(no_channel_items, monkeypatch):
    monkeypatch.setattr(config, "QUICKBOOKS_EBAY_SALES_ITEM_ID", "21")
    assert finance_utils.sales_item_for_platform("eBay") == ("21", None)
    assert finance_utils.sales_item_for_platform("In Person")[0] == config.QUICKBOOKS_CASH_SALES_ITEM_ID


def _sale(fresh_db, platform):
    return fresh_db.create_sale(platform, "2026-10-13", 25.0, False, created_by=1)


@pytest.mark.parametrize("platform,item_id,taxable", [
    ("eBay", "21", False), ("In Person", "22", True), ("Other: Mercari", "23", True),
])
def test_post_sale_uses_the_channel_item(fresh_db, monkeypatch, channel_items, platform, item_id, taxable):
    receipt = AsyncMock(return_value={"id": "sr-9", "doc_number": "1009"})
    monkeypatch.setattr(qb, "create_sales_receipt", receipt)
    sale_id = _sale(fresh_db, platform)

    result = asyncio.run(finance_utils.post_sale_to_quickbooks(sale_id, platform, 25.0, False, "2026-10-13"))

    assert result["ok"]
    kwargs = receipt.call_args.kwargs
    assert kwargs["item_id"] == item_id
    assert kwargs["income_account_id"] is None
    assert kwargs["taxable"] is taxable
    assert kwargs["amount"] == 25.0


def _capture_payload(monkeypatch):
    sent = {}

    async def fake_request(method, path, json_body=None, **_):
        sent["body"] = json_body
        return {"SalesReceipt": {"Id": "1", "DocNumber": "1"}}

    monkeypatch.setattr(qb, "_request", fake_request)
    return sent


def test_receipt_payload_omits_account_override_when_none(monkeypatch):
    sent = _capture_payload(monkeypatch)
    asyncio.run(qb.create_sales_receipt("1", "22", None, "24", 10.0, "2026-10-13", "memo", taxable=True))
    detail = sent["body"]["Line"][0]["SalesItemLineDetail"]
    assert detail["ItemRef"] == {"value": "22"}
    assert "ItemAccountRef" not in detail


def test_receipt_payload_keeps_account_override_when_given(monkeypatch):
    sent = _capture_payload(monkeypatch)
    asyncio.run(qb.create_sales_receipt("1", "2", "98", "24", 10.0, "2026-10-13", "memo", taxable=False))
    detail = sent["body"]["Line"][0]["SalesItemLineDetail"]
    assert detail["ItemAccountRef"] == {"value": "98"}
    assert detail["TaxCodeRef"] == {"value": "NON"}
