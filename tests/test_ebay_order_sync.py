"""
The eBay order auto-sync feature (cogs/ebay.py): order_poll_loop (checks
eBay for new orders and posts a confirm card per order matched to a
tracked item by SKU), _match_order_line_items (the SKU -> item resolution
shared by the poll loop and the confirm button), and EbayOrderConfirmButton
(re-fetches the order fresh on click and logs the sale the same way
/finance log-sale does - see finance_utils.post_sale_to_quickbooks).

Mirrors tests/test_quickbooks_poll.py's pattern for exercising a
tasks.loop's raw coroutine directly (Loop.coro) rather than starting the
real discord.ext.tasks.Loop, and
tests/test_finance_interaction_timing.py's pattern for exercising a
DynamicItem button's callback directly via __new__.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord
import pytest

import config
import database as db
import ebay_api
import cogs.ebay as ebay_module
from cogs.ebay import Ebay, EbayOrderConfirmButton, _match_order_line_items


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


class _FakeSelf:
    """Stands in for `self` inside order_poll_loop.coro - only needs a
    `.bot` attribute, same as test_quickbooks_poll.py's _FakeSelf."""
    def __init__(self, bot):
        self.bot = bot


@pytest.fixture
def channel_ready(fresh_db):
    fresh_db.set_shared_channel("ebay-orders", 321)
    return _FakeChannel()


def _order(order_id="12-34567-89012", total_price=25.50, sale_date="2026-02-03", line_items=None):
    return {
        "order_id": order_id,
        "sale_date": sale_date,
        "total_price": total_price,
        "line_items": line_items or [{"sku": "pallet-REPLACE-item-1", "quantity": 1}],
    }


# --------------------------------------------------------------- _match_order_line_items


def test_match_order_line_items_resolves_a_tracked_item(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    order = _order(line_items=[{"sku": f"pallet-{pallet_id}-item-{item['item_number']}", "quantity": 1}])

    matched = _match_order_line_items(order)
    assert len(matched) == 1
    assert matched[0]["id"] == item_id


def test_match_order_line_items_skips_unparseable_skus(fresh_db):
    order = _order(line_items=[{"sku": "SOME-OTHER-LISTING-SKU", "quantity": 1}])
    assert _match_order_line_items(order) == []


def test_match_order_line_items_skips_skus_with_no_matching_item(fresh_db):
    order = _order(line_items=[{"sku": "pallet-999-item-999", "quantity": 1}])
    assert _match_order_line_items(order) == []


def test_match_order_line_items_matches_a_bundle_partially(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    order = _order(line_items=[
        {"sku": f"pallet-{pallet_id}-item-{item['item_number']}", "quantity": 1},
        {"sku": "pallet-999-item-999", "quantity": 1},
    ])
    matched = _match_order_line_items(order)
    assert len(matched) == 1
    assert matched[0]["id"] == item_id


# --------------------------------------------------------------- _post_ebay_order_match


def test_post_ebay_order_match_warns_and_skips_when_channel_not_set_up(fresh_db, caplog):
    bot = _FakeBot(_FakeChannel())
    asyncio.run(ebay_module._post_ebay_order_match(bot, _order(), []))  # must not raise


def test_post_ebay_order_match_posts_an_embed_with_the_confirm_button(fresh_db, channel_ready):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    bot = _FakeBot(channel_ready)

    asyncio.run(ebay_module._post_ebay_order_match(bot, _order(total_price=19.99), [item]))

    assert len(channel_ready.sent) == 1
    kwargs = channel_ready.sent[0]
    assert "19.99" in kwargs["embed"].fields[1].value
    buttons = [c for c in kwargs["view"].children if isinstance(c, ebay_module.EbayOrderConfirmButton)]
    assert len(buttons) == 1
    assert buttons[0].order_id == "12-34567-89012"


# --------------------------------------------------------------- order_poll_loop


def test_poll_loop_noops_when_ebay_not_enabled(fresh_db, monkeypatch, channel_ready):
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    bot = _FakeBot(channel_ready)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("should not have queried eBay - EBAY_ENABLED is False")
    monkeypatch.setattr(ebay_module.ebay_api, "get_orders", fail_if_called)

    asyncio.run(Ebay.order_poll_loop.coro(_FakeSelf(bot)))
    assert channel_ready.sent == []


def test_poll_loop_posts_only_unseen_orders_matched_to_a_tracked_item(fresh_db, monkeypatch, channel_ready):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"
    fresh_db.mark_ebay_order_seen("already-seen")

    async def fake_get_orders(since, limit=50):
        return [
            _order(order_id="already-seen", line_items=[{"sku": sku, "quantity": 1}]),
            _order(order_id="brand-new", line_items=[{"sku": sku, "quantity": 1}]),
        ]
    monkeypatch.setattr(ebay_module.ebay_api, "get_orders", fake_get_orders)

    bot = _FakeBot(channel_ready)
    asyncio.run(Ebay.order_poll_loop.coro(_FakeSelf(bot)))

    assert len(channel_ready.sent) == 1
    assert fresh_db.has_seen_ebay_order("brand-new")


def test_poll_loop_marks_an_unmatched_order_seen_without_posting(fresh_db, monkeypatch, channel_ready):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)

    async def fake_get_orders(since, limit=50):
        return [_order(order_id="no-match", line_items=[{"sku": "pallet-999-item-999", "quantity": 1}])]
    monkeypatch.setattr(ebay_module.ebay_api, "get_orders", fake_get_orders)

    bot = _FakeBot(channel_ready)
    asyncio.run(Ebay.order_poll_loop.coro(_FakeSelf(bot)))

    assert channel_ready.sent == []
    assert fresh_db.has_seen_ebay_order("no-match")


def test_poll_loop_survives_an_ebay_api_error(fresh_db, monkeypatch, channel_ready):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)

    async def fake_get_orders(since, limit=50):
        raise ebay_api.EbayAPIError("eBay is down")
    monkeypatch.setattr(ebay_module.ebay_api, "get_orders", fake_get_orders)

    bot = _FakeBot(channel_ready)
    asyncio.run(Ebay.order_poll_loop.coro(_FakeSelf(bot)))  # must not raise
    assert channel_ready.sent == []


def test_poll_loop_does_not_mark_seen_when_posting_fails_so_it_retries_next_poll(fresh_db, monkeypatch, channel_ready):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"

    async def fake_get_orders(since, limit=50):
        return [_order(order_id="flaky", line_items=[{"sku": sku, "quantity": 1}])]
    monkeypatch.setattr(ebay_module.ebay_api, "get_orders", fake_get_orders)

    class _FakeResponse:
        status = 500
        reason = "Internal Server Error"

    async def broken_channel_send(**kwargs):
        raise discord.HTTPException(_FakeResponse(), "boom")
    channel_ready.send = broken_channel_send

    bot = _FakeBot(channel_ready)
    asyncio.run(Ebay.order_poll_loop.coro(_FakeSelf(bot)))  # must not raise
    assert not fresh_db.has_seen_ebay_order("flaky")


# --------------------------------------------------------------- EbayOrderConfirmButton


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = object()
    monkeypatch.setattr(ebay_module.runtime_settings, "resolve_role", lambda guild, name: role)
    monkeypatch.setattr(ebay_module, "_is_pallet_admin", lambda interaction: False)
    return role


def _confirm_interaction(role):
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
        client=SimpleNamespace(),
        user=SimpleNamespace(id=42, roles=[role]),
        guild=object(),
    )


def _button(order_id="12-34567-89012"):
    button = EbayOrderConfirmButton.__new__(EbayOrderConfirmButton)
    button.order_id = order_id
    return button


def test_confirm_button_requires_finance_role(fresh_db, monkeypatch):
    monkeypatch.setattr(ebay_module.runtime_settings, "resolve_role", lambda guild, name: None)
    event = _confirm_interaction(object())
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))
    event.response.send_message.assert_awaited_once()
    event.response.defer.assert_not_awaited()


def test_confirm_button_defers_before_ebay_lookup_and_uses_followup(fresh_db, monkeypatch, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"

    monkeypatch.setattr(
        ebay_module.ebay_api, "get_order",
        AsyncMock(return_value=_order(line_items=[{"sku": sku, "quantity": 1}])),
    )
    monkeypatch.setattr(
        ebay_module.finance_utils, "post_sale_to_quickbooks",
        AsyncMock(return_value={"ok": True, "sales_receipt_doc": "1001"}),
    )
    monkeypatch.setattr(ebay_module.finance_utils, "refresh_finance_message", AsyncMock())
    monkeypatch.setattr(ebay_module.finance_utils, "post_to_finance_audit_log", AsyncMock())

    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))

    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    event.followup.send.assert_awaited_once()
    ebay_module.ebay_api.get_order.assert_awaited_once_with("12-34567-89012")


def test_confirm_button_logs_the_sale_and_posts_to_quickbooks(fresh_db, monkeypatch, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"

    monkeypatch.setattr(
        ebay_module.ebay_api, "get_order",
        AsyncMock(return_value=_order(total_price=25.50, line_items=[{"sku": sku, "quantity": 1}])),
    )
    monkeypatch.setattr(
        ebay_module.finance_utils, "post_sale_to_quickbooks",
        AsyncMock(return_value={"ok": True, "sales_receipt_doc": "1001"}),
    )
    monkeypatch.setattr(ebay_module.finance_utils, "refresh_finance_message", AsyncMock())
    audit_log = AsyncMock()
    monkeypatch.setattr(ebay_module.finance_utils, "post_to_finance_audit_log", audit_log)

    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))

    sale = db.get_item_sale(item_id)
    assert sale is not None
    assert sale["allocated_price"] == 25.50
    assert db.get_item(item_id)["sale_platform"] == "eBay"
    audit_log.assert_awaited_once()
    event.message.edit.assert_awaited_once_with(view=None)
    assert "✅" in event.followup.send.await_args.args[0]


def test_confirm_button_splits_price_across_a_bundle(fresh_db, monkeypatch, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item1_id = fresh_db.create_item(pallet_id, "Widget 1", [], 1)
    item2_id = fresh_db.create_item(pallet_id, "Widget 2", [], 1)
    item1 = fresh_db.get_item(item1_id)
    item2 = fresh_db.get_item(item2_id)

    monkeypatch.setattr(
        ebay_module.ebay_api, "get_order",
        AsyncMock(return_value=_order(total_price=10.0, line_items=[
            {"sku": f"pallet-{pallet_id}-item-{item1['item_number']}", "quantity": 1},
            {"sku": f"pallet-{pallet_id}-item-{item2['item_number']}", "quantity": 1},
        ])),
    )
    monkeypatch.setattr(
        ebay_module.finance_utils, "post_sale_to_quickbooks",
        AsyncMock(return_value={"ok": True, "sales_receipt_doc": "1001"}),
    )
    monkeypatch.setattr(ebay_module.finance_utils, "refresh_finance_message", AsyncMock())
    monkeypatch.setattr(ebay_module.finance_utils, "post_to_finance_audit_log", AsyncMock())

    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))

    total = db.get_item_sale(item1_id)["allocated_price"] + db.get_item_sale(item2_id)["allocated_price"]
    assert round(total, 2) == 10.0


def test_confirm_button_rejects_an_already_sold_item(fresh_db, monkeypatch, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"
    existing_sale_id = db.create_sale("eBay", "2026-01-01", 19.99, False, created_by=1)
    db.add_sale_item(existing_sale_id, item_id, 19.99)

    monkeypatch.setattr(
        ebay_module.ebay_api, "get_order",
        AsyncMock(return_value=_order(line_items=[{"sku": sku, "quantity": 1}])),
    )
    quickbooks_call = AsyncMock()
    monkeypatch.setattr(ebay_module.finance_utils, "post_sale_to_quickbooks", quickbooks_call)

    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))

    quickbooks_call.assert_not_awaited()
    assert "already logged" in event.followup.send.await_args.args[0]


def test_confirm_button_handles_no_remaining_match(fresh_db, monkeypatch, bypass_role_check):
    monkeypatch.setattr(
        ebay_module.ebay_api, "get_order",
        AsyncMock(return_value=_order(line_items=[{"sku": "pallet-999-item-999", "quantity": 1}])),
    )
    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))
    assert "nothing was submitted" in event.followup.send.await_args.args[0]


def test_confirm_button_reports_ebay_lookup_failure(fresh_db, monkeypatch, bypass_role_check):
    async def raise_error(order_id):
        raise ebay_api.EbayAPIError("order not found")
    monkeypatch.setattr(ebay_module.ebay_api, "get_order", raise_error)

    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))
    assert "Couldn't re-fetch" in event.followup.send.await_args.args[0]


def test_confirm_button_reports_quickbooks_failure_without_losing_the_sale(fresh_db, monkeypatch, bypass_role_check):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)
    item = fresh_db.get_item(item_id)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"

    monkeypatch.setattr(
        ebay_module.ebay_api, "get_order",
        AsyncMock(return_value=_order(line_items=[{"sku": sku, "quantity": 1}])),
    )
    monkeypatch.setattr(
        ebay_module.finance_utils, "post_sale_to_quickbooks",
        AsyncMock(return_value={"ok": False, "error": "QuickBooks is down"}),
    )
    monkeypatch.setattr(ebay_module.finance_utils, "refresh_finance_message", AsyncMock())

    event = _confirm_interaction(bypass_role_check)
    asyncio.run(EbayOrderConfirmButton.callback(_button(), event))

    assert db.get_item_sale(item_id) is not None  # the sale itself was still saved
    assert "retry-sale" in event.followup.send.await_args.args[0]
