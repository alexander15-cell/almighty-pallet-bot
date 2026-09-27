"""
Regression guard: finalize_ebay_approval (EbayListingModal.on_submit's
target) used to send its FIRST response to the interaction only at the very
end, after already posting a new card (with photo uploads), fetching, and
clearing the old one - real Discord API round trips that can take longer
than the 3 seconds Discord allows for a modal's first response, causing a
real "Unknown interaction" (error 10062) failure in production. It must
defer immediately once the fast validity checks pass, then use a followup
for the final confirmation - never respond to the interaction directly
after any of that slower work.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import config
import database as db
import cogs.item_flow as flow


def interaction():
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        user=SimpleNamespace(id=42),
    )


def test_finalize_ebay_approval_defers_before_the_slow_work_and_uses_followup(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", False)
    pallet_id = fresh_db.create_pallet("Pallet", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Item", [], 1)
    fresh_db.update_status(item_id, db.STATUS_QUEUE_REVIEW, new_message_id=500)
    fresh_db.set_shared_channel("queue-review", 900)
    fresh_db.set_shared_channel("awaiting-listing", 901)

    old_message = SimpleNamespace(delete=AsyncMock())
    old_channel = SimpleNamespace(fetch_message=AsyncMock(return_value=old_message))
    new_message = SimpleNamespace(id=999)
    sender = AsyncMock(return_value=new_message)
    monkeypatch.setattr(flow, "send_item_card", sender)

    channels = {900: old_channel, 901: SimpleNamespace(id=901)}
    cog = flow.ItemFlow(SimpleNamespace(get_channel=lambda cid: channels.get(cid)))
    monkeypatch.setattr(flow.finance_utils, "refresh_finance_message", AsyncMock())

    event = interaction()
    asyncio.run(cog.finalize_ebay_approval(
        event, item_id, "3000", "12345", "A Title", 19.99, {},
        weight_lb=2.0, length_in=10.0, width_in=8.0, height_in=4.0, source_message_id=500,
    ))

    # The FIRST response must be the defer - never send_message once the
    # slow work (posting the new card, clearing the old one) has started.
    event.response.defer.assert_awaited_once()
    event.response.send_message.assert_not_awaited()
    event.followup.send.assert_awaited_once()
    sender.assert_awaited_once()
    old_message.delete.assert_awaited_once()
    assert db.get_item(item_id)["status"] == db.STATUS_AWAITING_LISTING
