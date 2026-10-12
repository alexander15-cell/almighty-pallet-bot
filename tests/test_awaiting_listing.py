"""
The Awaiting Listing card after the CSV batch paths were removed:
List on eBay (API) / Edit Details / Mark Listed (Other) - cogs/item_flow.py.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
import cogs.item_flow as item_flow_module
from cogs.item_flow import AwaitingListingView, EditListingDetailsModal, ItemFlow

LISTED, AWAITING = 801, 802


class _Msg:
    def __init__(self, msg_id):
        self.id = msg_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _Msg(self.id * 10 + len(self.sent))


class _Bot:
    def __init__(self):
        self.channels = {LISTED: _Channel(LISTED), AWAITING: _Channel(AWAITING)}

    def get_channel(self, cid):
        return self.channels.get(cid)


@pytest.fixture(autouse=True)
def roles(monkeypatch):
    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", lambda guild, name: "role")
    monkeypatch.setattr(item_flow_module.finance_utils, "refresh_finance_message", AsyncMock())


def _interaction(message=None):
    return SimpleNamespace(
        guild=object(), user=SimpleNamespace(id=3, roles=["role"]), message=message or _Msg(1),
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.fixture
def awaiting(fresh_db):
    fresh_db.set_shared_channel("listed", LISTED)
    fresh_db.set_shared_channel("awaiting-listing", AWAITING)
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "lamp", [], 1)
    fresh_db.save_ebay_listing_data(item_id, "Brass Lamp", "117503", "1500", 25.0, {"Brand": "Kichler"},
                                    listing_format="Auction", auction_duration="Days_7",
                                    weight_lb=3.0, length_in=10, width_in=8, height_in=6)
    fresh_db.update_status(item_id, fresh_db.STATUS_AWAITING_LISTING, actor_id=1)
    return fresh_db.get_item(item_id)


def _view(item_id):
    async def build():
        return AwaitingListingView(item_id)
    return asyncio.run(build())


def test_card_has_list_edit_and_mark_listed_only(monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    labels = [c.label for c in _view(4).children]
    assert labels == ["List on eBay", "Edit Details", "Mark Listed (Other)"]
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    assert [c.label for c in _view(4).children] == ["Edit Details", "Mark Listed (Other)"]


def test_edit_details_opens_the_form_prefilled_with_saved_details(fresh_db, awaiting):
    event = _interaction()
    asyncio.run(ItemFlow(_Bot()).open_edit_listing_details(event, awaiting["id"]))

    modal = event.response.send_modal.call_args.args[0]
    assert isinstance(modal, EditListingDetailsModal)
    assert modal.ebay_title.default == "Brass Lamp" and modal.price.default == "25.00"
    assert modal.item_specifics.default == "Brand: Kichler"
    assert (modal.condition_id, modal.category_id, modal.listing_format, modal.auction_duration) == \
        ("1500", "117503", "Auction", "Days_7")


def test_edit_details_saves_only_its_fields(fresh_db, awaiting):
    event = _interaction()
    asyncio.run(ItemFlow(_Bot()).save_listing_detail_edits(
        event, awaiting["id"], "Brass Table Lamp", 30.0, {"Brand": "Kichler", "Color": "Brass"}, 3.5, 11, 8, 6))

    saved = fresh_db.get_ebay_listing_data(awaiting["id"])
    assert saved["ebay_title"] == "Brass Table Lamp" and saved["price"] == 30.0 and saved["weight_lb"] == 3.5
    assert saved["item_specifics"] == {"Brand": "Kichler", "Color": "Brass"}
    assert (saved["category_id"], saved["condition_id"], saved["listing_format"], saved["auction_duration"]) == \
        ("117503", "1500", "Auction", "Days_7")
    assert fresh_db.get_item(awaiting["id"])["status"] == fresh_db.STATUS_AWAITING_LISTING
    assert "List on eBay" in event.followup.send.call_args.args[0]


def test_edit_details_refused_once_the_item_moved_on(fresh_db, awaiting):
    fresh_db.update_status(awaiting["id"], fresh_db.STATUS_LISTED, actor_id=1)
    event = _interaction()
    asyncio.run(ItemFlow(_Bot()).save_listing_detail_edits(event, awaiting["id"], "X", 1.0, {}, 1, 1, 1, 1))
    assert "nothing was changed" in event.response.send_message.call_args.args[0]
    assert fresh_db.get_ebay_listing_data(awaiting["id"])["ebay_title"] == "Brass Lamp"


def test_edit_form_submit_saves_instead_of_approving(fresh_db, awaiting, monkeypatch):
    cog = SimpleNamespace(save_listing_detail_edits=AsyncMock(), finalize_ebay_approval=AsyncMock())

    async def run():
        modal = EditListingDetailsModal(awaiting["id"], "1500", "117503", "FixedPrice", None)
        event = SimpleNamespace(client=SimpleNamespace(get_cog=lambda name: cog))
        await modal._submit(event, "T", 5.0, {}, 1.0, 1, 1, 1)
    asyncio.run(run())

    cog.save_listing_detail_edits.assert_awaited_once()
    cog.finalize_ebay_approval.assert_not_called()


def test_list_on_ebay_moves_to_listed_and_marks_the_category_proven(fresh_db, awaiting, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(item_flow_module.ebay_api, "create_listing", AsyncMock(return_value={"ebay_item_id": "1109"}))
    monkeypatch.setattr(item_flow_module.recovery_safety, "require_current_card", AsyncMock(return_value=True))
    bot = _Bot()

    asyncio.run(ItemFlow(bot).list_on_ebay_api(_interaction(), awaiting["id"]))

    assert fresh_db.get_item(awaiting["id"])["status"] == fresh_db.STATUS_LISTED
    assert fresh_db.get_ebay_listing_data(awaiting["id"])["ebay_item_id"] == "1109"
    with fresh_db.get_conn() as conn:
        assert conn.execute("SELECT confirmed_count FROM ebay_category_usage WHERE category_id = '117503'"
                            ).fetchone()["confirmed_count"] == 1


def test_list_on_ebay_failure_points_at_edit_details(fresh_db, awaiting, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(item_flow_module.ebay_api, "create_listing",
                        AsyncMock(side_effect=RuntimeError("Invalid category ID.")))
    monkeypatch.setattr(item_flow_module.recovery_safety, "require_current_card", AsyncMock(return_value=True))
    event = _interaction()

    asyncio.run(ItemFlow(_Bot()).list_on_ebay_api(event, awaiting["id"]))

    text = event.followup.send.call_args.args[0]
    assert "Invalid category ID." in text and "Edit Details" in text
    assert fresh_db.get_item(awaiting["id"])["status"] == fresh_db.STATUS_AWAITING_LISTING
