"""
The three places that search for an eBay category (EbayConditionSelectView's
automatic AI pre-fill, EbayCategorySearchModal's manual "Search categories"
step, and /ebay category-search) now call ebay_api.search_categories() - a
live eBay Taxonomy API lookup - instead of ebay_taxonomy.search()'s local
keyword matching. search_categories() itself is a real network call, so
each of these three interaction handlers must defer immediately and use
the interaction's followup/edit-original-response mechanism, never respond
to the interaction directly after that await - same "Unknown interaction"
(error 10062) risk as test_ebay_approval_interaction_timing.py.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import cogs.ebay as ebay_module
import database as db
from cogs.ebay import Ebay
from cogs.item_flow import EbayCategorySearchModal, EbayConditionSelectView


def _interaction():
    return SimpleNamespace(
        response=SimpleNamespace(
            edit_message=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()),
        user=SimpleNamespace(id=42),
    )


def test_condition_select_defers_before_the_live_search_and_edits_original(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Item", [], 1)
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET ai_suggested_category = ? WHERE id = ?", ("ceiling fan", item_id))

    import cogs.item_flow as flow_module
    search = AsyncMock(return_value=[("176937", "Home & Garden > Ceiling Fans")])
    monkeypatch.setattr(flow_module.ebay_api, "search_categories", search)

    view = EbayConditionSelectView(item_id)
    view.select._values = ["1500"]
    event = _interaction()
    asyncio.run(view._on_select(event))

    event.response.defer.assert_awaited_once()
    event.response.edit_message.assert_not_awaited()
    event.edit_original_response.assert_awaited_once()
    search.assert_awaited_once_with("ceiling fan", limit=1)


def test_category_search_modal_defers_before_the_live_search_and_edits_original(fresh_db, monkeypatch):
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Item", [], 1)

    import cogs.item_flow as flow_module
    search = AsyncMock(return_value=[("176937", "Home & Garden > Ceiling Fans")])
    monkeypatch.setattr(flow_module.ebay_api, "search_categories", search)

    modal = EbayCategorySearchModal(item_id, "1500")
    modal.query._value = "ceiling fan"
    event = _interaction()
    asyncio.run(modal.on_submit(event))

    event.response.defer.assert_awaited_once()
    event.response.edit_message.assert_not_awaited()
    event.edit_original_response.assert_awaited_once()
    search.assert_awaited_once_with("ceiling fan", limit=25)


def test_category_search_command_defers_before_the_live_search_and_uses_followup(monkeypatch):
    monkeypatch.setattr(ebay_module, "_is_pallet_admin", lambda interaction: True)
    search = AsyncMock(return_value=[("176937", "Home & Garden > Ceiling Fans")])
    monkeypatch.setattr(ebay_module.ebay_api, "search_categories", search)

    cog = Ebay.__new__(Ebay)
    event = _interaction()
    asyncio.run(Ebay.category_search.callback(cog, event, "ceiling fan"))

    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    event.followup.send.assert_awaited_once()
    search.assert_awaited_once_with("ceiling fan", limit=10)
    assert "176937" in event.followup.send.await_args.args[0]


def test_category_search_command_still_blocks_non_admins(monkeypatch):
    monkeypatch.setattr(ebay_module, "_is_pallet_admin", lambda interaction: False)
    search = AsyncMock()
    monkeypatch.setattr(ebay_module.ebay_api, "search_categories", search)

    cog = Ebay.__new__(Ebay)
    event = _interaction()
    asyncio.run(Ebay.category_search.callback(cog, event, "ceiling fan"))

    event.response.send_message.assert_awaited_once()
    event.response.defer.assert_not_awaited()
    search.assert_not_awaited()
