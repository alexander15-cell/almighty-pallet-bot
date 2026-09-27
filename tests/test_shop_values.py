"""Cross-platform pure validation tests; no sockets, files, or runtime imports."""
from decimal import Decimal

import pytest

from shop_values import (MAX_INTEGER, ShopValidationError, canonical_ebay_url, ebay_item_id,
                         parse_price_cents, validate_quantity)


@pytest.mark.parametrize(("value", "expected"), [
    ("12", 1200), ("12.3", 1230), ("12.30", 1230), ("$12.30", 1230),
    ("  $12.30  ", 1230), ("0.01", 1), ("00012.05", 1205),
    ("21474836.47", MAX_INTEGER), ("$0.10", 10), ("1.01", 101),
])
def test_price_is_exact_integer_cents(value, expected):
    assert parse_price_cents(value) == expected
    assert type(parse_price_cents(value)) is int


@pytest.mark.parametrize("value", [
    None, True, False, 12, 12.30, Decimal("12.30"), b"12.30", [], {}, "", "  ",
    ".50", "12.", "$ 12", "$$12", "12$", "+12", "-12", "1e3", "1E-2", "NaN", "Infinity",
    "1,000.00", "1_000.00", "0x12", "12.001", "0.001", "0.010", "12/2", "1 2", "１２.３０", "١٢.٣٠",
    "12\x001", "12\n30", "\u200b12", "0" * 129,
])
def test_invalid_price_format_rejected(value):
    with pytest.raises(ShopValidationError) as failure:
        parse_price_cents(value)
    assert failure.value.code == "invalid_price"


@pytest.mark.parametrize("value", ["0", "$0.00", "000.00", "21474836.48", "99999999999999999999999999999"])
def test_price_bounds_rejected_without_rounding(value):
    with pytest.raises(ShopValidationError) as failure:
        parse_price_cents(value)
    assert failure.value.code == "price_out_of_range"


@pytest.mark.parametrize("value", [1, 2, 17, MAX_INTEGER])
def test_quantity_is_explicit_whole_sale_units(value):
    assert validate_quantity(value) == value


@pytest.mark.parametrize("value", [None, True, False, 0, -1, MAX_INTEGER + 1, "1", 1.0, Decimal("1"), [], {}])
def test_quantity_never_coerces_or_infers(value):
    with pytest.raises(ShopValidationError) as failure:
        validate_quantity(value)
    assert failure.value.code == "invalid_quantity"


@pytest.mark.parametrize("value", [
    "https://ebay.com/itm/123456789012",
    "https://www.ebay.com/itm/123456789012",
    "HTTPS://WWW.EBAY.COM/itm/123456789012",
    "  https://www.ebay.com/itm/123456789012  ",
    "https://www.ebay.com:443/itm/123456789012",
    "https://ebay.com/itm/123456789012/",
    "https://www.ebay.com/itm/A-Product-Title/123456789012",
    "https://www.ebay.com/itm/A%20Product%20Title/123456789012/",
    "https://www.ebay.com/itm/Caf%C3%A9/123456789012",
    "https://www.ebay.com/itm/123456789012?mkcid=1&mkevt=1&campaign=Summer+Sale",
    "https://www.ebay.com/itm/123456789012?var=",
    "https://www.ebay.com/itm/123456789012?var",
    "https://www.ebay.com/itm/123456789012?tracking=https%3A%2F%2Fexample.com%2Ftracking",
])
def test_allowed_links_become_one_canonical_item_url(value):
    assert ebay_item_id(value) == "123456789012"
    assert canonical_ebay_url(value) == "https://www.ebay.com/itm/123456789012"


@pytest.mark.parametrize("item_id", ["123456789", "123456789012345"])
def test_item_id_length_boundaries(item_id):
    assert ebay_item_id("https://www.ebay.com/itm/" + item_id) == item_id


@pytest.mark.parametrize("value", [
    None, True, 123, b"https://ebay.com/itm/123456789012", "", " ",
    "http://www.ebay.com/itm/123456789012", "//www.ebay.com/itm/123456789012",
    "https://ebay.co.uk/itm/123456789012", "https://ebay.com.evil.example/itm/123456789012",
    "https://evil.example/itm/123456789012", "https://www.ebay.com./itm/123456789012",
    "https://m.ebay.com/itm/123456789012", "https://ebay.us/abc123", "https://ebay.to/abc123",
    "https://ebay.com@evil.example/itm/123456789012", "https://evil.example@ebay.com/itm/123456789012",
    "https://name:password@www.ebay.com/itm/123456789012", "https://@ebay.com/itm/123456789012",
    "https://www.ebay.com:80/itm/123456789012", "https://www.ebay.com:444/itm/123456789012",
    "https://www.ebay.com:0443/itm/123456789012", "https://www.ebay.com:/itm/123456789012",
    "https://www.ebay.com:bad/itm/123456789012", "https://www.ebay.com:99999/itm/123456789012",
    "https://www.ebay.com\\@evil.example/itm/123456789012", "https://www.ebay.com/itm/123456789012#details",
    "https://www.ebay.com/itm/123456789012#", "https://www.ebay.com/itm/123456789012\n",
    "\thttps://www.ebay.com/itm/123456789012", "https://www.ebay.com/itm/123456789012\x00",
    "https://www.ebay.com/itm/123456789012\x7f", "https://www.ebay.com/itm/123456789012\u200b",
    "https://www.ebay.com/itm/123456789012\u0085", "https://www.ebay.com/itm/123456789012\ud800",
    "https://www.ebay.com/ itm/123456789012", "https://www.ebay.com/ITM/123456789012",
    "https://www.ebay.com/itm/12345678", "https://www.ebay.com/itm/1234567890123456",
    "https://www.ebay.com/itm/012345678901", "https://www.ebay.com/itm/１２３４５６７８９０１２",
    "https://www.ebay.com/itm/123456789012/extra/path", "https://www.ebay.com/itm//123456789012",
    "https://www.ebay.com/itm/123456789012//", "https://www.ebay.com/itm/../123456789012",
    "https://www.ebay.com/itm/./123456789012", "https://www.ebay.com/itm/%2e%2e/123456789012",
    "https://www.ebay.com/itm/%2E/123456789012", "https://www.ebay.com/itm/foo%2fbar/123456789012",
    "https://www.ebay.com/itm/foo%5Cbar/123456789012", "https://www.ebay.com/itm/foo%252fbar/123456789012",
    "https://www.ebay.com/itm/%252e%252e/123456789012", "https://www.ebay.com/itm/a%00b/123456789012",
    "https://www.ebay.com/itm/a%0Ab/123456789012", "https://www.ebay.com/itm/a%E2%80%8Bb/123456789012",
    "https://www.ebay.com/itm/Bad%/123456789012", "https://www.ebay.com/itm/Bad%2/123456789012",
    "https://www.ebay.com/itm/Bad%GG/123456789012", "https://www.ebay.com/itm/Bad%FF/123456789012",
    "https://www.ebay.com/itm/%31%32%33%34%35%36%37%38%39%30%31%32",
    "https://www.ebay.com/itm/123456789012?x=%", "https://www.ebay.com/itm/123456789012?x=%FF",
    "https://www.ebay.com/itm/123456789012?x=%0A", "https://www.ebay.com/itm/123456789012?x=%5C",
    "https://www.ebay.com/itm/123456789012?%2576ar=123456789012",
    "https://www.ebay.com/itm/123456789012?" + "x=1&" * 51,
    "https://www.ebay.com/itm/123456789012?x=" + "a" * 2048,
])
def test_unsafe_or_unsupported_links_rejected(value):
    with pytest.raises(ShopValidationError) as failure:
        canonical_ebay_url(value)
    assert failure.value.code == "invalid_ebay_url"


@pytest.mark.parametrize("query", ["var=123456789012", "var=0", "var=%20", "VAR=123", "v%61r=123",
                                    "var=&var=123", "tracking=1&var=123", "var=none", "%20var%20=123"])
def test_variation_selection_never_silently_dropped(query):
    with pytest.raises(ShopValidationError) as failure:
        ebay_item_id("https://www.ebay.com/itm/123456789012?" + query)
    assert failure.value.code == "unsupported_ebay_variation"


def test_fixed_error_codes_never_echo_the_raw_input():
    private_input = "DO_NOT_ECHO_PRIVATE_INPUT"
    with pytest.raises(ShopValidationError) as failure:
        parse_price_cents(private_input)
    assert private_input not in str(failure.value)
    assert str(ShopValidationError(private_input)) == "invalid_shop_value"


def test_canonicalization_is_idempotent():
    first = canonical_ebay_url("https://ebay.com/itm/Some-Title/123456789012/?mkcid=1")
    assert canonical_ebay_url(first) == first
