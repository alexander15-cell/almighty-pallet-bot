"""
The eBay category search flow in cogs/item_flow.py (EbayCategoryPickView's
"Search categories" button -> EbayCategorySearchModal -> results select),
which replaced the old static ~30-entry EBAY_CATEGORIES dropdown now that
category selection searches eBay's full ~18,000-leaf official taxonomy
(ebay_taxonomy.py) instead. Also _category_option_label, the helper that
keeps a category's actual leaf name readable when Discord's 100-char
SelectOption label cap would otherwise truncate a long breadcrumb path
from the wrong end.
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import discord

from cogs.item_flow import EbayCategorySearchResultsView, _category_option_label


def test_category_option_label_short_path_unchanged():
    assert _category_option_label("Home & Garden > Ceiling Fans") == "Ceiling Fans (Home & Garden)"


def test_category_option_label_no_ancestors():
    assert _category_option_label("Antiques") == "Antiques"


def test_category_option_label_prefix_goes_on_leaf_not_ancestors():
    # A confirmed-category checkmark must stay attached to the visible leaf
    # name, not get buried inside the truncated ancestor parenthetical.
    label = _category_option_label("Home & Garden > Ceiling Fans", prefix="✅ ")
    assert label.startswith("✅ Ceiling Fans")


def test_category_option_label_truncates_long_ancestors_not_the_leaf():
    long_path = (
        "Business & Industrial > CNC, Metalworking & Manufacturing > Metalworking Equipment > "
        "Drilling & Tapping Machines > A Reasonably Long Leaf Category Name Here"
    )
    label = _category_option_label(long_path)
    assert len(label) <= 100
    assert label.startswith("A Reasonably Long Leaf Category Name Here")


def test_search_results_view_builds_options_from_matches():
    matches = [("176937", "Home & Garden > Lamps, Lighting & Ceiling Fans > Ceiling Fans")]
    view = EbayCategorySearchResultsView(item_id=1, condition_id="1500", matches=matches)
    assert view.select.options[0].value == "176937"
    assert "Ceiling Fans" in view.select.options[0].label


def test_search_results_view_has_search_again_button():
    matches = [("1", "Test > Widget")]
    view = EbayCategorySearchResultsView(item_id=1, condition_id="1500", matches=matches)
    assert any(isinstance(child, discord.ui.Button) for child in view.children)
