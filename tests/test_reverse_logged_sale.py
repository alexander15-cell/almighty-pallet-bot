"""
/finance reverse-sale on a sale that was LOGGED (sales/sale_items rows,
normally a QuickBooks Sales Receipt). It used to only clear the item's
price, leaving the sale_items row behind - so the item could never be
logged again and Discord no longer matched QuickBooks. Now the whole sale
is reversed (database.void_logged_sale) once its Sales Receipt has been
voided in QuickBooks, with nothing deleted outright.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import finance_utils
import cogs.finance as finance_module
from cogs.finance import Finance

_ROLE = SimpleNamespace(name=config.ROLE_FINANCE_MGMT)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(finance_module.runtime_settings, "resolve_role",
                        lambda guild, name: _ROLE if name == config.ROLE_FINANCE_MGMT else None)
    monkeypatch.setattr(finance_utils, "refresh_finance_message", AsyncMock())
    monkeypatch.setattr(finance_utils, "post_to_finance_audit_log", AsyncMock())


def _interaction(category_id):
    return SimpleNamespace(
        guild=object(), user=SimpleNamespace(id=5, roles=[_ROLE]), client=SimpleNamespace(get_channel=lambda i: None),
        channel=SimpleNamespace(category_id=category_id),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.fixture
def bundle(fresh_db):
    """Two items from one pallet logged together as sale #N for $30 with a
    QuickBooks Sales Receipt."""
    pallet_id = fresh_db.create_pallet("Pallets 1 -6", category_id=77, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 10.0, actor_id=1)
    a = fresh_db.create_item(pallet_id, "lamp", [], 1)
    b = fresh_db.create_item(pallet_id, "timer", [], 1)
    sale_id = fresh_db.create_sale("eBay", "2026-10-12", 30.0, False, created_by=1)
    for item_id in (a, b):
        fresh_db.add_sale_item(sale_id, item_id, 15.0)
        fresh_db.record_item_sale(item_id, 15.0, "eBay", actor_id=1)
    fresh_db.set_sale_receipt_id(sale_id, "SR-55")
    return pallet_id, [fresh_db.get_item(a), fresh_db.get_item(b)], sale_id


def _reverse(item, voided=False, reason="buyer cancelled"):
    event = _interaction(77)
    cog = Finance.__new__(Finance)
    cog.bot = SimpleNamespace()
    asyncio.run(Finance.reverse_sale.callback(cog, event, item["item_number"], reason, voided))
    return event


def test_logged_sale_asks_for_quickbooks_void_first_and_changes_nothing(fresh_db, bundle):
    pallet_id, items, sale_id = bundle
    event = _reverse(items[0])

    text = event.response.send_message.call_args.args[0]
    assert f"sale #{sale_id}" in text and "SR-55" in text and "quickbooks_voided:True" in text
    assert "2 item(s)" in text
    assert fresh_db.get_item_sale(items[0]["id"]) is not None
    assert fresh_db.get_item(items[0]["id"])["sale_price"] == 15.0


def test_confirmed_reversal_undoes_the_whole_sale_and_items_can_be_logged_again(fresh_db, bundle):
    pallet_id, items, sale_id = bundle
    revenue_before = fresh_db.get_pallet_financials(pallet_id)["revenue_so_far"]

    event = _reverse(items[0], voided=True)

    assert "Reversed logged **sale #" in event.followup.send.call_args.args[0]
    for item in items:
        assert fresh_db.get_item_sale(item["id"]) is None
        assert fresh_db.get_item(item["id"])["sale_price"] is None
    assert fresh_db.get_pallet_financials(pallet_id)["revenue_so_far"] == revenue_before - 30.0

    with fresh_db.get_conn() as conn:
        # Nothing deleted outright: the sale row stays, its lines moved to the archive.
        assert conn.execute("SELECT total_price FROM sales WHERE id = ?", (sale_id,)).fetchone()["total_price"] == 30.0
        assert conn.execute("SELECT COUNT(*) AS n FROM voided_sale_items WHERE sale_id = ?", (sale_id,)).fetchone()["n"] == 2
        voided = conn.execute("SELECT * FROM voided_sales WHERE sale_id = ?", (sale_id,)).fetchone()
        assert voided["quickbooks_sales_receipt_id"] == "SR-55" and voided["voided_by"] == 5
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM finance_transactions WHERE type = 'reversal'").fetchone()["n"] == 2

    new_sale = fresh_db.create_sale("eBay", "2026-10-13", 18.0, False, created_by=1)
    fresh_db.add_sale_item(new_sale, items[0]["id"], 18.0)  # previously blocked by the UNIQUE item_id
    assert fresh_db.get_item_sale(items[0]["id"])["sale_id"] == new_sale


def test_sale_that_never_reached_quickbooks_reverses_without_the_flag(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=77, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)
    sale_id = fresh_db.create_sale("In Person", "2026-10-12", 5.0, False, created_by=1)
    fresh_db.add_sale_item(sale_id, item_id, 5.0)
    fresh_db.record_item_sale(item_id, 5.0, "In Person", actor_id=1)

    event = _reverse(fresh_db.get_item(item_id))

    assert "never reached QuickBooks" in event.followup.send.call_args.args[0]
    assert fresh_db.get_item_sale(item_id) is None


def test_reversing_twice_is_a_clear_no_op(fresh_db, bundle):
    _, items, _ = bundle
    _reverse(items[0], voided=True)
    event = _reverse(items[1], voided=True)
    assert "nothing to reverse" in event.followup.send.call_args.args[0]


def test_void_logged_sale_rejects_unknown_or_already_voided(fresh_db, bundle):
    _, _, sale_id = bundle
    fresh_db.void_logged_sale(sale_id, "x", actor_id=1)
    with pytest.raises(ValueError, match="already reversed"):
        fresh_db.void_logged_sale(sale_id, "x", actor_id=1)
    with pytest.raises(ValueError, match="No sale"):
        fresh_db.void_logged_sale(99999, "x", actor_id=1)


def test_unlogged_price_still_reverses_the_old_way(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=77, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)
    fresh_db.record_item_sale(item_id, 9.0, "Other", actor_id=1)  # /finance record-sale, no sale row
    event = _reverse(fresh_db.get_item(item_id))
    assert "was $9.00" in event.followup.send.call_args.args[0]


def test_move_back_now_points_at_reverse_sale_and_works_after_it(fresh_db, bundle):
    _, items, _ = bundle
    with pytest.raises(ValueError, match="/finance reverse-sale"):
        fresh_db.unwind_item_downstream_data(items[0]["id"], fresh_db.STATUS_LISTED, actor_id=1)
    _reverse(items[0], voided=True)
    fresh_db.unwind_item_downstream_data(items[0]["id"], fresh_db.STATUS_LISTED, actor_id=1)  # no longer blocked
