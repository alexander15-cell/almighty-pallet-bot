"""
Without eBay API access, the only real "does this category actually work"
signal this bot has is whether an item in it has ever been CONFIRMED live
(database.record_ebay_category_confirmed) - not just picked during Queue
Review (record_ebay_category_use). EbayCategorySelectView (cogs/item_flow.py)
must sort confirmed categories first regardless of raw pick count, and mark
them, so the team naturally converges on proven-working categories over time.
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import config
import database as db
from cogs.item_flow import EbayCategorySelectView


def test_confirmed_category_outranks_a_merely_popular_one(fresh_db, monkeypatch):
    monkeypatch.setitem(config.__dict__, "EBAY_CATEGORIES", {
        "Popular But Unconfirmed": "111",
        "Rarely Picked But Confirmed": "999",
    })

    fresh_db.record_ebay_category_use("111")
    fresh_db.record_ebay_category_use("111")
    fresh_db.record_ebay_category_use("111")
    fresh_db.record_ebay_category_use("999")
    fresh_db.record_ebay_category_confirmed("999")

    view = EbayCategorySelectView(item_id=1, condition_id="1500")
    labels = [option.label for option in view.select.options]

    assert labels[0].startswith("✅"), labels
    assert "Rarely Picked But Confirmed" in labels[0]
    assert not labels[1].startswith("✅")


def test_unconfirmed_category_label_has_no_checkmark(fresh_db, monkeypatch):
    monkeypatch.setitem(config.__dict__, "EBAY_CATEGORIES", {"Never Used": "222"})

    view = EbayCategorySelectView(item_id=1, condition_id="1500")
    assert view.select.options[0].label == "Never Used"
