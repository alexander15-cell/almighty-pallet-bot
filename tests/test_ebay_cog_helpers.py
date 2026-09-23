"""cogs/ebay.py's pure helper functions: the specifics-string parser used by
/ebay retry-item."""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

from cogs.ebay import _parse_specifics


def test_parse_specifics_basic():
    assert _parse_specifics("Brand=DeWalt, Voltage=20V") == {"Brand": "DeWalt", "Voltage": "20V"}


def test_parse_specifics_trims_whitespace():
    assert _parse_specifics(" Brand = DeWalt , Voltage = 20V ") == {"Brand": "DeWalt", "Voltage": "20V"}


def test_parse_specifics_rejects_missing_equals():
    with pytest.raises(ValueError):
        _parse_specifics("Brand DeWalt")


def test_parse_specifics_rejects_empty_key():
    with pytest.raises(ValueError):
        _parse_specifics("=DeWalt")


def test_parse_specifics_rejects_blank_input():
    with pytest.raises(ValueError):
        _parse_specifics("   ")
