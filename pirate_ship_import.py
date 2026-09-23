"""
Parses a Pirate Ship order/shipping CSV export (uploaded via
/finance import-pirateship in cogs/finance.py) and finds each row's
shipping cost and its "pallet-<id>-item-<number>" reference - the same
identifier ebay_csv.custom_label() and pirate_ship_csv.order_number()
already embed as their own Custom Label (SKU) / Order Number columns.

No real Pirate Ship export was available to check exact column names
against when this was built (flagged at spec time; the user explicitly
chose "build it now with flexible best-guess matching" over waiting for a
sample), so both the cost and the reference are found by SCANNING the row
rather than trusting one fixed header name:
  - the shipping cost: the first column whose header name looks cost-like
    (contains "cost"/"price"/"rate"/"postage"/"total"/"amount"/"label")
    AND whose value parses as money.
  - the "pallet-<id>-item-<number>" reference: searched for in EVERY cell
    of the row, not just one column, since it could show up as our own
    "Order Number" (re-importing a pirate_ship_csv.py export after Pirate
    Ship processes it) or inside whatever order-reference field Pirate
    Ship's own eBay/label integration happens to pass through.

If a real export's format turns out to look different once a sample is
available, tighten this matching rather than guessing further - don't
widen it speculatively.

Purely a text/regex parser with no database access (matches the existing
ebay_results.py on purpose), so it's easy to test without touching one.
The caller resolves each match's pallet_id/item_number against the
database and performs the actual pallet_costs write - see
cogs/finance.py's import_pirateship command.
"""
import csv
import io
import re

_REFERENCE_PATTERN = re.compile(r"pallet-(\d+)-item-(\d+)", re.IGNORECASE)
_COST_HEADER_HINTS = ("cost", "price", "rate", "postage", "total", "amount", "label")


def _looks_like_money(value):
    if value is None:
        return None
    cleaned = str(value).strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_cost(row: dict):
    for header, value in row.items():
        if header and any(hint in header.lower() for hint in _COST_HEADER_HINTS):
            amount = _looks_like_money(value)
            if amount is not None:
                return amount
    return None


def _find_reference(row: dict):
    for value in row.values():
        if not value:
            continue
        match = _REFERENCE_PATTERN.search(str(value))
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _row_label(row: dict) -> str:
    for value in row.values():
        if value and str(value).strip():
            return str(value).strip()
    return "(blank row)"


def parse_shipping_costs(csv_bytes: bytes) -> dict:
    """
    Returns:
      {
        "matched": [{"pallet_id": int, "item_number": int, "amount": float, "row_label": str}, ...],
        "unmatched": [{"row_label": str, "amount": float | None}, ...],
      }

    A row lands in "unmatched" here if it has no parseable cost or no
    pallet-X-item-Y reference anywhere in it. The caller additionally
    treats a "matched" row as unmatched if its pallet_id/item_number don't
    resolve to a real item (e.g. from a different/older bot install, or a
    typo'd reference) - that lookup needs the database, which this module
    deliberately doesn't touch.
    """
    text = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    matched, unmatched = [], []
    for row in reader:
        if not any((v or "").strip() for v in row.values() if v is not None):
            continue  # skip genuinely blank rows

        amount = _find_cost(row)
        reference = _find_reference(row)
        label = _row_label(row)

        if reference is None or amount is None:
            unmatched.append({"row_label": label, "amount": amount})
            continue

        pallet_id, item_number = reference
        matched.append({"pallet_id": pallet_id, "item_number": item_number, "amount": amount, "row_label": label})

    return {"matched": matched, "unmatched": unmatched}
