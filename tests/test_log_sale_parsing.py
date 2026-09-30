"""
Pure parsing/splitting helpers behind /finance log-sale (cogs/finance.py):
_parse_item_references (the "PalletName#3, OtherPallet#7" bundle syntax),
_split_evenly (rounding a total into per-item shares that sum exactly), and
_parse_cogs_entry (the cost/COGS modal's free-text, combined-or-itemized,
never-a-mix input).
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

from cogs.finance import _parse_item_references, _split_evenly, _parse_cogs_entry


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


# ----------------------------------------------------------- _parse_cogs_entry


def _item(item_id, number):
    return {"id": item_id, "item_number": number}


def test_combined_single_line_splits_evenly_across_items():
    items = [_item(1, 1), _item(2, 2)]
    result = _parse_cogs_entry("cost 10.00, cogs 8.00", ["Pallet A#1", "Pallet A#2"], items)
    assert result[1] == (5.0, 4.0)
    assert result[2] == (5.0, 4.0)


def test_combined_single_line_works_for_one_item_too():
    items = [_item(1, 1)]
    result = _parse_cogs_entry("cost 12.50, cogs 12.50", ["Pallet A#1"], items)
    assert result[1] == (12.5, 12.5)


def test_itemized_lines_match_by_reference_prefix():
    items = [_item(1, 1), _item(2, 2)]
    text = "Pallet A#1: cost 4.50, cogs 4.50\nPallet A#2: cost 6.00, cogs 5.00"
    result = _parse_cogs_entry(text, ["Pallet A#1", "Pallet A#2"], items)
    assert result[1] == (4.5, 4.5)
    assert result[2] == (6.0, 5.0)


def test_itemized_lines_work_in_any_order():
    items = [_item(1, 1), _item(2, 2)]
    text = "Pallet A#2: cost 6.00, cogs 5.00\nPallet A#1: cost 4.50, cogs 4.50"
    result = _parse_cogs_entry(text, ["Pallet A#1", "Pallet A#2"], items)
    assert result[1] == (4.5, 4.5)
    assert result[2] == (6.0, 5.0)


def test_rejects_empty_input():
    with pytest.raises(ValueError, match="Nothing entered"):
        _parse_cogs_entry("   \n  ", ["Pallet A#1"], [_item(1, 1)])


def test_rejects_a_combined_line_it_cant_parse():
    with pytest.raises(ValueError, match="Couldn't read a cost/COGS pair"):
        _parse_cogs_entry("this isn't the right format", ["Pallet A#1"], [_item(1, 1)])


def test_rejects_a_mix_of_wrong_line_count():
    items = [_item(1, 1), _item(2, 2)]
    with pytest.raises(ValueError, match="never a mix"):
        _parse_cogs_entry("Pallet A#1: cost 1, cogs 1", ["Pallet A#1", "Pallet A#2"], items)


def test_rejects_an_itemized_line_that_doesnt_match_any_reference():
    items = [_item(1, 1), _item(2, 2)]
    text = "Pallet A#1: cost 1, cogs 1\nSomewhere Else#9: cost 2, cogs 2"
    with pytest.raises(ValueError, match="Couldn't match line"):
        _parse_cogs_entry(text, ["Pallet A#1", "Pallet A#2"], items)


def test_rejects_an_itemized_line_with_unparseable_amounts():
    items = [_item(1, 1), _item(2, 2)]
    text = "Pallet A#1: cost 1, cogs 1\nPallet A#2: garbage"
    with pytest.raises(ValueError, match="Couldn't read a cost/COGS pair"):
        _parse_cogs_entry(text, ["Pallet A#1", "Pallet A#2"], items)
