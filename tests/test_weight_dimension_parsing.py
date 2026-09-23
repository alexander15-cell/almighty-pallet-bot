"""
The weight/dimension parsing pure helpers behind EbayListingModal (Queue
Review's approval step, item_flow.py) and /ebay retry-item's weight_lb/
dimensions correction params (cogs/ebay.py) - both required (or optional-
but-validated) inputs feeding eBay's Calculated shipping and the Pirate
Ship export, so a bad parse has a real dollar-cost or crash risk downstream,
not just a cosmetic one.
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

from cogs.ebay import _parse_dimensions as ebay_parse_dimensions
from cogs.item_flow import _parse_dimensions as modal_parse_dimensions
from cogs.item_flow import _parse_weight_lb


def test_parse_weight_lb_accepts_positive_number():
    assert _parse_weight_lb("2.5") == 2.5


def test_parse_weight_lb_rejects_non_numeric():
    with pytest.raises(ValueError, match="must be a number"):
        _parse_weight_lb("heavy")


def test_parse_weight_lb_rejects_zero_and_negative():
    with pytest.raises(ValueError, match="greater than 0"):
        _parse_weight_lb("0")
    with pytest.raises(ValueError, match="greater than 0"):
        _parse_weight_lb("-1.5")


def test_modal_parse_dimensions_accepts_lxwxh():
    assert modal_parse_dimensions("12 x 8 x 4") == (12.0, 8.0, 4.0)
    assert modal_parse_dimensions("12x8x4") == (12.0, 8.0, 4.0)
    assert modal_parse_dimensions("12 X 8 × 4") == (12.0, 8.0, 4.0)


def test_modal_parse_dimensions_rejects_wrong_count():
    with pytest.raises(ValueError, match="three numbers"):
        modal_parse_dimensions("12 x 8")


def test_modal_parse_dimensions_rejects_non_numeric():
    with pytest.raises(ValueError, match="must all be numbers"):
        modal_parse_dimensions("big x medium x small")


def test_modal_parse_dimensions_rejects_non_positive():
    with pytest.raises(ValueError, match="greater than 0"):
        modal_parse_dimensions("12 x 0 x 4")


def test_ebay_cog_parse_dimensions_matches_modal_behavior():
    # /ebay retry-item's dimensions param accepts the same "L x W x H"
    # format as EbayListingModal - one format for a reviewer to remember.
    assert ebay_parse_dimensions("12 x 8 x 4") == (12.0, 8.0, 4.0)
    with pytest.raises(ValueError):
        ebay_parse_dimensions("12 x 8")
    with pytest.raises(ValueError):
        ebay_parse_dimensions("12 x 0 x 4")
