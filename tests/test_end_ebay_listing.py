"""Ending an eBay listing when an item sells elsewhere - bot-made
(Inventory API) and hand-made (Trading API EndFixedPriceItem) listings."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import ebay_api

ITEM = {"id": 5, "pallet_id": 1, "item_number": 96}


class _Resp:
    def __init__(self, text, status=200):
        self._text, self.status = text, status

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _trading(monkeypatch, reply):
    sent = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, data=None, headers=None):
            sent.append((url, data.decode(), headers))
            return _Resp(reply)
    monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _Session())
    monkeypatch.setattr(ebay_api, "get_access_token", AsyncMock(return_value="tok"))
    return sent


OK = '<EndFixedPriceItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></EndFixedPriceItemResponse>'


def _fail(code, msg="nope"):
    return ('<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack><Errors><ErrorCode>' + code +
            '</ErrorCode><SeverityCode>Error</SeverityCode><LongMessage>' + msg + '</LongMessage></Errors></R>')


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)


def test_hand_made_listing_is_ended_by_its_listing_id(monkeypatch):
    monkeypatch.setattr(ebay_api, "_request", AsyncMock(side_effect=ebay_api.EbayAPIError("404 errorId 25713")))
    sent = _trading(monkeypatch, OK)
    assert asyncio.run(ebay_api.end_listing_for_item(ITEM, listing_id="860012952535")) == "eBay listing ended."
    url, body, headers = sent[0]
    assert headers["X-EBAY-API-CALL-NAME"] == "EndFixedPriceItem" and headers["X-EBAY-API-IAF-TOKEN"] == "tok"
    assert "<ItemID>860012952535</ItemID>" in body and "<EndingReason>NotAvailable</EndingReason>" in body


def test_bot_made_listing_is_withdrawn_without_the_trading_call(monkeypatch):
    calls = []

    async def request(method, path, json_body=None):
        calls.append((method, path))
        return {"offers": [{"status": "PUBLISHED", "offerId": "o1"}]} if method == "GET" else {}
    monkeypatch.setattr(ebay_api, "_request", request)
    sent = _trading(monkeypatch, OK)
    assert asyncio.run(ebay_api.end_listing_for_item(ITEM, listing_id="1")) == "eBay listing ended."
    assert ("POST", "/offer/o1/withdraw") in calls and sent == []


def test_already_ended_is_fine_and_other_errors_raise(monkeypatch):
    monkeypatch.setattr(ebay_api, "_request", AsyncMock(return_value={"offers": []}))
    _trading(monkeypatch, _fail("1047"))
    assert "already ended" in asyncio.run(ebay_api.end_listing_for_item(ITEM, listing_id="1"))
    _trading(monkeypatch, _fail("21916750", "Not your listing"))
    with pytest.raises(ebay_api.EbayAPIError, match="Not your listing"):
        asyncio.run(ebay_api.end_listing_for_item(ITEM, listing_id="1"))
    assert asyncio.run(ebay_api.end_listing_for_item(ITEM)) == "No live eBay listing found for it."


def test_relist_hand_made_listing_returns_the_new_id(monkeypatch):
    monkeypatch.setattr(ebay_api, "_request", AsyncMock(return_value={"offers": []}))
    sent = _trading(monkeypatch, '<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>'
                                 '<ItemID>318921999999</ItemID></R>')
    assert asyncio.run(ebay_api.relist_listing_for_item(ITEM, listing_id="318921888862")) == "318921999999"
    assert sent[0][2]["X-EBAY-API-CALL-NAME"] == "RelistFixedPriceItem" and "<ItemID>318921888862</ItemID>" in sent[0][1]
    _trading(monkeypatch, _fail("21916750", "Item cannot be relisted"))
    with pytest.raises(ebay_api.EbayAPIError, match="cannot be relisted"):
        asyncio.run(ebay_api.relist_listing_for_item(ITEM, listing_id="1"))


def test_relist_bot_made_listing_publishes_its_offer_again(monkeypatch):
    calls = []

    async def request(method, path, json_body=None):
        calls.append((method, path))
        if method == "GET":
            return {"offers": [{"status": "UNPUBLISHED", "offerId": "o7"}]}
        return {"listingId": "860000000123"}
    monkeypatch.setattr(ebay_api, "_request", request)
    assert asyncio.run(ebay_api.relist_listing_for_item(ITEM, listing_id="1")) == "860000000123"
    assert ("POST", "/offer/o7/publish") in calls
