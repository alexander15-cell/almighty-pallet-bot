"""
Parses a supplier-provided liquidation manifest (uploaded alongside an
invoice in #submit-invoices - see cogs/finance.py) into one row per
PHYSICAL UNIT, which database.upsert_manifest_lot stores as manifest_lines
for Queue Review to later match real items against (see
database.get_unmatched_manifest_lines_for_pallet) and for the proportional
COGS formula to read from (database.get_item_manifest_cost).

Different liquidators format these wildly differently - column names, CSV
vs XLSX, the same SKU repeated across several rows instead of one row with
a summed quantity - so, like pirate_ship_import.py, this SCANS header text
for keywords rather than trusting one fixed schema. Every failure mode
(unreadable file, no recognizable columns, no usable rows) is returned as
{"ok": False, "error": "..."} rather than raised, so the caller can show
the uploader exactly what's wrong and ask them to re-upload a corrected
file instead of silently accepting bad data that would otherwise only
surface much later, as a wrong COGS number at sale time.
"""
import csv
import io

import openpyxl

_QUANTITY_HINTS = ("qty", "quantity")
_PRICE_HINTS = ("retail price", "unit price", "price", "msrp", "value")
_PRODUCT_HINTS = ("product", "description", "item", "name")


def _clean_header(value) -> str:
    return str(value or "").strip().lower()


def _detect_columns(headers: list) -> dict:
    """
    Returns {"sku", "product", "quantity", "retail_price", "total_retail_price"}
    -> column index or None. "Retail Price" (per-unit) is distinguished
    from "Total Retail Price" (a line/grand total) by checking for "total"
    FIRST - a header containing both words is a total, never the per-unit
    price or the real per-row quantity. The first matching column wins for
    each field, so a file with several plausible headers (e.g. both "Item"
    and "Description") doesn't get reassigned partway through.
    """
    columns = {"sku": None, "product": None, "quantity": None, "retail_price": None, "total_retail_price": None}
    for idx, raw in enumerate(headers):
        header = _clean_header(raw)
        if not header:
            continue
        if columns["sku"] is None and "sku" in header:
            columns["sku"] = idx
            continue
        is_total = "total" in header
        if is_total:
            if columns["total_retail_price"] is None and any(hint in header for hint in _PRICE_HINTS):
                columns["total_retail_price"] = idx
            continue
        if columns["quantity"] is None and any(hint in header for hint in _QUANTITY_HINTS):
            columns["quantity"] = idx
            continue
        if columns["retail_price"] is None and any(hint in header for hint in _PRICE_HINTS):
            columns["retail_price"] = idx
            continue
        if columns["product"] is None and any(hint in header for hint in _PRODUCT_HINTS):
            columns["product"] = idx
            continue
    return columns


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


def _looks_like_quantity(value):
    amount = _looks_like_money(value)
    if amount is None:
        return None
    return int(round(amount))


def _rows_from_csv(file_bytes: bytes) -> list:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    return list(csv.reader(io.StringIO(text)))


def _rows_from_xlsx(file_bytes: bytes) -> list:
    workbook = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    return [["" if cell is None else cell for cell in row] for row in sheet.iter_rows(values_only=True)]


def _cell(row: list, idx):
    if idx is None or idx >= len(row):
        return None
    return row[idx]


def parse_manifest(file_bytes: bytes, filename: str) -> dict:
    """
    Returns, on success:
      {"ok": True, "lines": [{"sku", "product", "retail_price"}, ...],
       "total_units": int, "total_retail_value": float,
       "columns_used": {field: header_text}, "warnings": [str, ...]}
    lines has one entry per PHYSICAL UNIT (a row's quantity already
    exploded out), suitable for database.upsert_manifest_lot as-is.

    On failure: {"ok": False, "error": "<what's wrong, for the uploader>"}.
    warnings never blocks acceptance (e.g. a row with a blank price that
    got skipped) - only used to flag something worth a second look.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
    try:
        if ext in ("xlsx", "xlsm"):
            rows = _rows_from_xlsx(file_bytes)
        elif ext == "csv":
            rows = _rows_from_csv(file_bytes)
        else:
            return {"ok": False, "error": f'Unrecognized manifest file type "{filename}" - upload a .csv or .xlsx file.'}
    except Exception as e:
        return {"ok": False, "error": f'Couldn\'t open "{filename}" - it may be corrupted or not a real {ext or "spreadsheet"} file ({e}).'}

    rows = [row for row in rows if any(str(cell).strip() for cell in row if cell is not None)]
    if not rows:
        return {"ok": False, "error": "That file looks empty - no rows were found."}

    header, data_rows = rows[0], rows[1:]
    columns = _detect_columns(header)
    missing = [field for field in ("sku", "retail_price") if columns[field] is None]
    if missing:
        readable_header = ", ".join(str(h).strip() for h in header if str(h).strip()) or "(no header text)"
        wanted = " or ".join(f'"{field.replace("_", " ")}"' for field in missing)
        return {
            "ok": False,
            "error": (
                f"Couldn't find a column that looks like {wanted} in this file's header row "
                f"({readable_header}). Make sure the first row has real column headers, and that "
                f"the relevant column name includes the word \"SKU\" and/or \"price\"."
            ),
        }

    lines, warnings = [], []
    footer_total = None
    for row_number, row in enumerate(data_rows, start=2):
        sku_raw = _cell(row, columns["sku"])
        product_raw = _cell(row, columns["product"])
        sku = str(sku_raw).strip() if sku_raw not in (None, "") else ""
        product = str(product_raw).strip() if product_raw not in (None, "") else ""

        # Totals/footer row heuristic: blank Product AND blank SKU - a real
        # footer's "quantity"/"retail price" cells are grand totals, not
        # one item's values, and must never be parsed as a line item. Its
        # declared total (if this file has one) is instead used below as a
        # sanity cross-check against what was actually parsed.
        if not sku and not product:
            total_cell = _cell(row, columns["total_retail_price"])
            candidate = _looks_like_money(total_cell)
            if candidate is not None:
                footer_total = candidate
            continue

        retail_price = _looks_like_money(_cell(row, columns["retail_price"]))
        if retail_price is None:
            warnings.append(f"Row {row_number}: couldn't read a retail price - skipped.")
            continue

        quantity = _looks_like_quantity(_cell(row, columns["quantity"])) if columns["quantity"] is not None else 1
        if not quantity or quantity < 1:
            quantity = 1

        for _ in range(quantity):
            lines.append({"sku": sku or None, "product": product or None, "retail_price": retail_price})

    if not lines:
        return {"ok": False, "error": "No usable item rows were found - every row was blank or missing a retail price."}

    total_retail_value = round(sum(line["retail_price"] for line in lines), 2)
    if footer_total is not None and abs(footer_total - total_retail_value) > 0.01:
        warnings.append(
            f"This file's own declared total (${footer_total:,.2f}) doesn't match the sum of the "
            f"parsed line items (${total_retail_value:,.2f}) - double check the column mapping "
            f"below before this is used for COGS."
        )

    return {
        "ok": True,
        "lines": lines,
        "total_units": len(lines),
        "total_retail_value": total_retail_value,
        "columns_used": {field: header[idx] for field, idx in columns.items() if idx is not None},
        "warnings": warnings,
    }
