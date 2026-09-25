"""
ItemFlow._clear_old_card (cogs/item_flow.py) - the shared best-effort
cleanup used by every stage transition to remove an item's now-stale card.

Added after a real report: an item's "Add to eBay Batch" card was still
sitting in #awaiting-listing, fully clickable, after the item had already
moved on to pending_ebay_upload - clicking it just replied "already moved
on". The delete of the old card had silently failed and the failure was
swallowed with a bare `except ...: pass`, with no fallback and nothing
printed to diagnose it. _clear_old_card fixes both: it logs the real
exception, and falls back to stripping the card's buttons so a card that
can't be deleted at least can't be clicked again.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import aiohttp
import pytest

from cogs.item_flow import ItemFlow


class _FakeMessage:
    def __init__(self, delete_error=None, edit_error=None):
        self.deleted = False
        self.edited_view = "unset"
        self._delete_error = delete_error
        self._edit_error = edit_error

    async def delete(self):
        if self._delete_error:
            raise self._delete_error
        self.deleted = True

    async def edit(self, view=None, **kwargs):
        if self._edit_error:
            raise self._edit_error
        self.edited_view = view


@pytest.fixture
def cog():
    return ItemFlow(bot=None)


def test_clear_old_card_deletes_successfully(cog):
    message = _FakeMessage()
    asyncio.run(cog._clear_old_card(message, item_id=1, context="test"))

    assert message.deleted is True
    assert message.edited_view == "unset"  # edit never attempted once delete works


def test_clear_old_card_falls_back_to_stripping_buttons_when_delete_fails(cog, capsys):
    message = _FakeMessage(delete_error=aiohttp.ClientError("boom"))

    asyncio.run(cog._clear_old_card(message, item_id=7, context="test context"))

    assert message.deleted is False
    assert message.edited_view is None  # buttons stripped
    printed = capsys.readouterr().out
    assert "item 7" in printed
    assert "test context" in printed
    assert "boom" in printed


def test_clear_old_card_never_raises_even_if_both_delete_and_edit_fail(cog, capsys):
    message = _FakeMessage(
        delete_error=aiohttp.ClientError("delete failed"),
        edit_error=asyncio.TimeoutError(),
    )

    # Must not raise - this is called after the DB status is already updated
    # and the item's new card already posted, so nothing here should be able
    # to blow up the caller.
    asyncio.run(cog._clear_old_card(message, item_id=9, context="test context"))

    printed = capsys.readouterr().out
    assert "item 9" in printed
