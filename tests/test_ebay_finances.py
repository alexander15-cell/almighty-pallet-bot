"""
ebay_finances.py (/ebay sync-finances, /ebay net-report) - eBay's real
fees/shipping/payouts pulled into read-only mirror tables and compared
against what the bot has recorded.

The most important guarantee here is the first group of tests: a sync, a
report, and a failed sync all leave every pre-existing table byte-for-byte
identical - this feature only ever ADDS rows to its own four tables. The
rest cover eBay response parsing (field names checked against eBay's
published OpenAPI specs for the Finances and Fulfillment APIs), pagination
and eBay's 36-month window limit, item matching, and the report math.

aiohttp is faked with the same tiny ClientSession pattern as
test_ebay_api.py - no real network calls.
"""
import asyncio
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

import config
import ebay_api
import ebay_finances

MIRROR_TABLES = {"ebay_fin_transactions", "ebay_order_breakdowns", "ebay_order_lines", "ebay_finance_sync_log"}
PAST = "2025-01-01T00:00:00+00:00"


# ------------------------------------------------------------- fakes/helpers

class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responder, calls):
        self._responder = responder
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, auth=None, headers=None, json=None):
        # The only POST is the OAuth token refresh.
        return _FakeResponse(200, {"access_token": "user-token", "expires_in": 7200})

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append({"method": method, "url": url, "headers": headers})
        status, payload = self._responder(url)
        return _FakeResponse(status, payload)


@pytest.fixture(autouse=True)
def _clear_token_cache():
    ebay_api._cached_access_token = None
    ebay_api._cached_expires_at = None
    yield
    ebay_api._cached_access_token = None
    ebay_api._cached_expires_at = None


def _install(monkeypatch, responder):
    calls = []
    monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(responder, calls))
    monkeypatch.setattr(config, "EBAY_APP_ID", "app")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "cert")
    monkeypatch.setattr(config, "EBAY_REFRESH_TOKEN", "refresh")
    monkeypatch.setattr(config, "EBAY_ENVIRONMENT", "production")
    return calls


def _amt(value):
    return {"value": f"{value:.2f}", "currency": "USD"}


def _raw_order(order_id, lines, date="2026-03-01T12:00:00.000Z", shipping=5.0, total=None, fee=None,
               discount=None):
    subtotal = sum(l["cost"] for l in lines)
    pricing = {"priceSubtotal": _amt(subtotal), "deliveryCost": _amt(shipping),
               "total": _amt(total if total is not None else subtotal + shipping - (discount or 0))}
    if discount:
        pricing["priceDiscount"] = _amt(-discount)
    order = {
        "orderId": order_id,
        "creationDate": date,
        "pricingSummary": pricing,
        "buyer": {"username": "should-not-be-stored", "buyerRegistrationAddress": {"fullName": "Jane Buyer"}},
        "lineItems": [
            {
                "lineItemId": l["id"], "sku": l.get("sku"), "legacyItemId": l.get("listing"),
                "title": l.get("title", "Thing"), "quantity": 1, "lineItemCost": _amt(l["cost"]),
                **({"deliveryCost": {"shippingCost": _amt(l["ship"])}} if "ship" in l else {}),
            }
            for l in lines
        ],
    }
    if fee is not None:
        order["totalMarketplaceFee"] = _amt(fee)
    return order


def _raw_txn(txn_id, txn_type, amount, booking, order_id=None, fee=None, line_fees=None, refs=None,
             fee_type=None, date="2026-03-01T12:00:00.000Z"):
    raw = {"transactionId": txn_id, "transactionType": txn_type, "amount": _amt(amount), "bookingEntry": booking,
           "transactionDate": date, "transactionStatus": "PAYOUT", "buyer": {"username": "should-not-be-stored"}}
    if order_id:
        raw["orderId"] = order_id
    if fee is not None:
        raw["totalFeeAmount"] = _amt(fee)
    if line_fees:
        raw["orderLineItems"] = [
            {"lineItemId": lid, "marketplaceFees": [{"feeType": "FINAL_VALUE_FEE", "amount": _amt(v)}]}
            for lid, v in line_fees.items()
        ]
    if refs:
        raw["references"] = [{"referenceType": t, "referenceId": i} for t, i in refs]
    if fee_type:
        raw["feeType"] = fee_type
    return raw


def _make_item(db, pallet_id, created_at=PAST):
    item_id = db.create_item(pallet_id, "thing", [], 1)
    with db.get_conn() as conn:  # test-only: backdate so eBay orders come after the item
        conn.execute("UPDATE items SET created_at = ? WHERE id = ?", (created_at, item_id))
    return db.get_item(item_id)


def _store(db, orders=(), txns=()):
    for raw in orders:
        db.upsert_ebay_order_breakdown(ebay_finances.parse_order_breakdown(raw))
    for raw in txns:
        db.upsert_ebay_fin_transaction(ebay_finances.parse_transaction(raw))


def _snapshot(db, exclude=MIRROR_TABLES):
    """Every row of every table (sorted), excluding the mirror tables."""
    with db.get_conn() as conn:
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        return {
            t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall())
            for t in tables if t not in exclude
        }


def _populated(db):
    """A database with real-looking data in every existing financial table."""
    pallet_id = db.create_pallet("Pallet-2026-001", category_id=100, created_by=1)
    db.set_pallet_cost(pallet_id, 200.0, actor_id=1)
    item = _make_item(db, pallet_id)
    other = _make_item(db, pallet_id)
    db.save_ebay_listing_data(item["id"], "Drill", "12345", "1000", 20.0, {"Brand": "X"})
    db.set_ebay_item_id(item["id"], "110000000001")
    sale_id = db.create_sale("eBay", "2026-03-01", 25.0, False, created_by=1)
    db.add_sale_item(sale_id, item["id"], 25.0)
    db.record_item_sale(item["id"], 25.0, "eBay", actor_id=1)
    db.record_item_sale(other["id"], 40.0, "In Person", actor_id=1)
    db.record_expense(pallet_id, 3.0, "tape", actor_id=1)
    db.add_pallet_cost(pallet_id, "shipping", 12.0, "pirateship", description="freight")
    return pallet_id, db.get_item(item["id"]), db.get_item(other["id"])


def _sync_responder(orders, txns):
    def responder(url):
        if "/sell/fulfillment/v1/order" in url:
            return 200, {"orders": orders, "total": len(orders)}
        if "/sell/finances/v1/transaction" in url:
            return 200, {"transactions": txns, "total": len(txns)}
        raise AssertionError(f"unexpected URL {url}")
    return responder


# ------------------------------------------- nothing existing ever changes

def test_sync_never_changes_any_existing_table(fresh_db, monkeypatch):
    pallet_id, item, _ = _populated(fresh_db)
    before = _snapshot(fresh_db)
    sku = f"pallet-{pallet_id}-item-{item['item_number']}"
    _install(monkeypatch, _sync_responder(
        [_raw_order("O-1", [{"id": "L-1", "sku": sku, "cost": 20.0}], fee=3.25)],
        [_raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25, line_fees={"L-1": 3.25})],
    ))

    result = asyncio.run(ebay_finances.sync(1825, actor_id=1))

    assert result == {**result, "transactions": 1, "orders": 1}
    assert _snapshot(fresh_db) == before
    assert fresh_db.get_item(item["id"])["sale_price"] == 25.0


def test_resync_is_idempotent_and_still_changes_nothing_existing(fresh_db, monkeypatch):
    _populated(fresh_db)
    before = _snapshot(fresh_db)
    _install(monkeypatch, _sync_responder(
        [_raw_order("O-1", [{"id": "L-1", "sku": "x", "cost": 20.0}])],
        [_raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1")],
    ))
    asyncio.run(ebay_finances.sync(30))
    asyncio.run(ebay_finances.sync(30))

    with fresh_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM ebay_fin_transactions").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM ebay_order_lines").fetchone()["n"] == 1
    assert _snapshot(fresh_db) == before


def test_reports_are_pure_reads(fresh_db):
    pallet_id, item, _ = _populated(fresh_db)
    _store(fresh_db,
           orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}])],
           txns=[_raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25)])
    before = _snapshot(fresh_db, exclude=set())

    ebay_finances.build_pallet_report(pallet_id)
    ebay_finances.build_overview()

    assert _snapshot(fresh_db, exclude=set()) == before


def test_failed_sync_changes_nothing_existing_and_logs_the_error(fresh_db, monkeypatch):
    _populated(fresh_db)
    before = _snapshot(fresh_db)

    def responder(url):
        if "/order" in url:
            return 200, {"orders": [_raw_order("O-1", [{"id": "L-1", "sku": "x", "cost": 5.0}])], "total": 1}
        return 403, {"errors": [{"errorId": 1100, "message": "Insufficient permissions"}]}
    _install(monkeypatch, responder)

    with pytest.raises(ebay_api.EbayAPIError) as exc:
        asyncio.run(ebay_finances.sync(30))

    assert "sell.finances" in str(exc.value)  # tells the user how to fix it
    assert _snapshot(fresh_db) == before
    assert fresh_db.get_last_successful_ebay_finance_sync() is None
    with fresh_db.get_conn() as conn:
        log_row = conn.execute("SELECT * FROM ebay_finance_sync_log").fetchone()
        assert log_row["error"] and log_row["order_count"] == 1
        # The orders pulled before the failure are kept - they're still accurate.
        assert conn.execute("SELECT COUNT(*) AS n FROM ebay_order_breakdowns").fetchone()["n"] == 1


def test_pallet_delete_and_db_wipe_still_work_with_mirror_rows_present(fresh_db):
    pallet_id, item, _ = _populated(fresh_db)
    _store(fresh_db,
           orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}])],
           txns=[_raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1")])

    fresh_db.delete_pallet_permanently(pallet_id)  # no foreign keys to trip over
    assert fresh_db.get_pallet(pallet_id) is None
    fresh_db.wipe_database()
    # eBay-account-level mirror data survives a wipe, like ebay_seen_orders...
    assert len(fresh_db.get_all_ebay_fin_transactions()) == 1
    # ...but can't attach to a NEW pallet that reuses the old ID, because
    # the new item was created after the eBay order.
    new_pallet = fresh_db.create_pallet("Fresh", category_id=5, created_by=1)
    assert new_pallet == pallet_id
    fresh_db.create_item(new_pallet, "new thing", [], 1)
    rows = ebay_finances.build_line_rows()["lines"]
    assert rows[0]["item"] is None


# ------------------------------------------------------------------ parsing

def test_parse_transaction_reads_money_fields_and_drops_buyer():
    raw = _raw_txn("T-9", "SALE", 21.75, "CREDIT", order_id="O-9", fee=3.25, line_fees={"L-1": 2.0, "L-2": 1.25},
                   refs=[("ORDER_ID", "O-9")])
    raw["totalFeeBasisAmount"] = _amt(25.0)
    raw["ebayCollectedTaxAmount"] = _amt(1.80)

    txn = ebay_finances.parse_transaction(raw)

    assert txn["amount"] == 21.75 and txn["booking_entry"] == "CREDIT"
    assert txn["total_fee_amount"] == 3.25 and txn["total_fee_basis_amount"] == 25.0
    assert txn["ebay_collected_tax"] == 1.80
    assert json.loads(txn["line_fees_json"]) == {"L-1": 2.0, "L-2": 1.25}
    assert json.loads(txn["references_json"]) == [{"type": "ORDER_ID", "id": "O-9"}]
    assert "should-not-be-stored" not in json.dumps(txn)


def test_parse_transaction_accepts_ebays_alternate_tax_field_spelling():
    raw = _raw_txn("T-1", "SALE", 10.0, "CREDIT")
    raw["eBayCollectedTaxAmount"] = _amt(0.70)
    assert ebay_finances.parse_transaction(raw)["ebay_collected_tax"] == 0.70


def test_parse_transaction_skips_rows_without_id_or_type():
    assert ebay_finances.parse_transaction({"transactionType": "SALE"}) is None
    assert ebay_finances.parse_transaction({"transactionId": "x"}) is None


def test_parse_order_breakdown_keeps_money_only():
    raw = _raw_order("O-1", [{"id": "L-1", "sku": "pallet-1-item-2", "listing": "1100", "cost": 20.0, "ship": 5.0}],
                     fee=3.0, discount=2.0)

    order = ebay_finances.parse_order_breakdown(raw)

    assert order["price_subtotal"] == 20.0 and order["delivery_cost"] == 5.0
    assert order["price_discount"] == 2.0  # stored positive
    assert order["order_total"] == 23.0 and order["total_marketplace_fee"] == 3.0
    assert order["lines"] == [{
        "line_item_id": "L-1", "sku": "pallet-1-item-2", "legacy_item_id": "1100", "title": "Thing",
        "quantity": 1, "line_item_cost": 20.0, "shipping_cost": 5.0,
    }]
    assert "Jane Buyer" not in json.dumps(order) and "should-not-be-stored" not in json.dumps(order)


# ------------------------------------------------------ fetching / windows

def test_fetch_transactions_uses_apiz_host_windows_and_pagination(monkeypatch):
    pages = {0: [_raw_txn(f"T-{i}", "SALE", 1.0, "CREDIT") for i in range(1000)],
             1000: [_raw_txn("T-last", "SALE", 1.0, "CREDIT")]}

    def responder(url):
        offset = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
        return 200, {"transactions": pages.get(offset, []), "total": 1001}
    calls = _install(monkeypatch, responder)

    until = datetime(2026, 10, 1, tzinfo=timezone.utc)
    since = until - timedelta(days=800)  # needs three 365-day windows
    result = asyncio.run(ebay_finances.fetch_transactions(since, until))

    assert len(result) == 1001 * 3
    assert all(c["url"].startswith("https://apiz.ebay.com/sell/finances/v1/transaction?") for c in calls)
    filters = sorted({urllib.parse.parse_qs(urllib.parse.urlparse(c["url"]).query)["filter"][0] for c in calls})
    assert len(filters) == 3
    for f in filters:
        start, end = f[len("transactionDate:["):-1].split("..")
        span = datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))
        assert span <= timedelta(days=365)  # well inside eBay's 36-month limit
    assert all(c["headers"]["X-EBAY-C-MARKETPLACE-ID"] == config.EBAY_MARKETPLACE_ID for c in calls)


def test_fetch_orders_paginates(monkeypatch):
    def responder(url):
        offset = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["offset"][0])
        page = [_raw_order(f"O-{offset + i}", [{"id": f"L-{offset + i}", "cost": 1.0}]) for i in range(min(200, 250 - offset))]
        return 200, {"orders": page, "total": 250}
    calls = _install(monkeypatch, responder)

    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    result = asyncio.run(ebay_finances.fetch_orders(now - timedelta(days=30), now))

    assert len(result) == 250 and len(calls) == 2
    assert calls[0]["url"].startswith("https://api.ebay.com/sell/fulfillment/v1/order?")


def test_sync_clamps_to_ebays_history_limits(fresh_db, monkeypatch):
    calls = _install(monkeypatch, _sync_responder([], []))
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)

    result = asyncio.run(ebay_finances.sync(10000, now=now))

    assert now - result["transactions_from"] < timedelta(days=5 * 365)
    assert now - result["orders_from"] < timedelta(days=2 * 365)
    assert fresh_db.get_last_successful_ebay_finance_sync()["error"] is None


# ----------------------------------------------------------------- matching

def test_match_by_sku_then_listing_id_only_for_items_created_before_the_order(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    by_sku = _make_item(fresh_db, pallet_id)
    by_listing = _make_item(fresh_db, pallet_id)
    fresh_db.save_ebay_listing_data(by_listing["id"], "T", "1", "1000", 5.0, {})
    fresh_db.set_ebay_item_id(by_listing["id"], "999")
    too_new = _make_item(fresh_db, pallet_id, created_at="2026-06-01T00:00:00+00:00")
    date = "2026-03-01T12:00:00.000Z"

    assert ebay_finances.match_line_to_item({"sku": f"pallet-{pallet_id}-item-1", "order_date": date})["id"] == by_sku["id"]
    assert ebay_finances.match_line_to_item({"sku": "not-ours", "legacy_item_id": "999", "order_date": date})["id"] == by_listing["id"]
    assert ebay_finances.match_line_to_item({"sku": f"pallet-{pallet_id}-item-3", "order_date": date}) is None
    assert ebay_finances.match_line_to_item({"sku": None, "order_date": date}) is None


# ------------------------------------------------------------- report math

def test_single_item_report_shows_fees_labels_ads_and_shipping_in_revenue(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 10.0, actor_id=1)
    item = _make_item(fresh_db, pallet_id)
    fresh_db.record_item_sale(item["id"], 25.0, "eBay", actor_id=1)  # order total, incl. $5 shipping
    _store(
        fresh_db,
        orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "listing": "L1100",
                                     "cost": 20.0, "ship": 5.0}], fee=3.25)],
        txns=[
            _raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25, line_fees={"L-1": 3.25}),
            _raw_txn("T-2", "SHIPPING_LABEL", 4.10, "DEBIT", order_id="O-1"),
            _raw_txn("T-3", "NON_SALE_CHARGE", 1.00, "DEBIT", refs=[("ITEM_ID", "L1100")], fee_type="AD_FEE"),
        ],
    )

    report = ebay_finances.build_pallet_report(pallet_id)

    assert report["totals"] == {
        "item_price": 20.0, "buyer_shipping": 5.0, "ebay_fees": 3.25, "refunds": 0.0,
        "shipping_labels": 4.10, "ad_fees": 1.00, "net_payout": 16.65,
    }
    assert report["recorded_for_matched"] == 25.0
    assert [e["item"]["id"] for e in report["shipping_in_revenue"]] == [item["id"]]
    # Card profit is 25 - 10 = 15; swapping in eBay's net gives 16.65 - 10.
    assert report["financials"]["profit_so_far"] == 15.0
    assert report["profit_with_ebay_net"] == 6.65
    assert report["missing"] == [] and report["any_estimates"] is False


def test_multi_line_order_is_split_by_item_price(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    a, b = _make_item(fresh_db, pallet_id), _make_item(fresh_db, pallet_id)
    _store(
        fresh_db,
        orders=[_raw_order("O-1", [{"id": "L-a", "sku": f"pallet-{pallet_id}-item-1", "cost": 30.0},
                                    {"id": "L-b", "sku": f"pallet-{pallet_id}-item-2", "cost": 10.0}], shipping=8.0)],
        txns=[_raw_txn("T-1", "SALE", 42.0, "CREDIT", order_id="O-1", fee=6.0, line_fees={"L-a": 4.5, "L-b": 1.5})],
    )

    matched = {e["item"]["id"]: e for e in ebay_finances.build_pallet_report(pallet_id)["matched"]}

    assert matched[a["id"]]["buyer_shipping"] == 6.0 and matched[b["id"]]["buyer_shipping"] == 2.0
    assert matched[a["id"]]["ebay_fees"] == 4.5 and matched[b["id"]]["ebay_fees"] == 1.5
    assert matched[a["id"]]["net_payout"] == 31.5 and matched[b["id"]]["net_payout"] == 10.5


def test_refund_reduces_net_using_ebays_own_signed_amounts(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    _make_item(fresh_db, pallet_id)
    _store(
        fresh_db,
        orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}])],
        txns=[_raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25),
              _raw_txn("T-2", "REFUND", 21.45, "DEBIT", order_id="O-1")],
    )

    totals = ebay_finances.build_pallet_report(pallet_id)["totals"]

    assert totals["refunds"] == 21.45
    assert totals["net_payout"] == 0.30


def test_order_without_payout_data_is_an_estimate(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    _make_item(fresh_db, pallet_id)
    _store(fresh_db, orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}],
                                        fee=3.0)])

    report = ebay_finances.build_pallet_report(pallet_id)

    assert report["any_estimates"] is True
    assert report["totals"]["net_payout"] == 22.0  # 25 total - 3 fee
    assert report["totals"]["ebay_fees"] == 3.0


def test_recorded_ebay_sale_with_no_synced_order_is_listed_as_missing(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _make_item(fresh_db, pallet_id)
    fresh_db.record_item_sale(item["id"], 12.0, "eBay", actor_id=1)
    in_person = _make_item(fresh_db, pallet_id)
    fresh_db.record_item_sale(in_person["id"], 12.0, "In Person", actor_id=1)

    report = ebay_finances.build_pallet_report(pallet_id)

    assert [i["id"] for i in report["missing"]] == [item["id"]]
    assert report["profit_with_ebay_net"] is None  # no cost set - nothing to compare


def test_manual_refunds_on_matched_items_are_flagged(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item = _make_item(fresh_db, pallet_id)
    fresh_db.record_refund(item["id"], 5.0, "buyer return", actor_id=1)
    _store(fresh_db, orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}])])

    assert ebay_finances.build_pallet_report(pallet_id)["manual_entries_on_matched"] == 1


def test_overview_separates_unmatched_lines_and_account_level_fees(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    _make_item(fresh_db, pallet_id)
    _store(
        fresh_db,
        orders=[
            _raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}]),
            _raw_order("O-2", [{"id": "L-2", "sku": "someone-elses-sku", "cost": 9.0}], shipping=0.0),
        ],
        txns=[
            _raw_txn("T-1", "SALE", 21.75, "CREDIT", order_id="O-1", fee=3.25),
            _raw_txn("T-2", "SALE", 8.0, "CREDIT", order_id="O-2", fee=1.0),
            _raw_txn("T-3", "NON_SALE_CHARGE", 21.95, "DEBIT", fee_type="EBAY_STORE_SUBSCRIPTION_FEE"),
            _raw_txn("T-4", "SALE", 50.0, "CREDIT", order_id="O-OLD", fee=6.0),  # older than getOrders' 2 years
            _raw_txn("T-5", "WITHDRAWAL", 100.0, "DEBIT"),  # money movement, not a cost
        ],
    )

    overview = ebay_finances.build_overview()

    assert [r["pallet"]["id"] for r in overview["pallets"]] == [pallet_id]
    assert len(overview["unmatched_lines"]) == 1 and overview["unmatched_net"] == 8.0
    assert overview["account_level"] == {"EBAY_STORE_SUBSCRIPTION_FEE": 21.95}
    assert overview["orders_without_breakdown"] == 1 and overview["orders_without_breakdown_net"] == 50.0


# ------------------------------------------------------------ Discord output

def test_embeds_and_csv_fit_discord_limits(fresh_db):
    import cogs.ebay as ebay_cog

    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    fresh_db.set_pallet_cost(pallet_id, 100.0, actor_id=1)
    orders, txns = [], []
    for n in range(1, 60):
        item = _make_item(fresh_db, pallet_id)
        fresh_db.record_item_sale(item["id"], 25.0, "eBay", actor_id=1)
        orders.append(_raw_order(f"O-{n}", [{"id": f"L-{n}", "sku": f"pallet-{pallet_id}-item-{n}", "cost": 20.0, "ship": 5.0}]))
        txns.append(_raw_txn(f"T-{n}", "SALE", 21.75, "CREDIT", order_id=f"O-{n}", fee=3.25))
    _store(fresh_db, orders=orders, txns=txns)

    embeds = [
        ebay_cog.build_pallet_net_embed(ebay_finances.build_pallet_report(pallet_id), None),
        ebay_cog.build_overview_net_embed(ebay_finances.build_overview()),
    ]
    for embed in embeds:
        assert len(embed.fields) <= 25
        total = len(embed.title) + len(embed.description or "") + len(embed.footer.text or "")
        for field in embed.fields:
            assert 1 <= len(field.value) <= 1024, field.name
            total += len(field.name) + len(field.value)
        assert total <= 6000

    rows = ebay_cog.build_net_report_csv([ebay_finances.build_pallet_report(pallet_id)]).decode().splitlines()
    assert len(rows) == 60
    assert rows[1].split(",")[3:] == ["25.00", "20.00", "5.00", "3.25", "0.00", "0.00", "0.00", "21.75", "3.25", "no"]


def _interaction(category_id=None, roles=()):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        channel=SimpleNamespace(category_id=category_id),
        user=SimpleNamespace(id=7, roles=list(roles)),
        guild=object(),
    )


def _cog():
    from types import SimpleNamespace
    import cogs.ebay as ebay_cog
    cog = ebay_cog.Ebay.__new__(ebay_cog.Ebay)
    cog.bot = SimpleNamespace()
    return cog


def test_net_report_command_requires_finance_or_admin_role(fresh_db, monkeypatch):
    import cogs.ebay as ebay_cog
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: object())
    event = _interaction(roles=[])

    asyncio.run(ebay_cog.Ebay.net_report.callback(_cog(), event))

    assert "Finance Management" in event.response.send_message.call_args.args[0]
    event.followup.send.assert_not_called()


def test_net_report_command_sends_embed_and_csv_for_the_channels_pallet(fresh_db, monkeypatch):
    import cogs.ebay as ebay_cog
    role = object()
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: role)
    pallet_id = fresh_db.create_pallet("Report Pallet", category_id=555, created_by=1)
    _make_item(fresh_db, pallet_id)
    _store(fresh_db, orders=[_raw_order("O-1", [{"id": "L-1", "sku": f"pallet-{pallet_id}-item-1", "cost": 20.0}])])
    before = _snapshot(fresh_db, exclude=set())
    event = _interaction(category_id=555, roles=[role])

    asyncio.run(ebay_cog.Ebay.net_report.callback(_cog(), event))

    event.response.defer.assert_awaited_once()
    kwargs = event.followup.send.call_args.kwargs
    assert kwargs["embed"].title.startswith("🧾 Report Pallet")
    assert kwargs["file"].filename == "ebay-net-Report Pallet.csv"
    assert kwargs["ephemeral"] is True
    assert _snapshot(fresh_db, exclude=set()) == before


def test_sync_command_refuses_without_ebay_api_configured(fresh_db, monkeypatch):
    import cogs.ebay as ebay_cog
    role = object()
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: role)
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    event = _interaction(roles=[role])

    asyncio.run(ebay_cog.Ebay.sync_finances.callback(_cog(), event))

    assert "isn't configured" in event.response.send_message.call_args.args[0]


def test_sync_command_reports_an_ebay_error_without_crashing(fresh_db, monkeypatch):
    import cogs.ebay as ebay_cog
    role = object()
    monkeypatch.setattr(ebay_cog.runtime_settings, "resolve_role", lambda guild, name: role)
    monkeypatch.setattr(config, "EBAY_ENABLED", True)

    async def boom(days_back, actor_id=None):
        raise ebay_api.EbayAPIError("eBay getTransactions failed (403)")
    monkeypatch.setattr(ebay_cog.ebay_finances, "sync", boom)
    event = _interaction(roles=[role])

    asyncio.run(ebay_cog.Ebay.sync_finances.callback(_cog(), event))

    message = event.followup.send.call_args.args[0]
    assert "403" in message and "No recorded numbers were changed" in message


def test_money_formatting_never_shows_negative_zero():
    import cogs.ebay as ebay_cog
    assert ebay_cog._money(-0.0) == "$0.00"
    assert ebay_cog._money(-0.001) == "$0.00"
    assert ebay_cog._money(-3.5) == "-$3.50"
