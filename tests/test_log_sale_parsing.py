"""
Pure parsing/splitting helpers behind /finance log-sale (cogs/finance.py):
_parse_item_references (the "PalletName#3, OtherPallet#7" bundle syntax) and
_split_evenly (rounding a total into per-item shares that sum exactly).
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

from cogs.finance import _parse_item_references, _split_evenly


# --------------------------------------------------------- _parse_item_references


def test_parses_a_single_item_reference(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "Widget", [], 1)

    resolved, references, errors = _parse_item_references(f"Pallet A#{fresh_db.get_item(item_id)['item_number']}")

    assert errors == []
    assert len(resolved) == 1
    assert resolved[0]["id"] == item_id
    assert references == [f"Pallet A#{fresh_db.get_item(item_id)['item_number']}"]


def test_parses_a_bundle_spanning_different_pallets(fresh_db):
    p1 = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    p2 = fresh_db.create_pallet("Pallet B", category_id=2, created_by=1)
    item1 = fresh_db.create_item(p1, "Widget", [], 1)
    item2 = fresh_db.create_item(p2, "Gadget", [], 1)

    resolved, references, errors = _parse_item_references("Pallet A#1, Pallet B#1")

    assert errors == []
    assert {item["id"] for item in resolved} == {item1, item2}


def test_rejects_a_reference_missing_the_hash(fresh_db):
    resolved, references, errors = _parse_item_references("Pallet A 3")
    assert resolved == []
    assert "expected the format" in errors[0]


def test_rejects_a_nonexistent_pallet(fresh_db):
    resolved, references, errors = _parse_item_references("Nonexistent Pallet#1")
    assert resolved == []
    assert "no pallet named" in errors[0]


def test_rejects_a_nonexistent_item_number(fresh_db):
    fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    resolved, references, errors = _parse_item_references("Pallet A#99")
    assert resolved == []
    assert "no item #99" in errors[0]


def test_rejects_a_non_numeric_item_number(fresh_db):
    fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    resolved, references, errors = _parse_item_references("Pallet A#abc")
    assert resolved == []
    assert "whole number" in errors[0]


def test_blank_segments_are_skipped_not_errors(fresh_db):
    pallet_id = fresh_db.create_pallet("Pallet A", category_id=1, created_by=1)
    fresh_db.create_item(pallet_id, "Widget", [], 1)
    resolved, references, errors = _parse_item_references("Pallet A#1, , ")
    assert errors == []
    assert len(resolved) == 1


# --------------------------------------------------------------- _split_evenly


def test_split_evenly_divides_cleanly():
    assert _split_evenly(30.0, 3) == [10.0, 10.0, 10.0]


def test_split_evenly_sums_exactly_despite_rounding():
    shares = _split_evenly(10.0, 3)
    assert round(sum(shares), 2) == 10.0
    assert shares == [3.33, 3.33, 3.34]


def test_split_evenly_single_item_returns_the_whole_amount():
    assert _split_evenly(19.99, 1) == [19.99]
