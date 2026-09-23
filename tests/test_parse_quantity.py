"""
item_flow.parse_quantity() - the "3x cordless drill" / "we have 3 of the
same thing" detection that lets one Data Entry submission spawn N
independently-tracked item cards instead of one. See cogs/item_flow.py's
own docstring on the function for the supported phrasings and why the
prefix pattern specifically avoids misreading a dimension like "3 x 5 tarp".
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

from cogs.item_flow import MAX_DATA_ENTRY_QUANTITY, parse_quantity


def test_no_quantity_indicator_defaults_to_one():
    assert parse_quantity("cordless drill, new in box") == (1, "cordless drill, new in box", False)


def test_blank_note_defaults_to_one():
    assert parse_quantity("") == (1, "", False)


def test_x_prefix_strips_and_returns_quantity():
    assert parse_quantity("3x cordless drill, new in box") == (3, "cordless drill, new in box", False)


def test_x_prefix_with_spaces():
    assert parse_quantity("3 x cordless drill") == (3, "cordless drill", False)


def test_x_prefix_case_insensitive():
    assert parse_quantity("3X cordless drill") == (3, "cordless drill", False)


def test_x_prefix_does_not_misread_a_dimension():
    # "3 x 5 tarp" is a size, not a quantity of 3 - the next token after
    # "x " is itself a number, so this must NOT be treated as a quantity.
    quantity, text, clamped = parse_quantity("3 x 5 tarp, blue")
    assert quantity == 1
    assert text == "3 x 5 tarp, blue"


def test_qty_prefix_variants():
    assert parse_quantity("qty 3: cordless drill") == (3, "cordless drill", False)
    assert parse_quantity("qty: 3 cordless drill") == (3, "cordless drill", False)
    assert parse_quantity("QTY 3 cordless drill") == (3, "cordless drill", False)


def test_quantity_prefix_variants():
    assert parse_quantity("quantity 3: cordless drill") == (3, "cordless drill", False)
    assert parse_quantity("quantity: 3 cordless drill") == (3, "cordless drill", False)


def test_of_the_same_phrase_is_detected_but_not_stripped():
    text = "we have 3 of the same thing, cordless drill new in box"
    assert parse_quantity(text) == (3, text, False)


def test_of_these_phrase_is_detected_but_not_stripped():
    text = "found 3 of these, all look identical"
    assert parse_quantity(text) == (3, text, False)


def test_quantity_of_one_is_a_no_op():
    assert parse_quantity("1x cordless drill") == (1, "cordless drill", False)


def test_quantity_is_capped_and_reports_clamping():
    quantity, text, clamped = parse_quantity("300x cordless drill")
    assert quantity == MAX_DATA_ENTRY_QUANTITY
    assert text == "cordless drill"
    assert clamped is True


def test_quantity_within_cap_is_not_clamped():
    quantity, text, clamped = parse_quantity(f"{MAX_DATA_ENTRY_QUANTITY}x cordless drill")
    assert quantity == MAX_DATA_ENTRY_QUANTITY
    assert clamped is False
