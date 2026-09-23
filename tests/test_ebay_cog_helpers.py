"""cogs/ebay.py's pure helper functions: the specifics-string parser used by
/ebay retry-item, and the pre-export requirement check that blocks a batch
from being exported (rather than guaranteed to fail on every row) while
eBay's non-negotiable per-row fields (location, shipping) aren't configured."""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import config
from cogs.ebay import _missing_export_requirements, _parse_specifics


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


def test_missing_export_requirements_lists_everything_unset(monkeypatch):
    monkeypatch.setattr(config, "EBAY_ITEM_LOCATION", "")
    monkeypatch.setattr(config, "EBAY_SHIPPING_SERVICE", "")
    monkeypatch.setattr(config, "EBAY_SHIPPING_PACKAGE_TYPE", "")

    missing_names = {name for name, _why in _missing_export_requirements()}
    assert missing_names == {"EBAY_ITEM_LOCATION", "EBAY_SHIPPING_SERVICE", "EBAY_SHIPPING_PACKAGE_TYPE"}


def test_missing_export_requirements_empty_once_all_set(monkeypatch):
    monkeypatch.setattr(config, "EBAY_ITEM_LOCATION", "Columbus, OH")
    monkeypatch.setattr(config, "EBAY_SHIPPING_SERVICE", "USPSPriority")
    monkeypatch.setattr(config, "EBAY_SHIPPING_PACKAGE_TYPE", "PackageThickEnvelope")

    assert _missing_export_requirements() == []
