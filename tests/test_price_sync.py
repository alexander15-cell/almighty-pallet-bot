"""eBay -> bot price sync (price_sync.py, ebay_api.get_live_listing_price)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import config
import ebay_api
import price_sync
from cogs.item_flow import PRICE_FIELD


@pytest.fixture
def env(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    monkeypatch.setattr("finance_utils.refresh_finance_message", AsyncMock())
    monkeypatch.setattr(ebay_api, "get_active_listings", AsyncMock(return_value=[]))
    return fresh_db


def _listed(db, price, listing_id, fmt="FixedPrice", status=None, pallet="P"):
    pallet_id = db.create_pallet(pallet + listing_id, category_id=int(listing_id[-3:]), created_by=1)
    item_id = db.create_item(pallet_id, "lamp", [], 1)
    db.save_ebay_listing_data(item_id, "Lamp", "123", "1500", price, {}, listing_format=fmt)
    db.set_ebay_item_id(item_id, listing_id)
    db.update_status(item_id, status or db.STATUS_LISTED, actor_id=1)
    return db.get_item(item_id)


class _Message:
    def __init__(self, fields):
        embed = discord.Embed(title="Lamp")
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=True)
        self.embeds = [embed]
        self.edited = None

    async def edit(self, embeds=None):
        self.edited = embeds


def _bot(message):
    flow = SimpleNamespace(_fetch_current_card=AsyncMock(return_value=message))
    return SimpleNamespace(get_cog=lambda name: flow if name == "ItemFlow" else None)


def test_changed_price_updates_the_bot_and_the_card(env, monkeypatch):
    item = _listed(env, 24.99, "110000000101")
    same = _listed(env, 10.00, "110000000102")
    prices = {"110000000101": {"price": 19.99, "currency": "USD"}, "110000000102": {"price": 10.0, "currency": "USD"}}
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(side_effect=lambda lid: prices[lid]))
    message = _Message([("📦 Pallet", "P"), ("Item #", "1"), ("Status", "listed"), ("Website price cents", "2499")])

    result = asyncio.run(price_sync.sync_prices(_bot(message)))

    assert result["checked"] == 2 and [(c[0]["id"], c[1], c[2]) for c in result["changed"]] == [(item["id"], 24.99, 19.99)]
    assert env.get_ebay_listing_data(item["id"])["price"] == 19.99
    assert env.get_ebay_listing_data(same["id"])["price"] == 10.0
    fields = {f.name: f.value for f in message.edited[0].fields}
    assert fields[PRICE_FIELD] == "$19.99" and fields["Website price cents"] == "1999"
    assert [f.name for f in message.edited[0].fields][3] == PRICE_FIELD  # right after Status
    assert "Price synced from eBay: $24.99 -> $19.99" in [e["note"] for e in env.get_item_events(item["id"])]


def test_existing_price_field_is_replaced_not_duplicated(env, monkeypatch):
    _listed(env, 24.99, "110000000201")
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(return_value={"price": 30.0, "currency": "USD"}))
    message = _Message([("Status", "listed"), (PRICE_FIELD, "$24.99")])
    asyncio.run(price_sync.sync_prices(_bot(message)))
    names = [f.name for f in message.edited[0].fields]
    assert names.count(PRICE_FIELD) == 1 and message.edited[0].fields[1].value == "$30.00"


def test_auctions_ended_listings_errors_and_non_listed_items_are_left_alone(env, monkeypatch):
    auction = _listed(env, 5.0, "110000000301", fmt="Auction")
    ended = _listed(env, 12.0, "110000000302")
    broken = _listed(env, 8.0, "110000000303")
    sold = _listed(env, 9.0, "110000000304", status=env.STATUS_SOLD)

    async def live(lid):
        if lid == "110000000303":
            raise ebay_api.EbayAPIError("eBay down")
        return None if lid == "110000000302" else {"price": 99.0, "currency": "USD"}
    monkeypatch.setattr(ebay_api, "get_live_listing_price", live)

    result = asyncio.run(price_sync.sync_prices(_bot(None)))

    assert result["checked"] == 2  # the auction and the sold item aren't even checked
    assert result["changed"] == [] and len(result["errors"]) == 1
    for item, price in ((auction, 5.0), (ended, 12.0), (broken, 8.0), (sold, 9.0)):
        assert env.get_ebay_listing_data(item["id"])["price"] == price


def test_live_price_reader(monkeypatch):
    monkeypatch.setattr(ebay_api, "get_app_access_token", AsyncMock(return_value="t"))
    replies = {}

    class _Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body

        async def json(self, content_type=None):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, headers=None):
            replies["url"] = url
            return _Resp(*replies["next"])
    monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _Session())

    replies["next"] = (200, {"price": {"value": "19.99", "currency": "USD"}, "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"]})
    assert asyncio.run(ebay_api.get_live_listing_price("110000000101")) == {"price": 19.99, "currency": "USD"}
    assert "get_item_by_legacy_id?legacy_item_id=110000000101" in replies["url"]
    replies["next"] = (200, {"currentBidPrice": {"value": "3.00"}, "buyingOptions": ["AUCTION"]})
    assert asyncio.run(ebay_api.get_live_listing_price("1")) is None
    replies["next"] = (404, {"errors": [{"errorId": 11001}]})
    assert asyncio.run(ebay_api.get_live_listing_price("1")) is None


# ------------------------------------------- matching eBay listings to items

XML = """<?xml version="1.0" encoding="UTF-8"?>
<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack>
<ActiveList><ItemArray>
<Item><ItemID>860012952535</ItemID><SKU>pallet-1-item-96</SKU><Title>Halo 4" Canless LED</Title>
 <SellingStatus><CurrentPrice currencyID="USD">22.0</CurrentPrice></SellingStatus><QuantityAvailable>1</QuantityAvailable>
 <ListingType>FixedPriceItem</ListingType><PrimaryCategory><CategoryID>20706</CategoryID></PrimaryCategory><ConditionID>3000</ConditionID></Item>
<Item><ItemID>318921540269</ItemID><SKU>pallet-1-item-43</SKU><Title>Recessed Light</Title>
 <SellingStatus><CurrentPrice currencyID="USD">22.0</CurrentPrice></SellingStatus><QuantityAvailable>17</QuantityAvailable></Item>
</ItemArray><PaginationResult><TotalNumberOfPages>1</TotalNumberOfPages></PaginationResult></ActiveList>
</GetMyeBaySellingResponse>"""


def test_active_listings_reply_is_parsed():
    listings, pages = ebay_api._parse_active_listings(XML)
    assert pages == 1 and [l["sku"] for l in listings] == ["pallet-1-item-96", "pallet-1-item-43"]
    first = listings[0]
    assert first["listing_id"] == "860012952535" and first["price"] == 22.0 and first["category_id"] == "20706"
    assert listings[1]["quantity"] == 17
    with pytest.raises(ebay_api.EbayAPIError, match="token expired"):
        ebay_api._parse_active_listings(
            '<R xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Failure</Ack><Errors><LongMessage>token expired'
            "</LongMessage></Errors></R>")


def _item(db, number_of_items=1):
    pallet_id = db.create_pallet("Pallets 1-6", category_id=1, created_by=1)
    ids = [db.create_item(pallet_id, "x", [], 1) for _ in range(number_of_items)]
    for item_id in ids:
        db.update_status(item_id, db.STATUS_LISTED, actor_id=1)
    return pallet_id, [db.get_item(i) for i in ids]


def _listing(sku, lid, price=22.0):
    return {"listing_id": lid, "sku": sku, "title": "Lamp", "price": price, "quantity": 1,
            "listing_type": "FixedPriceItem", "category_id": "20706", "condition_id": "3000"}


def test_linking_only_fills_blanks(env, monkeypatch):
    pallet_id, (no_data, no_id, same, different) = _item(env, 4)
    for item in (no_id, same, different):
        env.save_ebay_listing_data(item["id"], "Lamp", "1", "1500", 15.0, {})
    env.set_ebay_item_id(same["id"], "333")
    env.set_ebay_item_id(different["id"], "999")
    sku = lambda i: f"pallet-{pallet_id}-item-{i['item_number']}"
    monkeypatch.setattr(ebay_api, "get_active_listings", AsyncMock(return_value=[
        _listing(sku(no_data), "111"), _listing(sku(no_id), "222"), _listing(sku(same), "333"),
        _listing(sku(different), "444"), _listing("pallet-99-item-1", "555"), _listing("", "666")]))

    r = asyncio.run(price_sync.link_listings())

    assert [i["id"] for i, _ in r["created"]] == [no_data["id"]] and [i["id"] for i, _ in r["linked"]] == [no_id["id"]]
    assert r["already"] == 1 and [i["id"] for i, _ in r["conflicts"]] == [different["id"]] and len(r["unmatched"]) == 2
    created = env.get_ebay_listing_data(no_data["id"])
    assert created["ebay_item_id"] == "111" and created["price"] == 22.0 and created["category_id"] == "20706"
    assert env.get_ebay_listing_data(no_id["id"])["ebay_item_id"] == "222"
    assert env.get_ebay_listing_data(no_id["id"])["price"] == 15.0  # linking never touches the price
    assert env.get_ebay_listing_data(different["id"])["ebay_item_id"] == "999"  # never overwritten


def test_price_sync_links_first_then_updates_the_newly_linked(env, monkeypatch):
    pallet_id, (item,) = _item(env)
    env.save_ebay_listing_data(item["id"], "Halo", "1", "1500", 15.0, {})
    monkeypatch.setattr(ebay_api, "get_active_listings", AsyncMock(return_value=[
        _listing(f"pallet-{pallet_id}-item-{item['item_number']}", "860012952535")]))
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(return_value={"price": 22.0, "currency": "USD"}))

    result = asyncio.run(price_sync.sync_prices(_bot(None)))

    assert len(result["linked"]["linked"]) == 1 and result["checked"] == 1
    assert env.get_ebay_listing_data(item["id"])["price"] == 22.0


def test_listing_fetch_failure_still_syncs_the_linked_ones(env, monkeypatch):
    _listed(env, 10.0, "110000000901")
    monkeypatch.setattr(ebay_api, "get_active_listings", AsyncMock(side_effect=ebay_api.EbayAPIError("scope")))
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(return_value={"price": 12.0, "currency": "USD"}))
    result = asyncio.run(price_sync.sync_prices(_bot(None)))
    assert result["linked"] is None and len(result["changed"]) == 1


# ------------------------------------------------- card edits under Discord's limit

class _Discord:
    """Hands out a fresh card per fetch; refuses edits after `refuse_after`."""
    def __init__(self, refuse_after):
        self.calls, self.refuse_after = 0, refuse_after

    def bot(self):
        discord_ = self

        class _Card(_Message):
            async def edit(self, embeds=None):
                discord_.calls += 1
                if discord_.calls > discord_.refuse_after:
                    raise discord.HTTPException(SimpleNamespace(status=429, reason="Too Many Requests"),
                                                {"code": 30046, "message": "Maximum number of edits"})
        flow = SimpleNamespace(_fetch_current_card=AsyncMock(side_effect=lambda *a: _Card([("Status", "listed")])))
        return SimpleNamespace(get_cog=lambda name: flow if name == "ItemFlow" else None)


def test_card_edits_are_queued_and_stop_at_discords_limit(env, monkeypatch):
    monkeypatch.setattr(price_sync, "CARD_EDIT_GAP_SECONDS", 0)
    items = [_listed(env, 10.0, f"11000000050{n}") for n in range(4)]
    monkeypatch.setattr(ebay_api, "get_live_listing_price", AsyncMock(return_value={"price": 12.0, "currency": "USD"}))
    fake = _Discord(refuse_after=2)

    result = asyncio.run(price_sync.sync_prices(fake.bot()))

    assert len(result["changed"]) == 4 and fake.calls == 3 and result["cards_pending"] == 2
    for item in items:  # the bot's own price is updated regardless of Discord
        assert env.get_ebay_listing_data(item["id"])["price"] == 12.0

    fake.refuse_after = 99
    done = asyncio.run(price_sync.update_queued_cards(fake.bot()))
    assert done == {"edited": 2, "remaining": 0, "limited": False}


def test_queued_card_for_an_item_no_longer_listed_is_dropped(env, monkeypatch):
    item = _listed(env, 10.0, "110000000601")
    env.queue_price_card_update(item["id"], 11.0)
    env.queue_price_card_update(item["id"], 12.0)  # latest price wins, still one entry
    assert env.pending_price_card_updates() == [{"item_id": item["id"], "price": 12.0}]
    env.update_status(item["id"], env.STATUS_SOLD, actor_id=1)
    message = _Message([("Status", "listed")])
    assert asyncio.run(price_sync.update_queued_cards(_bot(message), gap=0))["remaining"] == 0
    assert message.edited is None


def test_recently_synced_cards_are_requeued_and_unchanged_cards_not_edited(env, monkeypatch):
    item = _listed(env, 10.0, "110000000701")
    other = _listed(env, 10.0, "110000000702")
    env.update_listing_price(item["id"], 14.0, 10.0, "Price synced from eBay")
    assert env.queue_recently_synced_cards() == 1
    assert env.pending_price_card_updates() == [{"item_id": item["id"], "price": 14.0}]

    message = _Message([("Status", "listed"), (PRICE_FIELD, "$14.00")])
    assert asyncio.run(price_sync.update_card_price(_bot(message), env.get_item(item["id"]), 14.0)) is True
    assert message.edited is None  # already right - Discord not touched
    assert other["id"] not in [r["item_id"] for r in env.pending_price_card_updates()]
