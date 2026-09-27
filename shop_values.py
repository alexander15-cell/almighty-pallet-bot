"""Pure input validation for a deliberately approved shop listing.

No Discord, database, filesystem, environment, or network imports. These helpers
never infer quantity, condition, tested status, or approval from another value.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_INTEGER = 2_147_483_647
MAX_PRICE_INPUT = 128
MAX_URL_INPUT = 2048
_ITEM_ID = re.compile(r"[1-9][0-9]{8,14}\Z")
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_ERROR_CODES = frozenset({"invalid_shop_value", "invalid_price", "price_out_of_range", "invalid_ebay_url",
                          "unsupported_ebay_variation", "invalid_quantity"})


class ShopValidationError(ValueError):
    """Fixed diagnostic code only; user-entered values are never echoed."""
    def __init__(self, code="invalid_shop_value"):
        self.code = code if isinstance(code, str) and code in _ERROR_CODES else "invalid_shop_value"
        super().__init__(self.code)


def parse_price_cents(value: str) -> int:
    """Parse a human dollar amount exactly, without float conversion or rounding.

    Examples: ``12``, ``12.3``, ``$12.30``, and outer whitespace. A leading zero
    is required below one dollar. Dollar signs cannot be separated from digits.
    Scientific notation, commas, signs, and fractional cents are not accepted.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PRICE_INPUT:
        raise ShopValidationError("invalid_price")
    text = value.strip()
    match = re.fullmatch(r"\$?([0-9]+)(?:\.([0-9]{1,2}))?", text)
    if match is None:
        raise ShopValidationError("invalid_price")
    dollars, fraction = match.groups()
    # Integer arithmetic preserves the approved decimal amount exactly.
    amount = int(dollars) * 100 + int((fraction or "0").ljust(2, "0"))
    if not 1 <= amount <= MAX_INTEGER:
        raise ShopValidationError("price_out_of_range")
    return amount


def validate_quantity(value: int) -> int:
    """Require an explicit positive whole number of sale units/lots."""
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ShopValidationError("invalid_quantity")
    return value


def _control(value):
    # C0/C1 controls, invisible formatting controls, and lone surrogates are not
    # suitable in pasted purchase links, including percent-decoded components.
    return any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value)


def _path_segment(value):
    if not value or _BAD_PERCENT.search(value):
        raise ShopValidationError("invalid_ebay_url")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except (UnicodeError, ValueError):
        raise ShopValidationError("invalid_ebay_url") from None
    if (decoded in {".", ".."} or "/" in decoded or "\\" in decoded or _control(decoded)
            or _ESCAPE.search(decoded)):
        # Reject encoded separators/traversal and another encoded path layer.
        raise ShopValidationError("invalid_ebay_url")
    return decoded


def ebay_item_id(value: str) -> str:
    """Extract an item ID only from a safe, non-variation eBay US item link.

    Allows HTTPS ebay.com/www.ebay.com, optional default port 443, the ordinary
    /itm/id and /itm/title/id shapes, and one optional trailing slash. Benign
    tracking parameters are discarded by canonical_ebay_url. A nonempty `var`
    selection is rejected because removing it could point at another variation.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_URL_INPUT or _control(value) or "\\" in value:
        raise ShopValidationError("invalid_ebay_url")
    text = value.strip()
    if (not text or any(character.isspace() for character in text) or "#" in text
            or _BAD_PERCENT.search(text)):
        raise ShopValidationError("invalid_ebay_url")
    try:
        parsed = urlsplit(text)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.hostname not in {"ebay.com", "www.ebay.com"}
                or parsed.port not in (None, 443)
                or not re.fullmatch(r"(?:www\.)?ebay\.com(?::443)?", parsed.netloc, re.IGNORECASE)):
            raise ShopValidationError("invalid_ebay_url")
        path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
        parts = path.split("/")
        if len(parts) not in (3, 4) or parts[:2] != ["", "itm"]:
            raise ShopValidationError("invalid_ebay_url")
        for segment in parts[2:]:
            _path_segment(segment)
        # Require a literal ASCII item ID, not an encoded or Unicode lookalike.
        item_id = parts[-1]
        if _ITEM_ID.fullmatch(item_id) is None:
            raise ShopValidationError("invalid_ebay_url")
        query = parse_qsl(parsed.query, keep_blank_values=True, encoding="utf-8", errors="strict", max_num_fields=50)
        for key, item in query:
            if _control(key) or _control(item) or "\\" in key or "\\" in item or _ESCAPE.search(key):
                raise ShopValidationError("invalid_ebay_url")
            if key.strip().casefold() == "var" and item != "":
                raise ShopValidationError("unsupported_ebay_variation")
        return item_id
    except ShopValidationError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise ShopValidationError("invalid_ebay_url") from None


def canonical_ebay_url(value: str) -> str:
    """Return the canonical item-only URL, with no title, query, port or fragment."""
    return "https://www.ebay.com/itm/" + ebay_item_id(value)
