"""
Without a live eBay API, the only real "does this category actually
perform for us" signal this bot has is whether an item in it has ever
been CONFIRMED live (database.record_ebay_category_confirmed) - not just
picked during Queue Review (record_ebay_category_use). EbayCategoryPickView
(cogs/item_flow.py)'s quick-pick dropdown must sort confirmed categories
first regardless of raw pick count, and mark them, so the team naturally
converges on proven-working categories over time. (ID validity itself is
no longer this signal's job - ebay_taxonomy.py's real eBay data already
guarantees every ID it can return is a real, listable leaf category.)
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord

import database as db
import ebay_taxonomy
from cogs.item_flow import EbayCategoryPickView


def test_confirmed_category_outranks_a_merely_popular_one(fresh_db, monkeypatch):
    monkeypatch.setattr(ebay_taxonomy, "_BY_ID", {
        "111": "Test > Popular But Unconfirmed",
        "999": "Test > Rarely Picked But Confirmed",
    })

    fresh_db.record_ebay_category_use("111")
    fresh_db.record_ebay_category_use("111")
    fresh_db.record_ebay_category_use("111")
    fresh_db.record_ebay_category_use("999")
    fresh_db.record_ebay_category_confirmed("999")

    view = EbayCategoryPickView(item_id=1, condition_id="1500")
    labels = [option.label for option in view.select.options]

    assert labels[0].startswith("✅"), labels
    assert "Rarely Picked But Confirmed" in labels[0]
    assert not labels[1].startswith("✅")


def test_unconfirmed_category_label_has_no_checkmark(fresh_db, monkeypatch):
    monkeypatch.setattr(ebay_taxonomy, "_BY_ID", {"222": "Test > Never Confirmed"})
    fresh_db.record_ebay_category_use("222")

    view = EbayCategoryPickView(item_id=1, condition_id="1500")
    assert view.select.options[0].label == "Never Confirmed (Test)"


def test_stale_id_no_longer_in_taxonomy_is_skipped_not_shown_blank(fresh_db, monkeypatch):
    # A team's usage history could outlive the taxonomy data (a refresh, or
    # a manually-entered id via /ebay retry-item that was never real) -
    # such an id should just be left out of the quick-pick, not shown blank.
    monkeypatch.setattr(ebay_taxonomy, "_BY_ID", {})
    fresh_db.record_ebay_category_use("333")

    view = EbayCategoryPickView(item_id=1, condition_id="1500")
    assert not hasattr(view, "select")
    assert any(isinstance(child, discord.ui.Button) for child in view.children)


def test_no_previously_used_categories_still_offers_search(fresh_db):
    view = EbayCategoryPickView(item_id=1, condition_id="1500")
    assert not hasattr(view, "select")
    assert len(view.children) == 1  # just the search button
