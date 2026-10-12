"""
eBay order automation (config.EBAY_ORDER_AUTOMATION): cogs/ebay.py's
_reconcile_ebay_order_statuses driving ItemFlow.auto_mark_sold_from_ebay /
auto_mark_shipped_from_ebay from eBay's own order data, plus
ebay_api.get_recently_modified_orders' parsing/pagination.

The ItemFlow transitions run for real against fake Discord channels (same
fakes as test_confirm_listed_buttons.py), so these check the actual status
changes, cards, and audit trail - not a mock of them.
"""
import asyncio
import os
import urllib.parse

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from unittest.mock import AsyncMock

import pytest

import config
import ebay_api
import cogs.ebay as ebay_module
import cogs.item_flow as item_flow_module
from cogs.item_flow import ItemFlow

LISTED, SOLD, ORDERS, ACCOUNTING = 501, 502, 503, 504
AFTER = "2099-01-01T00:00:00.000Z"   # after the test item was created/listed
BEFORE = "2000-01-01T00:00:00.000Z"  # before it was listed


class _FakeMessage:
    def __init__(self, msg_id):
        self.id = msg_id
        self.deleted = False
        self.edits = []
        self.embeds = []

    async def delete(self):
        self.deleted = True

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.sent = []
        self.messages = {}
        self._next_id = channel_id * 1000

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        self._next_id += 1
        msg = _FakeMessage(self._next_id)
        msg.embeds = list(kwargs.get("embeds") or ([kwargs["embed"]] if kwargs.get("embed") else []))
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            # Real Discord raises NotFound (an HTTPException) - OSError is in
            # the same TRANSIENT_DISCORD_ERRORS tuple and needs no fake response.
            raise OSError(f"message {message_id} not found")
        return self.messages[message_id]


class _FakeBot:
    def __init__(self):
        self.channels = {cid: _FakeChannel(cid) for cid in (LISTED, SOLD, ORDERS, ACCOUNTING)}
        self.cog = None

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_cog(self, name):
        return self.cog


@pytest.fixture
def env(fresh_db, monkeypatch):
    for stage, cid in (("listed", LISTED), ("sold", SOLD), ("ebay-orders", ORDERS),
                       ("accounting", ACCOUNTING)):
        fresh_db.set_shared_channel(stage, cid)
    monkeypatch.setattr(item_flow_module.finance_utils, "refresh_finance_message", AsyncMock())
    # The missing-card fetch (_ensure_order_card) - unavailable by default so
    # the older tests stay about status moves; the card tests below override.
    monkeypatch.setattr(ebay_module.ebay_api, "get_order",
                        AsyncMock(side_effect=ebay_api.EbayAPIError("not faked in this test")))
    bot = _FakeBot()
    bot.cog = ItemFlow(bot)
    pallet_id = fresh_db.create_pallet("Pallet-A", category_id=1, created_by=1)
    return bot, pallet_id


def _listed_item(db, bot, pallet_id):
    """An item with a real card sitting in #listed."""
    item_id = db.create_item(pallet_id, "widget", [], 1)
    db.save_ebay_listing_data(item_id, "Widget", "123", "1000", 20.0, {})
    card = asyncio.run(bot.channels[LISTED].send(content="card"))
    db.update_status(item_id, db.STATUS_LISTED, actor_id=1, new_message_id=card.id)
    return db.get_item(item_id), card


def _order(item, order_id="O-1", date=AFTER, payment="PAID", cancel="NONE_REQUESTED", fulfillment="NOT_STARTED",
           sku=None, legacy=None):
    return {
        "order_id": order_id, "order_date": date, "payment_status": payment, "cancel_state": cancel,
        "line_items": [{
            "line_item_id": f"L-{order_id}",
            "sku": sku if sku is not None else f"pallet-{item['pallet_id']}-item-{item['item_number']}",
            "legacy_item_id": legacy, "fulfillment_status": fulfillment,
        }],
    }


def _run(bot, orders):
    return asyncio.run(ebay_module._reconcile_ebay_order_statuses(bot, orders))


def _order_alerts(bot):
    return [m["content"] for m in bot.channels[ORDERS].sent if m.get("content")]


def _order_cards(bot):
    return [m for m in bot.channels[ORDERS].sent if m.get("embed") is not None]


def _full_order(item, order_id="O-1", total=16.76):
    """ebay_api.get_order's shape - what the Confirm & log sale card needs."""
    return {"order_id": order_id, "sale_date": "2026-10-12", "total_price": total,
            "line_items": [{"sku": f"pallet-{item['pallet_id']}-item-{item['item_number']}", "quantity": 1}]}


# ------------------------------------------------------------------- Sold

def test_paid_order_moves_listed_item_to_sold(fresh_db, env):
    bot, pallet_id = env
    item, card = _listed_item(fresh_db, bot, pallet_id)

    counts = _run(bot, [_order(item)])

    after = fresh_db.get_item(item["id"])
    assert counts == {"sold": 1, "shipped": 0, "alerts": 0}
    assert after["status"] == fresh_db.STATUS_SOLD and after["sale_platform"] == "eBay"
    assert after["sale_price"] is None  # price/QuickBooks stays the human Confirm & log sale click
    assert card.deleted  # old #listed card cleared
    sold_cards = bot.channels[SOLD].sent
    assert len(sold_cards) == 1 and after["current_message_id"] == bot.channels[SOLD]._next_id
    assert any("Confirm & log sale" in m["content"] for m in bot.channels[ACCOUNTING].sent)
    assert not any("COGS" in m["content"] for m in bot.channels[ACCOUNTING].sent)
    assert any("moved to Sold" in a for a in _order_alerts(bot))
    with fresh_db.get_conn() as conn:
        event = conn.execute(
            "SELECT * FROM item_events WHERE item_id = ? AND to_status = ? ORDER BY id DESC", (item["id"], "sold")
        ).fetchone()
    assert event["actor_id"] is None and "O-1" in event["note"]


def test_reprocessing_the_same_order_does_nothing_more(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    _run(bot, [_order(item)])
    sent_before = len(bot.channels[SOLD].sent), len(bot.channels[ORDERS].sent)

    assert _run(bot, [_order(item)]) == {"sold": 0, "shipped": 0, "alerts": 0}
    assert (len(bot.channels[SOLD].sent), len(bot.channels[ORDERS].sent)) == sent_before


def test_a_human_moving_the_item_back_sticks(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    _run(bot, [_order(item)])
    fresh_db.update_status(item["id"], fresh_db.STATUS_LISTED, actor_id=9)  # e.g. /item move-back

    _run(bot, [_order(item, date="2100-01-01T00:00:00.000Z")])

    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED


@pytest.mark.parametrize("payment", ["PENDING", "FAILED", "FULLY_REFUNDED"])
def test_unpaid_orders_are_left_alone(fresh_db, env, payment):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    _run(bot, [_order(item, payment=payment)])
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED


def test_order_with_a_pending_cancel_request_is_left_alone(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    _run(bot, [_order(item, cancel="IN_PROGRESS")])
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED


def test_order_from_before_the_current_listing_is_ignored(fresh_db, env):
    """Returned + relisted: the old order must not mark the new listing sold."""
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    with fresh_db.get_conn() as conn:  # backdate creation only, so the item-created guard passes
        conn.execute("UPDATE items SET created_at = ? WHERE id = ?", ("1999-01-01T00:00:00+00:00", item["id"]))
    _run(bot, [_order(item, date=BEFORE)])
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED


def test_unmatched_skus_are_ignored(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    assert _run(bot, [_order(item, sku="someone-elses-sku")]) == {"sold": 0, "shipped": 0, "alerts": 0}
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED


# ---------------------------------------------------------------- Shipped

def test_shipped_line_moves_sold_item_to_shipped_and_updates_its_card(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    _run(bot, [_order(item)])
    sold_card = bot.channels[SOLD].messages[fresh_db.get_item(item["id"])["current_message_id"]]

    counts = _run(bot, [_order(item, fulfillment="FULFILLED")])

    assert counts["shipped"] == 1
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SHIPPED
    assert fresh_db.get_item(item["id"])["shipped_at"] is not None
    assert sold_card.embeds, "send_item_card should have posted an embed"
    assert sold_card.edits and sold_card.edits[-1]["view"] is None  # buttons removed
    assert sold_card.edits[-1]["embeds"][0].footer.text.endswith("Shipped ✅ (per eBay)")


def test_sold_and_shipped_in_the_same_poll(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    assert _run(bot, [_order(item, fulfillment="FULFILLED")]) == {"sold": 1, "shipped": 1, "alerts": 0}
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SHIPPED


def test_manually_sold_item_still_gets_auto_shipped(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    fresh_db.update_status(item["id"], fresh_db.STATUS_SOLD, actor_id=1)
    _run(bot, [_order(item, fulfillment="FULFILLED")])
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SHIPPED


# ------------------------------------------------------------------ alerts

def test_cancelled_order_on_a_sold_item_alerts_once_and_changes_nothing(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    _run(bot, [_order(item)])
    before = len(_order_alerts(bot))

    _run(bot, [_order(item, cancel="CANCELED")])
    _run(bot, [_order(item, cancel="CANCELED")])

    alerts = _order_alerts(bot)[before:]
    assert len(alerts) == 1 and "cancelled" in alerts[0] and "/item move-back" in alerts[0]
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SOLD


def test_cancelled_order_for_a_still_listed_item_is_silent(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    assert _run(bot, [_order(item, cancel="CANCELED")]) == {"sold": 0, "shipped": 0, "alerts": 0}
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED


def test_sale_of_an_on_hold_item_alerts_once_and_leaves_it_on_hold(fresh_db, env):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    fresh_db.update_status(item["id"], fresh_db.STATUS_ON_HOLD, actor_id=1)

    _run(bot, [_order(item)])
    _run(bot, [_order(item)])

    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_ON_HOLD
    alerts = [a for a in _order_alerts(bot) if "on hold" in a]
    assert len(alerts) == 1


# ------------------------------------- missing "Confirm & log sale" cards
# The production bug: older orders (created > 3 days ago, so the new-order
# check never saw them) got moved to Sold here once eBay updated them, but
# never got a card to log the sale from.

def test_auto_sold_order_without_a_card_gets_one(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    get_order = AsyncMock(return_value=_full_order(item, total=16.04))
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", get_order)

    _run(bot, [_order(item)])

    cards = _order_cards(bot)
    assert len(cards) == 1
    assert cards[0]["embed"].title == "📦 New eBay order" and "$16.04" in cards[0]["embed"].fields[1].value
    assert cards[0]["view"].children[0].order_id == "O-1"
    assert fresh_db.has_seen_ebay_order("O-1")
    get_order.assert_awaited_once_with("O-1")


def test_item_already_sold_from_an_uncarded_order_gets_its_card(fresh_db, env, monkeypatch):
    """Exactly the case that happened: items were moved to Sold before
    this fix, with no card - the next poll that sees the order posts it."""
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    fresh_db.update_status(item["id"], fresh_db.STATUS_SOLD, actor_id=None)
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", AsyncMock(return_value=_full_order(item)))

    _run(bot, [_order(item, fulfillment="FULFILLED")])

    assert len(_order_cards(bot)) == 1
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SHIPPED


def test_a_card_is_never_posted_twice(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    get_order = AsyncMock(return_value=_full_order(item))
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", get_order)

    _run(bot, [_order(item)])
    _run(bot, [_order(item, fulfillment="FULFILLED")])

    assert len(_order_cards(bot)) == 1 and get_order.await_count == 1


def test_order_the_new_order_check_already_carded_is_left_alone(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    fresh_db.mark_ebay_order_seen("O-1")
    get_order = AsyncMock(return_value=_full_order(item))
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", get_order)

    _run(bot, [_order(item)])

    assert _order_cards(bot) == [] and get_order.await_count == 0


def test_sale_already_logged_another_way_gets_no_card(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    fresh_db.update_status(item["id"], fresh_db.STATUS_SOLD, actor_id=1)
    sale_id = fresh_db.create_sale("eBay", "2026-10-12", 16.04, False, created_by=1)  # e.g. /finance log-sale
    fresh_db.add_sale_item(sale_id, item["id"], 16.04)
    get_order = AsyncMock(return_value=_full_order(item))
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", get_order)

    _run(bot, [_order(item)])

    assert _order_cards(bot) == [] and get_order.await_count == 0


def test_ebay_failure_retries_the_card_next_poll(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)

    _run(bot, [_order(item)])  # get_order fails (fixture default)
    assert _order_cards(bot) == [] and not fresh_db.has_seen_ebay_order("O-1")
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SOLD  # the move itself still happened

    monkeypatch.setattr(ebay_module.ebay_api, "get_order", AsyncMock(return_value=_full_order(item)))
    _run(bot, [_order(item)])
    assert len(_order_cards(bot)) == 1


def test_cancelled_or_unpaid_orders_get_no_card(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    fresh_db.update_status(item["id"], fresh_db.STATUS_SOLD, actor_id=1)
    get_order = AsyncMock(return_value=_full_order(item))
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", get_order)

    _run(bot, [_order(item, cancel="CANCELED"), _order(item, order_id="O-2", payment="FULLY_REFUNDED")])

    assert _order_cards(bot) == [] and get_order.await_count == 0


# ------------------------------------------------------------ poll wiring

def test_poll_loop_runs_automation_only_when_enabled(fresh_db, env, monkeypatch):
    bot, pallet_id = env
    item, _ = _listed_item(fresh_db, bot, pallet_id)
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(ebay_module.ebay_api, "get_orders", AsyncMock(return_value=[]))
    modified = AsyncMock(return_value=[_order(item)])
    monkeypatch.setattr(ebay_module.ebay_api, "get_recently_modified_orders", modified)

    class _Self:
        pass
    me = _Self()
    me.bot = bot

    monkeypatch.setattr(config, "EBAY_ORDER_AUTOMATION", False)
    asyncio.run(ebay_module.Ebay.order_poll_loop.coro(me))
    modified.assert_not_called()
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_LISTED

    monkeypatch.setattr(config, "EBAY_ORDER_AUTOMATION", True)
    asyncio.run(ebay_module.Ebay.order_poll_loop.coro(me))
    modified.assert_awaited_once()
    assert fresh_db.get_item(item["id"])["status"] == fresh_db.STATUS_SOLD


def test_automation_survives_an_ebay_error(fresh_db, env, monkeypatch):
    bot, _ = env

    async def boom(since):
        raise ebay_api.EbayAPIError("eBay is down")
    monkeypatch.setattr(ebay_module.ebay_api, "get_recently_modified_orders", boom)
    assert asyncio.run(ebay_module._run_order_automation(bot)) == {}


# ------------------------------------------- get_recently_modified_orders

class _FakeResponse:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responder, calls):
        self._responder, self._calls = responder, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, auth=None, headers=None, json=None):
        return _FakeResponse(200, {"access_token": "tok", "expires_in": 7200})

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append(url)
        return _FakeResponse(*self._responder(url))


def test_get_recently_modified_orders_parses_statuses_and_paginates(monkeypatch):
    ebay_api._cached_access_token = None
    monkeypatch.setattr(config, "EBAY_APP_ID", "a")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "c")
    monkeypatch.setattr(config, "EBAY_REFRESH_TOKEN", "r")
    monkeypatch.setattr(config, "EBAY_ENVIRONMENT", "production")

    def raw(n):
        return {
            "orderId": f"O-{n}", "creationDate": "2026-10-01T00:00:00.000Z", "orderPaymentStatus": "PAID",
            "cancelStatus": {"cancelState": "NONE_REQUESTED"},
            "buyer": {"username": "not-returned"},
            "lineItems": [{"lineItemId": f"L-{n}", "sku": f"pallet-1-item-{n}", "legacyItemId": 1100 + n,
                           "lineItemFulfillmentStatus": "FULFILLED"}],
        }

    def responder(url):
        offset = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
        page = [raw(offset + i) for i in range(min(200, 210 - offset))]
        return 200, {"orders": page, "total": 210}
    calls = []
    monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(responder, calls))

    from datetime import datetime, timezone
    orders = asyncio.run(ebay_api.get_recently_modified_orders(datetime(2026, 10, 1, tzinfo=timezone.utc)))

    assert len(orders) == 210 and len(calls) == 2
    assert "lastmodifieddate%3A%5B2026-10-01T00%3A00%3A00.000Z..%5D" in calls[0]
    assert orders[0] == {
        "order_id": "O-0", "order_date": "2026-10-01T00:00:00.000Z", "payment_status": "PAID",
        "cancel_state": "NONE_REQUESTED",
        "line_items": [{"line_item_id": "L-0", "sku": "pallet-1-item-0", "legacy_item_id": "1100",
                        "fulfillment_status": "FULFILLED"}],
    }
    ebay_api._cached_access_token = None
