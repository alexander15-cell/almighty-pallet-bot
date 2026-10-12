"""
ebay_listing_rules.py - the field-level eBay rules shared by the API listing
path and every SKU -> item lookup (moved here, unchanged, from the removed
CSV batch module).
"""
import config
import ebay_listing_rules as rules


def test_custom_label_format():
    assert rules.custom_label({"pallet_id": 3, "item_number": 7}) == "pallet-3-item-7"


def test_parse_custom_label_reverses_custom_label():
    assert rules.parse_custom_label(rules.custom_label({"pallet_id": 3, "item_number": 7})) == (3, 7)


def test_parse_custom_label_rejects_unrelated_formats():
    assert rules.parse_custom_label("SOME-OTHER-SKU-123") is None
    assert rules.parse_custom_label("pallet-3-item-") is None
    assert rules.parse_custom_label("pallet-item-7") is None
    assert rules.parse_custom_label("") is None
    assert rules.parse_custom_label(None) is None


def test_parse_custom_label_tolerates_surrounding_whitespace():
    assert rules.parse_custom_label("  pallet-3-item-7  ") == (3, 7)


def test_condition_falls_back_outside_the_broadly_accepted_set():
    assert rules.resolved_condition_id("1000", "x") == "1000"
    assert rules.resolved_condition_id("1500", "x") == config.EBAY_CONDITION_FALLBACK_ID


def test_infer_type_matches_keywords_or_returns_blank():
    assert rules.infer_type("Modern Chandelier, 5 light") == "Chandelier"
    assert rules.infer_type("Cordless drill") == ""


def test_estimated_weight_uses_category_keywords_then_fallback():
    assert rules.estimated_weight_lb("Home > Lighting > Ceiling Fan") == config.EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB["ceiling fan"]
    assert rules.estimated_weight_lb("Tools > Drills") == config.EBAY_DEFAULT_WEIGHT_FALLBACK_LB
