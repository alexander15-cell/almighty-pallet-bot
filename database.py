"""
Database layer for the pallet tracking bot.

Uses SQLite via the standard library for zero-setup persistence.
All access goes through the functions in this file, not raw SQL scattered
through the bot code - this is deliberate, so that migrating to Postgres
or MySQL later only requires rewriting this one file, not the whole bot.

Schema:
    pallets        - one row per pallet (a Discord category)
    items          - one row per physical item, tracked through every stage
    item_events    - an append-only audit log of every stage transition

Item status values (stored as plain strings, see STATUS_* constants):
    data_entry -> automated_review -> queue_review -> awaiting_listing
        -> listed -> sold -> shipped
    (an item can also be sent back to data_entry from queue_review if rejected,
    or removed entirely via /item delete, which sets status to "deleted"
    rather than actually removing the row - so the audit trail survives)

Note: pallet cost and item sale price ARE tracked here again (Purchase
Management sets cost via /finance setprice, Finance Management records sale
prices via /finance record-sale) - this is a deliberate reversal of an earlier
decision to keep pricing out of Discord entirely. The difference this time:
recording a sale price is fully decoupled from the operational "Mark as
Sold" button, so warehouse-side clicking stays instant - only Finance
Management, working at their own pace, ever has to type a number.
"""
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config

STATUS_DATA_ENTRY = "data_entry"
STATUS_AUTOMATED_REVIEW = "automated_review"
STATUS_QUEUE_REVIEW = "queue_review"
STATUS_AWAITING_LISTING = "awaiting_listing"
# (The "pending_ebay_upload"/"pending_fb_marketplace_upload" stages belonged to
# the removed eBay/Facebook CSV batch paths - every eBay listing now goes
# straight from awaiting_listing to listed through the API.)
STATUS_LISTED = "listed"
STATUS_SOLD = "sold"
STATUS_SHIPPED = "shipped"
STATUS_ON_HOLD = "on_hold"    # see item_holds table - a pause with a reason, resolved back to whatever status it was held from
STATUS_REJECTED = "rejected"  # sent back to data entry for redo
STATUS_DELETED = "deleted"    # soft-deleted via /item delete - row kept for audit trail

# Fixed hold reasons for /item hold - a proper lookup value (not free text)
# so holds can be filtered/reported on later, e.g. "how many items are
# stuck on an eBay category problem right now". HOLD_REASON_OTHER is the
# only one where a note is actually required (enforced in cogs/admin_tools.py's
# /item hold command) - every other reason's note is optional extra context.
HOLD_REASON_EBAY_POLICY = "ebay_policy"
HOLD_REASON_CATEGORY_REVIEW = "category_review"
HOLD_REASON_MANUAL_LISTING = "manual_listing"
HOLD_REASON_PHOTO_DATA_ISSUE = "photo_data_issue"
HOLD_REASON_OTHER = "other"
HOLD_REASON_LABELS = {
    HOLD_REASON_EBAY_POLICY: "eBay policy - permanently blocked",
    HOLD_REASON_CATEGORY_REVIEW: "Category needs manual review",
    HOLD_REASON_MANUAL_LISTING: "Needs manual listing",
    HOLD_REASON_PHOTO_DATA_ISSUE: "Photo/data issue",
    HOLD_REASON_OTHER: "Other",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    Path(config.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_add_columns(conn):
    """
    Adds columns/tables introduced after the initial release, for anyone
    upgrading a database that already has data in it. SQLite has no "ADD
    COLUMN IF NOT EXISTS", so each ALTER is attempted and a "duplicate
    column" error is treated as "already migrated" and ignored - anything
    else re-raises.
    """
    migrations = [
        "ALTER TABLE pallets ADD COLUMN archived INTEGER DEFAULT 0",
        "ALTER TABLE pallets ADD COLUMN archived_at TEXT",
        "ALTER TABLE pallets ADD COLUMN pallet_cost REAL",
        "ALTER TABLE pallets ADD COLUMN cost_set_by INTEGER",
        "ALTER TABLE pallets ADD COLUMN items_received_override INTEGER",
        "ALTER TABLE pallets ADD COLUMN finance_message_id INTEGER",
        "ALTER TABLE items ADD COLUMN sale_price REAL",
        "ALTER TABLE items ADD COLUMN sale_platform TEXT",
        "ALTER TABLE items ADD COLUMN photo_public_urls TEXT",
        "ALTER TABLE items ADD COLUMN ai_suggested_category TEXT",
        "ALTER TABLE items ADD COLUMN ai_suggested_price REAL",
        "ALTER TABLE items ADD COLUMN recipient_name TEXT",
        "ALTER TABLE items ADD COLUMN shipping_address TEXT",
        "ALTER TABLE items ADD COLUMN address_line1 TEXT",
        "ALTER TABLE items ADD COLUMN address_line2 TEXT",
        "ALTER TABLE items ADD COLUMN city TEXT",
        "ALTER TABLE items ADD COLUMN state TEXT",
        "ALTER TABLE items ADD COLUMN postal_code TEXT",
        "ALTER TABLE items ADD COLUMN country TEXT",
        "ALTER TABLE items ADD COLUMN pirate_ship_exported INTEGER DEFAULT 0",
        "ALTER TABLE ebay_listing_data ADD COLUMN listing_format TEXT NOT NULL DEFAULT 'FixedPrice'",
        "ALTER TABLE ebay_listing_data ADD COLUMN auction_duration TEXT",
        "ALTER TABLE ebay_listing_data ADD COLUMN ebay_item_id TEXT",
        "ALTER TABLE items ADD COLUMN ebay_batch_id INTEGER",
        "ALTER TABLE ebay_category_usage ADD COLUMN confirmed_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE items ADD COLUMN ai_suggested_weight_lb REAL",
        "ALTER TABLE items ADD COLUMN ai_suggested_length_in REAL",
        "ALTER TABLE items ADD COLUMN ai_suggested_width_in REAL",
        "ALTER TABLE items ADD COLUMN ai_suggested_height_in REAL",
        "ALTER TABLE ebay_listing_data ADD COLUMN weight_lb REAL",
        "ALTER TABLE ebay_listing_data ADD COLUMN length_in REAL",
        "ALTER TABLE ebay_listing_data ADD COLUMN width_in REAL",
        "ALTER TABLE ebay_listing_data ADD COLUMN height_in REAL",
        # Set only by record_item_sale, independent of items.updated_at
        # (which other, later, unrelated changes to the same item also
        # bump) - /finance overview's month-to-date revenue needs a
        # timestamp that means specifically "when this sale was recorded",
        # not "whenever this item was last touched for any reason".
        "ALTER TABLE items ADD COLUMN sale_recorded_at TEXT",
        # Manually-submitted invoices (see #submit-invoices in
        # cogs/finance.py) share the awaiting_pallet_charges table with
        # QuickBooks-sourced charges - 'source' distinguishes the two so
        # claiming knows whether to push a QuickBooks expense, and
        # invoice_photo_path holds the local file path to the submitted
        # invoice image/PDF (only ever set for source='manual').
        "ALTER TABLE awaiting_pallet_charges ADD COLUMN source TEXT NOT NULL DEFAULT 'quickbooks'",
        "ALTER TABLE awaiting_pallet_charges ADD COLUMN invoice_photo_path TEXT",
        "ALTER TABLE items ADD COLUMN ai_review_failed INTEGER NOT NULL DEFAULT 0",
    ]
    for stmt in migrations:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def init_db():
    """Create tables if they don't already exist. Safe to call on every startup."""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pallets (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                name                     TEXT UNIQUE NOT NULL,
                category_id              INTEGER UNIQUE NOT NULL,
                created_by               INTEGER NOT NULL,
                created_at               TEXT NOT NULL,
                archived                 INTEGER DEFAULT 0,
                archived_at              TEXT,
                pallet_cost              REAL,
                cost_set_by              INTEGER,
                items_received_override  INTEGER,
                finance_message_id       INTEGER,
                notes                    TEXT
            );

            CREATE TABLE IF NOT EXISTS channel_map (
                pallet_id       INTEGER NOT NULL REFERENCES pallets(id),
                stage           TEXT NOT NULL,
                channel_id      INTEGER NOT NULL,
                PRIMARY KEY (pallet_id, stage)
            );

            -- One row per stage in config.SHARED_STAGE_CHANNELS. Created once
            -- via /setup shared-channels and used by every pallet, instead of
            -- each pallet getting its own copy of these channels (keeps total
            -- channel count from scaling with the number of pallets).
            CREATE TABLE IF NOT EXISTS shared_channels (
                stage           TEXT PRIMARY KEY,
                channel_id      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS items (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id           INTEGER NOT NULL REFERENCES pallets(id),
                item_number         INTEGER NOT NULL,
                status              TEXT NOT NULL,
                raw_description     TEXT,
                photo_urls          TEXT,              -- JSON list of local file paths
                photo_public_urls   TEXT,              -- JSON list of durable R2 public URLs (same order as photo_urls, see r2_storage.py)
                ai_title            TEXT,
                ai_description      TEXT,
                ai_flags            TEXT,
                ai_suggested_category TEXT,          -- short free-text product-type guess (e.g. "cordless drill") - resolved against eBay's real taxonomy via ebay_taxonomy.search() to pre-select the Queue Review category step
                ai_suggested_price  REAL,             -- rough AI price guess (general knowledge, not real market data) - pre-fills the Queue Review price field, always human-editable
                ai_suggested_weight_lb  REAL,         -- rough AI shipping-weight guess (visual estimate, not a real measurement) - pre-fills the Queue Review weight field
                ai_suggested_length_in  REAL,         -- rough AI packaged-dimension guesses (visual estimate) - pre-fill the Queue Review dimensions field
                ai_suggested_width_in   REAL,
                ai_suggested_height_in  REAL,
                ai_review_failed    INTEGER NOT NULL DEFAULT 0, -- set when the last AI review was a fallback (see ai_review._fallback) - cogs/item_flow.py's ai_retry_backlog_loop retries these automatically once the backend's reachable again; cleared on a successful review or a manual edit
                submitted_by        INTEGER,
                current_message_id  INTEGER,            -- message representing item in its CURRENT channel
                listed_at           TEXT,
                sold_at             TEXT,
                sale_price          REAL,
                sale_platform       TEXT,
                sale_recorded_at    TEXT,               -- when record_item_sale last set sale_price - see its own migration comment for why this isn't just updated_at/sold_at
                recipient_name      TEXT,               -- non-eBay ("Other" platform) sales only - for the Pirate Ship CSV export
                shipping_address    TEXT,               -- legacy freeform address (pre-structured-fields items) - kept for old rows, see address_line1 etc. below
                address_line1       TEXT,               -- non-eBay ("Other" platform) sales only - structured shipping address for the Pirate Ship CSV export
                address_line2       TEXT,
                city                TEXT,
                state               TEXT,
                postal_code         TEXT,
                country             TEXT,
                pirate_ship_exported INTEGER DEFAULT 0,  -- set once this item's shipping info has gone out in a /pirate-ship export-batch, so it isn't exported twice
                ebay_batch_id       INTEGER,             -- history only: the ebay_batches row from the removed eBay CSV batch path this item went out in, if any
                shipped_at          TEXT,
                stale_alert_sent    INTEGER DEFAULT 0,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                UNIQUE(pallet_id, item_number)
            );

            CREATE TABLE IF NOT EXISTS item_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     INTEGER NOT NULL REFERENCES items(id),
                from_status TEXT,
                to_status   TEXT NOT NULL,
                actor_id    INTEGER,
                note        TEXT,
                timestamp   TEXT NOT NULL
            );

            -- One row per hold placed on an item (see /item hold,
            -- cogs/admin_tools.py) - a real, filterable record, not just
            -- text buried in item_events.note, so "how many items are held
            -- for X reason" can actually be queried. resolved_at/resolved_by
            -- stay NULL while the hold is open; resolving NEVER deletes the
            -- row (see database.resolve_item_hold), so the full history of
            -- every hold an item's ever had survives, including which
            -- ones are still open (an item can only have one open hold at
            -- a time - its status is STATUS_ON_HOLD while one exists).
            -- pre_hold_status is what status the item was AT when placed
            -- on hold, so resolving can send its card back to exactly
            -- that stage's channel/view instead of guessing.
            CREATE TABLE IF NOT EXISTS item_holds (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id          INTEGER NOT NULL REFERENCES items(id),
                reason           TEXT NOT NULL,     -- one of HOLD_REASON_* above
                note             TEXT,              -- required only when reason = HOLD_REASON_OTHER
                pre_hold_status  TEXT NOT NULL,
                set_by           INTEGER,
                set_at           TEXT NOT NULL,
                resolved_by      INTEGER,
                resolved_at      TEXT
            );

            -- One row per item, captured during Queue Review approval (see the
            -- eBay listing modal in item_flow.py) - everything needed to list
            -- the item on eBay besides the description/photos already on the
            -- items row itself. item_specifics is a JSON object since which
            -- attributes apply (brand, size, color, ...) varies by category.
            CREATE TABLE IF NOT EXISTS ebay_listing_data (
                item_id         INTEGER PRIMARY KEY REFERENCES items(id),
                ebay_title      TEXT NOT NULL,
                category_id     TEXT NOT NULL,
                condition_id    TEXT NOT NULL,
                price           REAL NOT NULL,        -- fixed price, or auction starting bid when listing_format = 'Auction'
                listing_format  TEXT NOT NULL DEFAULT 'FixedPrice',  -- 'FixedPrice' or 'Auction'
                auction_duration TEXT,                -- eBay *Duration value (e.g. 'Days_7') - only set when listing_format = 'Auction'
                item_specifics  TEXT,
                ebay_item_id    TEXT,                 -- the real eBay listing ID, set once /ebay import-results (or confirm-listed) confirms this item went live
                weight_lb       REAL,                 -- shipping weight, feeds the Pirate Ship CSV export (every approved item gets this captured here regardless of eventual sale platform) - eBay's own CSV upload doesn't carry shipping data at all
                length_in       REAL,                 -- packaged dimensions, same purpose as weight_lb
                width_in        REAL,
                height_in       REAL,
                set_by          INTEGER,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            -- History only: one row per /ebay export-batch run from the
            -- removed eBay CSV batch path (items.ebay_batch_id points back
            -- here). Nothing writes to it anymore; kept so past batch
            -- records aren't deleted.
            CREATE TABLE IF NOT EXISTS ebay_batches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                csv_filename TEXT NOT NULL,
                exported_by  INTEGER,
                item_count   INTEGER NOT NULL,
                created_at   TEXT NOT NULL
            );

            -- One row per eBay category ID that's ever been picked during
            -- Queue Review approval. Lets EbayCategoryPickView's quick-pick
            -- dropdown (item_flow.py) sort previously-used categories by
            -- actual usage (most-picked first) instead of a fixed order.
            -- confirmed_count (unlike use_count, bumped as soon as a
            -- category is picked during Queue Review) only increments once
            -- an item in that category is actually confirmed live on eBay -
            -- see record_ebay_category_confirmed - so a category that's
            -- been picked but never successfully gone live doesn't look
            -- just as trusted as one that's proven to actually sell.
            -- ebay_taxonomy.py (eBay's own official category data) already
            -- guarantees every ID is a real, listable leaf category, so
            -- this table is purely about which ones this team has actually
            -- had success with, not about ID validity anymore.
            CREATE TABLE IF NOT EXISTS ebay_category_usage (
                category_id     TEXT PRIMARY KEY,
                use_count       INTEGER NOT NULL DEFAULT 0,
                confirmed_count INTEGER NOT NULL DEFAULT 0
            );

            -- Refunds, pallet/item-level expenses, and sale reversals - kept
            -- separate from items.sale_price so a correction never destroys
            -- history. get_pallet_financials() sums these by type to net
            -- against raw revenue; /finance history lists them per pallet.
            CREATE TABLE IF NOT EXISTS finance_transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id   INTEGER NOT NULL REFERENCES pallets(id),
                item_id     INTEGER REFERENCES items(id),  -- NULL for a pallet-level expense not tied to one item
                type        TEXT NOT NULL,                  -- 'refund', 'expense', or 'reversal'
                amount      REAL NOT NULL,                   -- always positive; type determines its effect on net revenue
                note        TEXT,
                actor_id    INTEGER,
                created_at  TEXT NOT NULL
            );

            -- Itemized ADDITIONAL costs allocated to a pallet from the
            -- QuickBooks credit-card and Pirate Ship shipping workflows
            -- (see quickbooks.py, cogs/finance.py) - deliberately separate
            -- from finance_transactions' pre-existing 'expense' type, which
            -- stays exactly what it always was (a manual, freeform
            -- /finance expense entry). This table exists because these
            -- newer workflows need a structured cost_type (for the
            -- /finance pallet-summary breakdown) and a durable link back to
            -- the QuickBooks transaction they came from, for traceability
            -- and so the same charge can never be allocated twice.
            --
            -- A pallet's full cost basis = pallets.pallet_cost (the legacy
            -- manual /finance setprice lump sum) + SUM(pallet_costs.amount)
            -- - additive, not a replacement; see get_pallet_financials().
            CREATE TABLE IF NOT EXISTS pallet_costs (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                pallet_id          INTEGER NOT NULL REFERENCES pallets(id),
                item_id            INTEGER REFERENCES items(id),  -- set for a shipping cost tied to one sold item; NULL for a pallet-level charge
                cost_type          TEXT NOT NULL,                  -- 'credit_card', 'shipping', 'supplies', 'misc'
                amount             REAL NOT NULL,
                description        TEXT,                            -- e.g. the merchant name, or a shipment reference
                source             TEXT NOT NULL,                    -- 'quickbooks', 'pirateship', 'manual'
                quickbooks_txn_id  TEXT,                             -- the QuickBooks Purchase transaction this came from, if any
                created_by         INTEGER,
                created_at         TEXT NOT NULL
            );

            -- A QuickBooks credit-card charge allocated to "New Pallet (not
            -- arrived yet)" before that pallet exists in Discord - sits
            -- here, shown in #awaiting-pallet-charges, until a real pallet
            -- claims it at creation time (see pallet_setup.py), which
            -- copies it into pallet_costs above and marks it claimed here.
            CREATE TABLE IF NOT EXISTS awaiting_pallet_charges (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                quickbooks_txn_id  TEXT NOT NULL UNIQUE,  -- synthetic "manual-<uuid>" for source='manual' rows
                amount             REAL NOT NULL,
                merchant           TEXT,
                txn_date           TEXT,
                allocated_by       INTEGER,
                message_id         INTEGER,           -- its #awaiting-pallet-charges card, so it can be removed once claimed
                claimed            INTEGER NOT NULL DEFAULT 0,
                claimed_pallet_id  INTEGER REFERENCES pallets(id),
                claimed_at         TEXT,
                source             TEXT NOT NULL DEFAULT 'quickbooks',  -- 'quickbooks' or 'manual'
                invoice_photo_path TEXT,               -- local file path, only set for source='manual'
                created_at         TEXT NOT NULL
            );

            -- Every QuickBooks transaction ID the credit-card poller has
            -- ever shown in Discord, regardless of what happened to it
            -- afterward - makes re-polling idempotent (a charge is never
            -- posted twice) without relying solely on QuickBooks' own date
            -- filtering, which an edited/backdated transaction could fool.
            CREATE TABLE IF NOT EXISTS quickbooks_seen_charges (
                quickbooks_txn_id  TEXT PRIMARY KEY,
                first_seen_at      TEXT NOT NULL
            );

            -- Same idea as quickbooks_seen_charges above, for the eBay
            -- order auto-sync poll loop (cogs/ebay.py) - every eBay order
            -- ID it's ever posted a confirm card for, so re-polling never
            -- posts the same order twice.
            CREATE TABLE IF NOT EXISTS ebay_seen_orders (
                ebay_order_id  TEXT PRIMARY KEY,
                first_seen_at  TEXT NOT NULL
            );

            -- Single-row OAuth token storage for the one connected
            -- QuickBooks company (id is always 1 - CHECK enforces that).
            -- refresh_token is overwritten on every refresh since
            -- QuickBooks rotates it and the old one stops working;
            -- access_token_expires_at drives proactive refresh-before-
            -- expiry (see quickbooks.py) rather than waiting for a 401.
            CREATE TABLE IF NOT EXISTS quickbooks_connection (
                id                       INTEGER PRIMARY KEY CHECK (id = 1),
                realm_id                 TEXT NOT NULL,
                access_token             TEXT NOT NULL,
                refresh_token            TEXT NOT NULL,
                access_token_expires_at  TEXT NOT NULL,
                connected_by             INTEGER,
                connected_at             TEXT NOT NULL,
                updated_at               TEXT NOT NULL
            );

            -- The single pinned business-wide dashboard message (see
            -- finance_utils.py's build_dashboard_embed/refresh_dashboard_message)
            -- in #finance-dashboard - id is always 1, same single-row
            -- pattern as quickbooks_connection above.
            CREATE TABLE IF NOT EXISTS dashboard_message (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                message_id  INTEGER NOT NULL
            );

            -- One row per real-world sale transaction, which can cover one
            -- item or a bundle sold together (possibly across different
            -- pallets - e.g. a buyer checking out with items from two
            -- separate pallets at once). total_price is always the pre-tax
            -- amount actually entered by a partner - this is also what
            -- QuickBooks Automated Sales Tax is fed for a FB/in-person
            -- Sales Receipt, letting IT compute tax rather than this bot
            -- guessing at a flat rate.
            CREATE TABLE IF NOT EXISTS sales (
                id                            INTEGER PRIMARY KEY AUTOINCREMENT,
                platform                      TEXT NOT NULL,   -- 'eBay', 'Facebook Marketplace', 'In Person', or 'Other: <text>'
                sale_date                     TEXT NOT NULL,
                total_price                   REAL NOT NULL,   -- pre-tax
                already_deposited             INTEGER NOT NULL DEFAULT 0,
                quickbooks_sales_receipt_id   TEXT,
                -- quickbooks_journal_entry_id/cogs_logged_at: legacy, from
                -- before the switch to cash-basis accounting - a sale used
                -- to also push a COGS Journal Entry at sale time, which set
                -- these. Kept (never backfilled) so old rows stay readable;
                -- a new sale is now "fully logged" once
                -- quickbooks_sales_receipt_id is set - see
                -- finance_utils.post_sale_to_quickbooks.
                quickbooks_journal_entry_id   TEXT,
                cogs_logged_at                TEXT,
                created_by                    INTEGER NOT NULL,
                created_at                    TEXT NOT NULL
            );

            -- One row per item within a sales row above - an item can only
            -- ever belong to one sale (UNIQUE on item_id). purchase_price/
            -- cogs_amount are legacy NOT NULL columns from before the
            -- switch to pure cash basis - always written as 0.0 now (see
            -- add_sale_item), never backfilled or dropped since SQLite
            -- can't drop a column cleanly and old rows' real figures stay
            -- meaningful history.
            CREATE TABLE IF NOT EXISTS sale_items (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id          INTEGER NOT NULL REFERENCES sales(id),
                item_id          INTEGER NOT NULL UNIQUE REFERENCES items(id),
                allocated_price  REAL NOT NULL,
                purchase_price   REAL NOT NULL,
                cogs_amount      REAL NOT NULL
            );

            -- eBay's own financial record, pulled by /ebay sync-finances
            -- (see ebay_finances.py) - a READ-ONLY mirror of what eBay
            -- reports, kept entirely separate from this bot's own sale/cost
            -- tables (items.sale_price, sales, sale_items, pallet_costs,
            -- finance_transactions), which nothing in the sync ever writes
            -- to. Lets /ebay net-report show eBay's real fees/shipping/
            -- payout side-by-side with what's been recorded, without
            -- changing a single existing number.
            --
            -- Deliberately NO foreign keys: these rows describe eBay's
            -- data, not this bot's, so they must never block (or be
            -- silently cascaded by) /pallet delete or /admin db-wipe.
            -- Items are matched at report time instead (SKU / listing ID,
            -- see ebay_finances.match_line_to_item), never stored here.
            -- No buyer name/address is stored either - only money fields.

            -- One row per eBay Finances API monetary transaction
            -- (getTransactions) - SALE, REFUND, SHIPPING_LABEL,
            -- NON_SALE_CHARGE (ad/insertion/store fees), CREDIT, DISPUTE,
            -- etc. (transaction_id, transaction_type) is eBay's own unique
            -- key. amount is always positive; booking_entry (CREDIT/DEBIT)
            -- gives its direction. Re-syncing refreshes a row in place
            -- (payout status changes over time), never duplicates it.
            CREATE TABLE IF NOT EXISTS ebay_fin_transactions (
                transaction_id          TEXT NOT NULL,
                transaction_type        TEXT NOT NULL,
                transaction_date        TEXT,
                transaction_status      TEXT,
                booking_entry           TEXT,
                amount                  REAL,
                total_fee_amount        REAL,
                total_fee_basis_amount  REAL,
                ebay_collected_tax      REAL,
                order_id                TEXT,
                fee_type                TEXT,
                transaction_memo        TEXT,
                references_json         TEXT,   -- [{"type": "ITEM_ID", "id": "..."}, ...]
                line_fees_json          TEXT,   -- {"<lineItemId>": total marketplace fees for that line}
                payout_id               TEXT,
                first_synced_at         TEXT NOT NULL,
                last_synced_at          TEXT NOT NULL,
                PRIMARY KEY (transaction_id, transaction_type)
            );

            -- One row per eBay order (Fulfillment API getOrders) - the
            -- price/shipping/discount breakdown the Finances API doesn't
            -- itemize. Money fields only.
            CREATE TABLE IF NOT EXISTS ebay_order_breakdowns (
                ebay_order_id           TEXT PRIMARY KEY,
                order_date              TEXT,
                price_subtotal          REAL,
                price_discount          REAL,   -- stored as a positive number
                delivery_cost           REAL,
                delivery_discount       REAL,   -- stored as a positive number
                adjustment              REAL,
                order_total             REAL,   -- pricingSummary.total - what the eBay order card logs as the sale
                total_marketplace_fee   REAL,
                total_fee_basis_amount  REAL,
                cancel_state            TEXT,
                first_synced_at         TEXT NOT NULL,
                last_synced_at          TEXT NOT NULL
            );

            -- One row per eBay order line item - sku/legacy_item_id are
            -- how a line is matched back to a tracked item at report time.
            CREATE TABLE IF NOT EXISTS ebay_order_lines (
                line_item_id     TEXT PRIMARY KEY,
                ebay_order_id    TEXT NOT NULL,
                sku              TEXT,
                legacy_item_id   TEXT,
                title            TEXT,
                quantity         INTEGER,
                line_item_cost   REAL,
                shipping_cost    REAL,
                first_synced_at  TEXT NOT NULL,
                last_synced_at   TEXT NOT NULL
            );

            -- One row per automatic action/alert the eBay order automation
            -- (cogs/ebay.py's _reconcile_ebay_order_statuses) has taken for
            -- an order + item - 'sold', 'shipped', 'cancel_alert',
            -- 'hold_alert'. The UNIQUE key is the dedup ledger (an alert is
            -- posted once, not every poll) and the table doubles as an
            -- audit trail. No foreign keys, same reasoning as the eBay
            -- finance mirror tables: it must never block /pallet delete.
            CREATE TABLE IF NOT EXISTS ebay_order_automation_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ebay_order_id  TEXT NOT NULL,
                item_id        INTEGER NOT NULL,
                action         TEXT NOT NULL,
                created_at     TEXT NOT NULL,
                UNIQUE (ebay_order_id, item_id, action)
            );

            -- One row per /ebay sync-finances run, so the report can say
            -- how current its eBay numbers are.
            CREATE TABLE IF NOT EXISTS ebay_finance_sync_log (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at         TEXT NOT NULL,
                finished_at        TEXT,
                transactions_from  TEXT,
                orders_from        TEXT,
                transaction_count  INTEGER,
                order_count        INTEGER,
                error              TEXT,
                actor_id           INTEGER
            );

            -- eBay's own list of item specifics ("aspects": Brand, Type,
            -- Color, ...) for a leaf category, from the Taxonomy API's
            -- getItemAspectsForCategory - cached because it rarely changes
            -- and eBay asks callers to cache it (see ebay_aspects.py for
            -- the refresh age). aspects_json is ebay_aspects' parsed list.
            CREATE TABLE IF NOT EXISTS ebay_category_aspects (
                category_id   TEXT PRIMARY KEY,
                aspects_json  TEXT NOT NULL,
                fetched_at    TEXT NOT NULL
            );

            -- The AI's suggested item specifics for an item, filled against
            -- eBay's aspect list for the category Automated Review matched
            -- (ebay_aspects.prefill_for_items) - pre-fills Queue Review's
            -- Item Specifics box at Approve, only when the reviewer keeps
            -- that same category. A suggestion, never applied by itself.
            -- item_created_at guards against a stale row ever attaching to
            -- a different item after /admin db-wipe resets item IDs (no
            -- foreign keys, same reasoning as the eBay mirror tables).
            CREATE TABLE IF NOT EXISTS item_aspect_suggestions (
                item_id          INTEGER PRIMARY KEY,
                item_created_at  TEXT NOT NULL,
                category_id      TEXT NOT NULL,
                specifics_json   TEXT NOT NULL,
                created_at       TEXT NOT NULL
            );

            -- The eBay fee expense ledger (ebay_fee_expenses.py): one row
            -- per eBay Finances transaction (same key as
            -- ebay_fin_transactions) that's been booked to QuickBooks by a
            -- "Confirm & log fees" click. The primary key is what makes it
            -- impossible to book the same eBay charge twice. amount is the
            -- cost booked (negative for a credit back, e.g. a voided label).
            CREATE TABLE IF NOT EXISTS ebay_fee_postings (
                transaction_id     TEXT NOT NULL,
                transaction_type   TEXT NOT NULL,
                ebay_order_id      TEXT NOT NULL,
                category           TEXT NOT NULL,   -- 'fees', 'shipping', 'ads'
                amount             REAL NOT NULL,
                quickbooks_je_id   TEXT,
                posted_by          INTEGER,
                posted_at          TEXT NOT NULL,
                PRIMARY KEY (transaction_id, transaction_type)
            );

            -- Which eBay transactions already appear on a posted fee card,
            -- so the background job posts a card once, not every run. A
            -- charge arriving later (a label bought after the first card)
            -- isn't in here yet, so it gets its own follow-up card.
            CREATE TABLE IF NOT EXISTS ebay_fee_card_txns (
                transaction_id     TEXT NOT NULL,
                transaction_type   TEXT NOT NULL,
                ebay_order_id      TEXT NOT NULL,
                carded_at          TEXT NOT NULL,
                PRIMARY KEY (transaction_id, transaction_type)
            );

            -- Single row: the first day fee cards were posted automatically
            -- (see config.EBAY_FEE_CARDS_START_DATE) - so turning the
            -- feature on doesn't flood #ebay-orders with every past order.
            CREATE TABLE IF NOT EXISTS ebay_fee_card_state (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                start_date  TEXT NOT NULL
            );

            -- eBay payouts (Finances API getPayouts) - the money eBay sends
            -- to the bank, mirrored like ebay_fin_transactions (written only
            -- by ebay_finances.sync). Each transaction's payout_id says which
            -- payout it went out in. Only the bank's nickname and last four
            -- digits are kept, never a full account number.
            CREATE TABLE IF NOT EXISTS ebay_payouts (
                payout_id            TEXT PRIMARY KEY,
                payout_status        TEXT,
                amount               REAL,
                payout_date          TEXT,
                transaction_count    INTEGER,
                instrument_nickname  TEXT,
                instrument_last4     TEXT,
                first_synced_at      TEXT NOT NULL,
                last_synced_at       TEXT NOT NULL
            );

            -- A payout recorded in QuickBooks (ebay_books.confirm_payout):
            -- the Deposit (or fallback Journal Entry) that moved it from
            -- Undeposited Funds into the bank. The primary key is what stops
            -- one payout being deposited twice.
            CREATE TABLE IF NOT EXISTS ebay_payout_deposits (
                payout_id             TEXT PRIMARY KEY,
                quickbooks_txn_id     TEXT NOT NULL,
                quickbooks_txn_type   TEXT NOT NULL,   -- 'Deposit' or 'JournalEntry'
                amount                REAL NOT NULL,
                bank_account_id       TEXT NOT NULL,
                posted_by             INTEGER,
                posted_at             TEXT NOT NULL
            );

            -- Which payouts / refund orders / account-charge months already
            -- have a card in #ebay-orders, so the background job posts each
            -- once. kind: 'payout', 'refund' (key = order id + txn) or
            -- 'account' (key = YYYY-MM + txn).
            -- Web dashboard sign-ins (webapp/auth.py). token_hash is the
            -- SHA-256 of the session cookie, never the cookie itself.
            CREATE TABLE IF NOT EXISTS web_sessions (
                token_hash        TEXT PRIMARY KEY,
                discord_user_id   INTEGER NOT NULL,
                display_name      TEXT,
                csrf_token        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                expires_at        TEXT NOT NULL
            );

            -- Stripe website checkouts (stripe_shop.py): one row per
            -- Checkout Session the bot created, and how far its fulfillment
            -- got (each step is recorded so a retry never repeats one).
            CREATE TABLE IF NOT EXISTS stripe_checkouts (
                session_id        TEXT PRIMARY KEY,
                source            TEXT NOT NULL,      -- 'website' or 'link'
                status            TEXT NOT NULL,      -- open / processing / paid / fulfilled / attention / expired / failed
                item_ids          TEXT NOT NULL,      -- JSON list
                url               TEXT,
                delivery          TEXT,               -- 'ship' or 'pickup'
                expires_at        TEXT,
                amount_total      REAL,
                amount_tax        REAL,
                amount_shipping   REAL,
                stripe_fee        REAL,
                sale_id           INTEGER,
                sold_marked       INTEGER NOT NULL DEFAULT 0,
                fees_booked       INTEGER NOT NULL DEFAULT 0,
                notified          INTEGER NOT NULL DEFAULT 0,
                note              TEXT,
                created_by        INTEGER,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            );

            -- Items held by an open Stripe checkout, so a one-of-a-kind item
            -- can't be bought twice. Released when the session expires/fails.
            CREATE TABLE IF NOT EXISTS stripe_holds (
                item_id      INTEGER PRIMARY KEY,
                session_id   TEXT NOT NULL,
                expires_at   TEXT NOT NULL
            );

            -- Stripe webhook events already handled (Stripe can resend).
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id     TEXT PRIMARY KEY,
                event_type   TEXT,
                received_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ebay_book_cards (
                kind       TEXT NOT NULL,
                card_key   TEXT NOT NULL,
                carded_at  TEXT NOT NULL,
                PRIMARY KEY (kind, card_key)
            );

            -- The #accounting worklist: which #accounting message belongs to
            -- which sold item (ItemFlow._post_to_accounting_channel), so it
            -- can be edited to "✅ logged" once the sale is booked to
            -- QuickBooks (finance_utils.update_accounting_entries). One row
            -- per item - a re-sold item's newer post replaces the old row.
            -- item_created_at guards against a re-issued item ID after
            -- /admin db-wipe (no foreign keys, like the eBay tables).
            CREATE TABLE IF NOT EXISTS accounting_worklist (
                item_id          INTEGER PRIMARY KEY,
                item_created_at  TEXT NOT NULL,
                channel_id       INTEGER NOT NULL,
                message_id       INTEGER NOT NULL,
                platform         TEXT,
                posted_at        TEXT NOT NULL,
                sale_id          INTEGER,
                logged_at        TEXT
            );

            -- Sales undone with /finance reverse-sale after being logged
            -- (void_logged_sale). The sales row itself is kept untouched;
            -- this marks it voided. Its sale_items rows MOVE to
            -- voided_sale_items (same columns) - sale_items.item_id is
            -- UNIQUE, so leaving them would block the item from ever being
            -- logged again - so the full history is kept, just not in the
            -- table that means "this item is currently sold".
            CREATE TABLE IF NOT EXISTS voided_sales (
                sale_id                      INTEGER PRIMARY KEY,
                reason                       TEXT,
                quickbooks_sales_receipt_id  TEXT,
                voided_by                    INTEGER,
                voided_at                    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voided_sale_items (
                id               INTEGER PRIMARY KEY,   -- the original sale_items.id
                sale_id          INTEGER NOT NULL,
                item_id          INTEGER NOT NULL,
                allocated_price  REAL NOT NULL,
                purchase_price   REAL NOT NULL,
                cogs_amount      REAL NOT NULL,
                voided_at        TEXT NOT NULL
            );

            -- The business's partners (see partner_ledger.py) - profit is
            -- split equally between the ACTIVE ones. Removing a partner
            -- only sets active = 0; their ledger history stays.
            CREATE TABLE IF NOT EXISTS partners (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id  INTEGER NOT NULL UNIQUE,
                display_name     TEXT NOT NULL,
                active           INTEGER NOT NULL DEFAULT 1,
                added_by         INTEGER,
                added_at         TEXT NOT NULL
            );

            -- Money between the business and its partners. A record only -
            -- the bot never moves money or posts these to QuickBooks.
            --   contribution  - partner put money into the business
            --   fronted       - partner paid a business expense personally
            --                   (the business owes it back; the expense
            --                   itself is still recorded the normal way)
            --   reimbursement - business paid a partner back for fronted costs
            --   payout        - business paid a partner part of the profit
            -- amount is always positive. Mistakes are voided (voided_at +
            -- reason), never deleted, so the history stays complete.
            CREATE TABLE IF NOT EXISTS partner_ledger (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id   INTEGER NOT NULL,
                entry_type   TEXT NOT NULL,
                amount       REAL NOT NULL,
                note         TEXT,
                entry_date   TEXT NOT NULL,
                created_by   INTEGER,
                created_at   TEXT NOT NULL,
                voided_at    TEXT,
                voided_by    INTEGER,
                void_reason  TEXT
            );
            """
        )
        _migrate_add_columns(conn)


# ---------------------------------------------------------------- pallets --

def create_pallet(name: str, category_id: int, created_by: int, notes: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO pallets (name, category_id, created_by, created_at, notes) VALUES (?, ?, ?, ?, ?)",
            (name, category_id, created_by, _now(), notes),
        )
        return cur.lastrowid


def get_pallet_by_name(name: str):
    """Case-insensitive exact match - used by /finance pallet-summary so it
    can be run from anywhere (e.g. a finance channel), not just from inside
    that pallet's own channels."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pallets WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
        return dict(row) if row else None


def get_pallet_by_category(category_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pallets WHERE category_id = ?", (category_id,)).fetchone()
        return dict(row) if row else None


def get_pallet(pallet_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pallets WHERE id = ?", (pallet_id,)).fetchone()
        return dict(row) if row else None


def get_all_pallets(include_archived: bool = True):
    """All pallets, newest first. Used by /pallet-list."""
    with get_conn() as conn:
        query = "SELECT * FROM pallets"
        if not include_archived:
            query += " WHERE archived = 0"
        query += " ORDER BY id DESC"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]


def get_all_channel_ids_for_pallet(pallet_id: int) -> list[int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT channel_id FROM channel_map WHERE pallet_id = ?", (pallet_id,)).fetchall()
        return [r["channel_id"] for r in rows]


def archive_pallet(pallet_id: int):
    """
    Marks a pallet archived. Does NOT delete anything from the database -
    items and their full history stay exactly as they are, permanently
    queryable. The caller (admin_tools cog) is responsible for actually
    deleting the pallet's Discord category/channels, since that's a Discord
    API action, not a database one - this function only flips the flag.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE pallets SET archived = 1, archived_at = ? WHERE id = ?",
            (_now(), pallet_id),
        )


def delete_pallet_permanently(pallet_id: int):
    """
    Irreversibly erases a pallet and everything tied to it: every item and
    its full history (item_events, item_holds, ebay_listing_data), and
    every finance_transactions/pallet_costs row for the pallet or any of
    its items. Frees pallets.name (UNIQUE) for reuse - unlike
    archive_pallet, which deliberately keeps every row forever. The only
    way back after this is a database backup; see cogs/admin_tools.py's
    /admin pallet-delete for the confirmation step this requires.

    Any awaiting_pallet_charges row claimed by this pallet is NOT deleted -
    real money already logged there goes back into the unclaimed pool
    (reverted exactly like an un-claim) for a different pallet to pick up,
    rather than disappearing along with this one. The caller is
    responsible for the pallet's Discord category/channels, same division
    of labor as archive_pallet.

    A sale_items row for one of this pallet's items is deleted outright
    (not un-claimed) - unlike awaiting_pallet_charges, there's no "put it
    back in a pool" state for a QuickBooks Sales Receipt that already
    posted for real. The parent sales row is left alone even then,
    since /finance log-sale's bundles can span other pallets' items too -
    only ever this pallet's own sale_items rows are touched.
    """
    with get_conn() as conn:
        item_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM items WHERE pallet_id = ?", (pallet_id,)
        ).fetchall()]
        if item_ids:
            placeholders = ",".join("?" * len(item_ids))
            conn.execute(f"DELETE FROM item_events WHERE item_id IN ({placeholders})", item_ids)
            conn.execute(f"DELETE FROM item_holds WHERE item_id IN ({placeholders})", item_ids)
            conn.execute(f"DELETE FROM ebay_listing_data WHERE item_id IN ({placeholders})", item_ids)
            conn.execute(f"DELETE FROM sale_items WHERE item_id IN ({placeholders})", item_ids)
        conn.execute("DELETE FROM finance_transactions WHERE pallet_id = ?", (pallet_id,))
        conn.execute("DELETE FROM pallet_costs WHERE pallet_id = ?", (pallet_id,))
        conn.execute(
            "UPDATE awaiting_pallet_charges SET claimed = 0, claimed_pallet_id = NULL, claimed_at = NULL "
            "WHERE claimed_pallet_id = ?",
            (pallet_id,),
        )
        conn.execute("DELETE FROM items WHERE pallet_id = ?", (pallet_id,))
        conn.execute("DELETE FROM channel_map WHERE pallet_id = ?", (pallet_id,))
        conn.execute("DELETE FROM pallets WHERE id = ?", (pallet_id,))


def map_channel(pallet_id: int, stage: str, channel_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO channel_map (pallet_id, stage, channel_id) VALUES (?, ?, ?)",
            (pallet_id, stage, channel_id),
        )


# ------------------------------------------------------- shared channels --

def set_shared_channel(stage: str, channel_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO shared_channels (stage, channel_id) VALUES (?, ?)",
            (stage, channel_id),
        )


def get_shared_channel_id(stage: str):
    with get_conn() as conn:
        row = conn.execute("SELECT channel_id FROM shared_channels WHERE stage = ?", (stage,)).fetchone()
        return row["channel_id"] if row else None


def get_all_shared_channels() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT stage, channel_id FROM shared_channels").fetchall()
        return {r["stage"]: r["channel_id"] for r in rows}


def get_dashboard_message_id():
    with get_conn() as conn:
        row = conn.execute("SELECT message_id FROM dashboard_message WHERE id = 1").fetchone()
        return row["message_id"] if row else None


def set_dashboard_message_id(message_id: int):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO dashboard_message (id, message_id) VALUES (1, ?)", (message_id,))


def ensure_items_autoincrement_floor(floor: int) -> bool:
    """
    One-time bump so the NEXT item created gets id >= floor - used by
    cogs/website_shop.py so every item eligible for that feature is a new
    one created after it was turned on, never a pre-existing real item.
    Idempotent and safe to call every startup: only raises the counter,
    never lowers it (so it can't undo real IDs already issued above floor,
    and calling it again after the first real bump is a no-op).
    """
    with get_conn() as conn:
        current = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'items'").fetchone()
        current_seq = current["seq"] if current else 0
        target_seq = floor - 1
        if current_seq >= target_seq:
            return False
        # Deliberately not "INSERT OR REPLACE": against sqlite_sequence that
        # silently fails to persist (a documented SQLite quirk with this
        # internal bookkeeping table) even though it reports success and
        # raises nothing - confirmed by reproducing it directly against a
        # scratch database. A plain UPDATE (when a row already exists, which
        # it always will once any item has ever been inserted) or INSERT
        # (only for a table that has never had a row) are the only forms
        # that actually take effect.
        if current is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('items', ?)", (target_seq,))
        else:
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'items'", (target_seq,))
        return True


def is_shared_channels_setup() -> bool:
    existing = get_all_shared_channels()
    return all(stage in existing for stage in config.SHARED_STAGE_CHANNELS)


def get_stage_channel_id(pallet_id: int, stage: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT channel_id FROM channel_map WHERE pallet_id = ? AND stage = ?",
            (pallet_id, stage),
        ).fetchone()
        return row["channel_id"] if row else None


def resolve_channel_id(pallet_id: int, stage: str):
    """
    The single lookup item_flow.py should use for "what channel does this
    stage live in for this pallet". Transparently routes to the shared,
    server-wide channel for stages in config.SHARED_STAGE_CHANNELS, or the
    pallet's own per-pallet channel otherwise (data-entry, discussion) -
    callers don't need to know or care which kind a stage is.
    """
    if stage in config.SHARED_STAGE_CHANNELS:
        return get_shared_channel_id(stage)
    return get_stage_channel_id(pallet_id, stage)


def get_pallet_id_for_channel(channel_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT pallet_id FROM channel_map WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["pallet_id"] if row else None


def get_stage_for_channel(channel_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT stage FROM channel_map WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["stage"] if row else None


# ------------------------------------------------------------------ items --

def create_item(pallet_id: int, raw_description: str, photo_urls: list, submitted_by: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(item_number), 0) + 1 AS n FROM items WHERE pallet_id = ?",
            (pallet_id,),
        ).fetchone()
        item_number = row["n"]
        now = _now()
        cur = conn.execute(
            """INSERT INTO items
               (pallet_id, item_number, status, raw_description, photo_urls,
                submitted_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pallet_id, item_number, STATUS_DATA_ENTRY, raw_description,
                json.dumps(photo_urls), submitted_by, now, now,
            ),
        )
        item_id = cur.lastrowid
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, timestamp) VALUES (?, ?, ?, ?, ?)",
            (item_id, None, STATUS_DATA_ENTRY, submitted_by, now),
        )
        return item_id


def get_item(item_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def get_item_by_message(channel_id: int, message_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM items WHERE current_message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def update_status(item_id: int, new_status: str, actor_id: int = None, note: str = None,
                   new_message_id: int = None):
    with get_conn() as conn:
        current = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
        old_status = current["status"] if current else None
        now = _now()

        fields = ["status = ?", "updated_at = ?"]
        values = [new_status, now]

        if new_message_id is not None:
            fields.append("current_message_id = ?")
            values.append(new_message_id)
        if new_status == STATUS_LISTED:
            fields.append("listed_at = ?")
            values.append(now)
        if new_status == STATUS_SOLD:
            fields.append("sold_at = ?")
            values.append(now)
        if new_status == STATUS_SHIPPED:
            fields.append("shipped_at = ?")
            values.append(now)

        values.append(item_id)
        conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id = ?", values)
        conn.execute(
            """INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, old_status, new_status, actor_id, note, now),
        )


def resubmit_item(item_id: int, raw_description: str, photo_urls: list, submitted_by: int):
    """
    Used when someone replies to a REJECTED item's "sent back for redo" card
    with corrected photos/notes. Updates the SAME row in place (same
    item_number, same audit history) and moves it back to data_entry, rather
    than create_item() making a brand-new row - which was the bug where a
    resubmission counted as a second item and the original rejected card
    was left orphaned in Data Entry forever.

    Clears the old AI fields too, since a resubmission likely has different
    photos/description and the stale AI draft shouldn't carry over.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE items SET raw_description = ?, photo_urls = ?, submitted_by = ?,
               ai_title = NULL, ai_description = NULL, ai_flags = NULL, updated_at = ?
               WHERE id = ?""",
            (raw_description, json.dumps(photo_urls), submitted_by, _now(), item_id),
        )
    update_status(item_id, STATUS_DATA_ENTRY, actor_id=submitted_by, note="Resubmitted after rejection")


def soft_delete_item(item_id: int, actor_id: int):
    """
    Used by /item delete. Keeps the row (and its full item_events history)
    but marks it deleted, so admin cleanup never silently erases audit trail.
    Does not touch Discord - the caller is responsible for deleting the
    actual message, since that requires knowing which channel it's in.
    """
    update_status(item_id, STATUS_DELETED, actor_id=actor_id, note="Deleted by admin")


def get_open_hold(item_id: int):
    """The item's currently-open hold record (resolved_at IS NULL), or None
    if it isn't on hold - see item_holds' own CREATE TABLE comment. An item
    can only have one open hold at a time."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM item_holds WHERE item_id = ? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def place_item_on_hold(item_id: int, reason: str, note: str, pre_hold_status: str, actor_id: int = None) -> int:
    """
    Records a new open hold for this item (see item_holds' own CREATE
    TABLE comment) and moves its status to STATUS_ON_HOLD - the actual
    Discord side (posting the hold card, clearing the old one) is
    cogs/item_flow.py's ItemFlow.place_item_on_hold's job; this is just the
    data half, same split as every other stage transition in this file.
    Returns the new item_holds row's id.
    """
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO item_holds (item_id, reason, note, pre_hold_status, set_by, set_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, reason, note, pre_hold_status, actor_id, now),
        )
        hold_id = cur.lastrowid
    update_status(item_id, STATUS_ON_HOLD, actor_id=actor_id, note=f"On hold: {HOLD_REASON_LABELS[reason]}")
    return hold_id


def resolve_item_hold(item_id: int, actor_id: int = None):
    """
    Marks the item's open hold resolved (timestamp + who) WITHOUT deleting
    it - the full hold history (including this one) stays queryable/
    reportable forever, per item_holds' own CREATE TABLE comment. Does NOT
    change items.status itself - the caller (ItemFlow.resolve_item_hold)
    does that once it's actually reposted the item's card in the right
    channel, same ordering every other stage transition uses (post the new
    card, then update_status). Returns the hold's pre_hold_status (so the
    caller knows where to send the item back to), or None if there was no
    open hold to resolve.
    """
    hold = get_open_hold(item_id)
    if not hold:
        return None
    with get_conn() as conn:
        conn.execute(
            "UPDATE item_holds SET resolved_by = ?, resolved_at = ? WHERE id = ?",
            (actor_id, _now(), hold["id"]),
        )
    return hold["pre_hold_status"]


def get_item_by_pallet_and_number(pallet_id: int, item_number: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM items WHERE pallet_id = ? AND item_number = ?",
            (pallet_id, item_number),
        ).fetchone()
        return dict(row) if row else None


def get_items_by_status_for_pallet(pallet_id: int, status: str):
    """Used by /ebay confirm-listed and /fb-marketplace confirm-listed's
    bulk (whole-pallet) mode."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE pallet_id = ? AND status = ? ORDER BY item_number",
            (pallet_id, status),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_items(status: str = None, include_deleted: bool = False) -> list:
    """Every item across every pallet, optionally filtered to one status -
    the source for /list items (cogs/admin_tools.py). Excludes soft-deleted
    items (STATUS_DELETED) unless status is explicitly 'deleted' or
    include_deleted is True - those aren't really "collected" anymore from
    a browsing user's perspective (see soft_delete_item: the row survives
    for audit purposes, but shouldn't clutter an ordinary inventory list)."""
    with get_conn() as conn:
        query = "SELECT * FROM items"
        conditions, params = [], []
        if status:
            conditions.append("status = ?")
            params.append(status)
        elif not include_deleted:
            conditions.append("status != ?")
            params.append(STATUS_DELETED)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY pallet_id, item_number"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------- eBay listing data --

def save_ebay_listing_data(item_id: int, ebay_title: str, category_id: str, condition_id: str,
                            price: float, item_specifics: dict, listing_format: str = "FixedPrice",
                            auction_duration: str = None, weight_lb: float = None,
                            length_in: float = None, width_in: float = None, height_in: float = None,
                            actor_id: int = None):
    """
    Upserts the eBay fields captured for this item during Queue Review
    approval. Keyed one-to-one on item_id, so re-approving (or editing later)
    just overwrites the previous values rather than accumulating rows.

    listing_format is "FixedPrice" or "Auction" (see EbayFormatSelectView in
    item_flow.py); `price` holds either the fixed price or the auction
    starting bid depending on which, matching how eBay's own *StartPrice
    field is reused for both. auction_duration (an eBay *Duration value like
    "Days_7") is only meaningful when listing_format is "Auction".

    weight_lb/length_in/width_in/height_in are required for every item
    going forward (EbayListingModal makes them mandatory fields) - they
    feed the eBay API listing's Calculated shipping and the Pirate Ship CSV
    export, since every approved item gets this captured here regardless of
    where it eventually sells. Still nullable in the schema so
    already-approved items from before this existed don't break.
    """
    with get_conn() as conn:
        now = _now()
        conn.execute(
            """INSERT INTO ebay_listing_data
                   (item_id, ebay_title, category_id, condition_id, price, listing_format,
                    auction_duration, item_specifics, weight_lb, length_in, width_in, height_in,
                    set_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                   ebay_title = excluded.ebay_title,
                   category_id = excluded.category_id,
                   condition_id = excluded.condition_id,
                   price = excluded.price,
                   listing_format = excluded.listing_format,
                   auction_duration = excluded.auction_duration,
                   item_specifics = excluded.item_specifics,
                   weight_lb = excluded.weight_lb,
                   length_in = excluded.length_in,
                   width_in = excluded.width_in,
                   height_in = excluded.height_in,
                   set_by = excluded.set_by,
                   updated_at = excluded.updated_at""",
            (item_id, ebay_title, category_id, condition_id, price, listing_format, auction_duration,
             json.dumps(item_specifics), weight_lb, length_in, width_in, height_in, actor_id, now, now),
        )


def get_ebay_listing_data(item_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ebay_listing_data WHERE item_id = ?", (item_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["item_specifics"] = json.loads(data["item_specifics"]) if data["item_specifics"] else {}
        return data


def delete_ebay_listing_data(item_id: int):
    """Drops a single item's captured eBay listing details (title/category/
    condition/price/specifics, from Queue Review's Approve step) - used
    when an admin force-moves an item back before Queue Review (see
    unwind_item_downstream_data below), so re-approving it later doesn't
    show stale details from a different pass. The only other place this
    table is touched is delete_pallet_permanently's full pallet wipe."""
    with get_conn() as conn:
        conn.execute("DELETE FROM ebay_listing_data WHERE item_id = ?", (item_id,))


# Pipeline order for unwind_item_downstream_data's "how far forward did this
# item get" comparisons - deliberately excludes the off-pipeline statuses
# (on_hold, rejected, deleted),
# which are never compared against for that purpose.
_STAGE_ORDER = [
    STATUS_DATA_ENTRY, STATUS_AUTOMATED_REVIEW, STATUS_QUEUE_REVIEW, STATUS_AWAITING_LISTING,
    STATUS_LISTED, STATUS_SOLD, STATUS_SHIPPED,
]


def _stage_rank(status: str) -> int:
    try:
        return _STAGE_ORDER.index(status)
    except ValueError:
        return len(_STAGE_ORDER)


def unwind_item_downstream_data(item_id: int, target_status: str, actor_id: int, note: str = None) -> dict:
    """
    Clears whatever downstream data no longer applies once an item is
    force-moved to target_status (see cogs/admin_tools.py's /item move-back,
    cogs/item_flow.py's ItemFlow.admin_move_item_to_stage) - this bot's one
    deliberately-backward-capable transition. Unlike every forward
    transition, which only ever adds data, this has to actively unwind it so
    a corrected item doesn't carry stale sold/listed timestamps or a
    captured eBay listing from a pass that no longer applies:

      - If the item currently has a sale price and target_status isn't
        itself 'sold', reverses the sale (via reverse_sale - a real, audited
        finance_transactions row, not a silent NULL-out).
      - Clears listed_at/sold_at/shipped_at if target_status is earlier in
        the pipeline than the stage each one belongs to.
      - Deletes its ebay_listing_data row (the title/category/condition/
        price/specifics captured at Queue Review's Approve step) if
        target_status is earlier than Awaiting Listing - the data a partner
        would need to re-enter at Approve time if this item goes through
        Queue Review again.

    Raises ValueError (never silently proceeds) if the item's sale was
    already logged to QuickBooks via /finance log-sale (a real sale_items
    row exists) - undoing THAT needs a manual QuickBooks correction first,
    which this function doesn't attempt. Returns
    {"reversed_sale_amount": float|None, "cleared_listing_data": bool} for
    the caller to report back.
    """
    item = get_item(item_id)
    if get_item_sale(item_id) and target_status != STATUS_SOLD:
        raise ValueError(
            "This item's sale was already logged to QuickBooks - void its Sales Receipt in QuickBooks, "
            "then undo it here with `/finance reverse-sale` (quickbooks_voided:True) before moving it to a "
            "different stage."
        )

    reversed_amount = None
    if item.get("sale_price") is not None and target_status != STATUS_SOLD:
        reversed_amount = reverse_sale(item_id, note or "Moved to a different stage by an admin", actor_id)

    target_rank = _stage_rank(target_status)
    cleared_listing_data = False
    if target_rank < _stage_rank(STATUS_AWAITING_LISTING) and get_ebay_listing_data(item_id):
        delete_ebay_listing_data(item_id)
        cleared_listing_data = True

    clears = []
    if target_rank < _stage_rank(STATUS_LISTED):
        clears.append("listed_at = NULL")
    if target_rank < _stage_rank(STATUS_SOLD):
        clears.append("sold_at = NULL")
    if target_rank < _stage_rank(STATUS_SHIPPED):
        clears.append("shipped_at = NULL")
    if clears:
        with get_conn() as conn:
            conn.execute(f"UPDATE items SET {', '.join(clears)} WHERE id = ?", (item_id,))

    return {"reversed_sale_amount": reversed_amount, "cleared_listing_data": cleared_listing_data}


def set_ebay_item_id(item_id: int, ebay_item_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_listing_data SET ebay_item_id = ?, updated_at = ? WHERE item_id = ?",
            (ebay_item_id, _now(), item_id),
        )


def record_ebay_category_use(category_id: str):
    """Bumps this category's pick count - called once per Queue Review
    approval, so the category select menu can sort by actual usage. Just
    being picked doesn't mean it actually worked on eBay - see
    record_ebay_category_confirmed for that stronger signal."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_category_usage (category_id, use_count) VALUES (?, 1)
               ON CONFLICT(category_id) DO UPDATE SET use_count = use_count + 1""",
            (category_id,),
        )


def record_ebay_category_confirmed(category_id: str):
    """
    Bumps this category's confirmed-working count - called only once an
    item in this category is actually confirmed live on eBay (via /ebay
    confirm-listed or a matched success row in /ebay import-results), never
    just at Queue Review pick time. Since this bot has no eBay API access
    to verify a category is real/listable ahead of time, this is the only
    trustworthy "this category actually works" signal available - the
    category select sorts confirmed categories first, ahead of ones
    that have only ever been picked (or that already failed), so the whole
    team converges on proven-working categories over time.
    """
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_category_usage (category_id, use_count, confirmed_count) VALUES (?, 0, 1)
               ON CONFLICT(category_id) DO UPDATE SET confirmed_count = confirmed_count + 1""",
            (category_id,),
        )


def get_ebay_category_counts() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT category_id, use_count FROM ebay_category_usage").fetchall()
        return {r["category_id"]: r["use_count"] for r in rows}


def get_ebay_category_confirmed_counts() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT category_id, confirmed_count FROM ebay_category_usage").fetchall()
        return {r["category_id"]: r["confirmed_count"] for r in rows}


def update_photo_public_urls(item_id: int, urls: list):
    """
    Stores each photo's durable R2 public URL, same order/index as
    photo_urls (a None in place of a URL means that photo's upload failed or
    R2 wasn't enabled at the time). Set once at Data Entry time - see
    r2_storage.py and item_flow.on_message - and read by the eBay CSV batch
    export for PicURL instead of relying on any local path or Discord link.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET photo_public_urls = ? WHERE id = ?",
            (json.dumps(urls), item_id),
        )


def get_open_items_for_reconnect():
    """
    Every item currently sitting in a stage that has live buttons attached
    to its message (queue_review, awaiting_listing, listed, sold-awaiting-
    shipment). Called once on bot startup to re-register persistent views -
    without this, buttons on any item that was mid-pipeline when the bot
    last restarted stop working.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM items
               WHERE status IN (?, ?, ?, ?) AND current_message_id IS NOT NULL""",
            (STATUS_QUEUE_REVIEW, STATUS_AWAITING_LISTING, STATUS_LISTED, STATUS_SOLD),
        ).fetchall()
        return [dict(r) for r in rows]


def get_pallet_financials(pallet_id: int):
    """
    Full financial + pipeline snapshot for a pallet:
      - items_received: items_received_override if Finance set one,
        otherwise a live count of non-deleted items logged for this pallet
      - cost: pallets.pallet_cost (the legacy manual /finance setprice lump
        sum) PLUS SUM(pallet_costs.amount) (itemized QuickBooks/Pirate Ship
        allocations - see pallet_costs' own CREATE TABLE comment) - additive,
        not a replacement, so /finance setprice keeps meaning exactly what
        it always has. Stays None (not 0) if NEITHER source has anything
        yet, same as before this table existed, so "no cost entered" still
        reads as genuinely unknown rather than a misleading $0.
      - cost_per_item: cost / items_received
      - revenue_so_far: raw sum of sale_price across every item that has one
        set (regardless of status - Finance can record a price whenever)
      - refunds_total / expenses_total: sums from finance_transactions
        (see record_refund/record_expense) - refunds and expenses recorded
        against this pallet, independent of any single item's sale_price
      - net_revenue: revenue_so_far minus refunds_total and expenses_total -
        this, not raw revenue_so_far, is what profit_so_far/cost_recovery_pct
        are based on
      - status_counts: item counts by pipeline stage, for the "where's
        everything sitting" part of the live card
      - pending_sale_value / items_pending_sale: sum of ebay_listing_data.price
        (the listing price captured for every approved item regardless of
        which platform it eventually sells on - see save_ebay_listing_data's
        docstring) across every item that's been priced but not yet sold -
        awaiting_listing and listed. Deliberately broad: an item is "priced" the moment Queue
        Review approves it, well before it's actually live anywhere, and
        this is meant to answer "what's the value of everything I've priced
        so far" regardless of exactly which pre-sale stage it's sitting in
        - including on_hold, since a hold is a pause, not a change in
        whether the item is still genuinely priced and pending a sale.
        Expected revenue still "in the pipeline", not yet realized (that's
        revenue_so_far, from actual recorded sale_price) - the two are
        deliberately separate numbers, never added together.
    This is what both /finance summary and the auto-updating pinned message
    in #pallet-discussion are built from - one function, one source of truth.
    """
    pallet = get_pallet(pallet_id)
    with get_conn() as conn:
        received_row = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE pallet_id = ? AND status != ?",
            (pallet_id, STATUS_DELETED),
        ).fetchone()
        revenue_row = conn.execute(
            "SELECT COUNT(*) AS n_priced, COALESCE(SUM(sale_price), 0) AS revenue "
            "FROM items WHERE pallet_id = ? AND sale_price IS NOT NULL",
            (pallet_id,),
        ).fetchone()
        transactions_row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN type = 'refund' THEN amount ELSE 0 END), 0) AS refunds,
                 COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expenses
               FROM finance_transactions WHERE pallet_id = ?""",
            (pallet_id,),
        ).fetchone()
        itemized_costs_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM pallet_costs WHERE pallet_id = ?",
            (pallet_id,),
        ).fetchone()
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE pallet_id = ? GROUP BY status",
            (pallet_id,),
        ).fetchall()
        pending_row = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(eld.price), 0) AS total
               FROM items i JOIN ebay_listing_data eld ON eld.item_id = i.id
               WHERE i.pallet_id = ? AND i.status IN (?, ?, ?)""",
            (pallet_id, STATUS_AWAITING_LISTING, STATUS_LISTED, STATUS_ON_HOLD),
        ).fetchone()

    items_received = pallet.get("items_received_override")
    if items_received is None:
        items_received = received_row["n"]

    manual_pallet_cost = pallet.get("pallet_cost")
    itemized_costs_total = itemized_costs_row["total"] or 0.0
    if manual_pallet_cost is None and itemized_costs_total == 0.0:
        cost = None  # genuinely nothing entered anywhere - stays unknown, not a misleading $0
    else:
        cost = (manual_pallet_cost or 0.0) + itemized_costs_total
    cost_per_item = (cost / items_received) if (cost and items_received) else None
    revenue = revenue_row["revenue"] or 0.0
    n_priced = revenue_row["n_priced"]
    refunds_total = transactions_row["refunds"] or 0.0
    expenses_total = transactions_row["expenses"] or 0.0
    net_revenue = revenue - refunds_total - expenses_total

    return {
        "pallet": pallet,
        "items_received": items_received,
        "items_received_is_override": pallet.get("items_received_override") is not None,
        "cost": cost,
        "manual_pallet_cost": manual_pallet_cost,
        "itemized_costs_total": itemized_costs_total,
        "cost_per_item": cost_per_item,
        "items_priced": n_priced,
        "revenue_so_far": revenue,
        "refunds_total": refunds_total,
        "expenses_total": expenses_total,
        "net_revenue": net_revenue,
        "avg_sale_price": (revenue / n_priced) if n_priced else None,
        "profit_so_far": (net_revenue - cost) if cost is not None else None,
        "cost_recovery_pct": (net_revenue / cost * 100) if cost else None,
        "broke_even": (cost is not None and net_revenue >= cost),
        "status_counts": {r["status"]: r["n"] for r in counts},
        "items_pending_sale": pending_row["n"],
        "pending_sale_value": pending_row["total"] or 0.0,
    }


def get_pallet_manifest_value_estimate(pallet_id: int) -> float:
    """
    Sum of ai_suggested_price across every real (non-deleted) item logged
    for a pallet - the "manifest retail value estimate" used by the
    cost-basis-exceeds-estimate early warning (see finance_utils.py). Items
    with no AI price guess yet contribute 0, not None, so a pallet still
    mid data-entry doesn't spuriously trigger a warning against its own
    still-incomplete estimate.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(ai_suggested_price), 0) AS total FROM items WHERE pallet_id = ? AND status != ?",
            (pallet_id, STATUS_DELETED),
        ).fetchone()
        return row["total"] or 0.0


def get_pallet_progress_counts() -> dict:
    """
    {'in_progress': n, 'sold_out': n} across every non-archived pallet -
    'sold_out' means the pallet has at least one real (non-deleted) item
    and every one of them has reached STATUS_SOLD or STATUS_SHIPPED;
    everything else non-archived (including a pallet with zero items yet)
    counts as still in progress. Used by /finance overview.
    """
    with get_conn() as conn:
        pallet_ids = [r["id"] for r in conn.execute("SELECT id FROM pallets WHERE archived = 0").fetchall()]
        in_progress = 0
        sold_out = 0
        for pallet_id in pallet_ids:
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM items WHERE pallet_id = ? AND status != ? GROUP BY status",
                (pallet_id, STATUS_DELETED),
            ).fetchall()
            total = sum(c["n"] for c in counts)
            done = sum(c["n"] for c in counts if c["status"] in (STATUS_SOLD, STATUS_SHIPPED))
            if total > 0 and done == total:
                sold_out += 1
            else:
                in_progress += 1
        return {"in_progress": in_progress, "sold_out": sold_out}


def get_month_to_date_financials() -> dict:
    """
    {'revenue': float, 'spend': float} across every pallet for the current
    calendar month (UTC), for /finance overview:
      - revenue: sum of items.sale_price for sales recorded (see
        record_item_sale's sale_recorded_at, NOT items.updated_at/sold_at -
        see that column's migration comment) this month.
      - spend: sum of pallet_costs.amount (QuickBooks/Pirate Ship
        allocations) PLUS finance_transactions type='expense' rows,
        created this month. Deliberately doesn't include a pallet's
        /finance setprice lump sum, which isn't dated to a specific month.
    """
    month_start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with get_conn() as conn:
        revenue_row = conn.execute(
            "SELECT COALESCE(SUM(sale_price), 0) AS total FROM items WHERE sale_recorded_at >= ?",
            (month_start,),
        ).fetchone()
        pallet_costs_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM pallet_costs WHERE created_at >= ?",
            (month_start,),
        ).fetchone()
        expenses_row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM finance_transactions WHERE type = 'expense' AND created_at >= ?",
            (month_start,),
        ).fetchone()
        return {
            "revenue": revenue_row["total"] or 0.0,
            "spend": (pallet_costs_row["total"] or 0.0) + (expenses_row["total"] or 0.0),
        }


def set_pallet_cost(pallet_id: int, cost: float, actor_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE pallets SET pallet_cost = ?, cost_set_by = ? WHERE id = ?",
            (cost, actor_id, pallet_id),
        )


def set_items_received_override(pallet_id: int, count: int):
    with get_conn() as conn:
        conn.execute("UPDATE pallets SET items_received_override = ? WHERE id = ?", (count, pallet_id))


def clear_items_received_override(pallet_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE pallets SET items_received_override = NULL WHERE id = ?", (pallet_id,))


def record_item_sale(item_id: int, price: float, platform: str, actor_id: int):
    """
    Sets (or overwrites/corrects) an item's sale price and platform. Used by
    /finance record-sale for both the first recording and later corrections -
    deliberately NOT tied to the item's pipeline status, since Finance may
    record or fix a price at a different pace than the operational Mark as
    Sold click.
    """
    with get_conn() as conn:
        now = _now()
        conn.execute(
            "UPDATE items SET sale_price = ?, sale_platform = ?, updated_at = ?, sale_recorded_at = ? WHERE id = ?",
            (price, platform, now, now, item_id),
        )
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Sale price recorded: ${price:.2f} on {platform}", now),
        )


def record_sale_platform(item_id: int, platform: str, actor_id: int):
    """
    Sets just sale_platform, right at Mark as Sold time (cogs/item_flow.py's
    move_to_sold) - price isn't known yet at that point, so this deliberately
    doesn't touch sale_price. /finance log-sale later calls record_item_sale
    with the real price once COGS is entered, which overwrites platform too
    if it was picked wrong here - this is just an early, informational value
    so the item shows up in #accounting with a platform already attached.
    """
    with get_conn() as conn:
        now = _now()
        conn.execute("UPDATE items SET sale_platform = ?, updated_at = ? WHERE id = ?", (platform, now, item_id))
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Marked sold via {platform}", now),
        )


# ------------------------------------------------------------------ sales --
# See sales/sale_items' own CREATE TABLE comments (init_db). One "sale" can
# cover a single item or a bundle sold together across one or more pallets -
# /finance log-sale (cogs/finance.py) is the only writer.

def get_item_sale(item_id: int):
    """The sale_items row (if any) an item already belongs to - the
    idempotency check /finance log-sale uses to refuse double-logging the
    same item into a second sale. None if this item hasn't been sold yet."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sale_items WHERE item_id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def create_sale(platform: str, sale_date: str, total_price: float, already_deposited: bool, created_by: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sales (platform, sale_date, total_price, already_deposited, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (platform, sale_date, total_price, int(already_deposited), created_by, _now()),
        )
        return cur.lastrowid


def add_sale_item(sale_id: int, item_id: int, allocated_price: float):
    """
    purchase_price/cogs_amount are legacy NOT NULL columns from before the
    switch to pure cash basis (a pallet's full cost is now expensed in full
    at purchase time - see _claim_charges_for_pallet in
    cogs/pallet_setup.py), always written as 0.0 here rather than migrated
    away, since SQLite can't drop a column cleanly.
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sale_items (sale_id, item_id, allocated_price, purchase_price, cogs_amount) "
            "VALUES (?, ?, ?, 0.0, 0.0)",
            (sale_id, item_id, allocated_price),
        )


def get_sale(sale_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        return dict(row) if row else None


def get_sale_items(sale_id: int):
    """Every sale_items row for this sale, joined with the item's own
    pallet/number/title for display - the shape /finance log-sale's
    confirmation summary and completion message are built from."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT si.*, i.item_number, i.pallet_id, i.raw_description, i.ai_title, p.name AS pallet_name
               FROM sale_items si
               JOIN items i ON i.id = si.item_id
               JOIN pallets p ON p.id = i.pallet_id
               WHERE si.sale_id = ?
               ORDER BY si.id""",
            (sale_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_logged_sales(limit: int = 20, platform: str = None, pallet_id: int = None) -> list:
    """
    Logged sales (sales rows - /finance log-sale and the eBay order card),
    newest first, for /finance sales. Each: the sales row plus 'items'
    ([{'pallet_name', 'item_number', 'allocated_price'}]) and 'voided'
    (the voided_sales row, or None). A reversed sale's items come from
    voided_sale_items, so it still lists what it covered. `platform`
    matches case-insensitively; `pallet_id` keeps sales that include at
    least one item from that pallet. Read-only. (`platform` matches by
    prefix too, so "Other" finds "Other: Mercari".)
    """
    with get_conn() as conn:
        query = "SELECT * FROM sales"
        params = []
        if platform:
            query += " WHERE platform LIKE ?"  # SQLite LIKE is case-insensitive for ASCII
            params.append(f"{platform}%")
        query += " ORDER BY id DESC"
        results = []
        for sale in conn.execute(query, params).fetchall():
            item_rows = conn.execute(
                """SELECT x.allocated_price, i.item_number, i.pallet_id, p.name AS pallet_name FROM (
                       SELECT item_id, allocated_price, id FROM sale_items WHERE sale_id = ?
                       UNION ALL
                       SELECT item_id, allocated_price, id FROM voided_sale_items WHERE sale_id = ?
                   ) x JOIN items i ON i.id = x.item_id JOIN pallets p ON p.id = i.pallet_id ORDER BY x.id""",
                (sale["id"], sale["id"]),
            ).fetchall()
            items = [dict(r) for r in item_rows]
            if pallet_id is not None and not any(i["pallet_id"] == pallet_id for i in items):
                continue
            voided = conn.execute("SELECT * FROM voided_sales WHERE sale_id = ?", (sale["id"],)).fetchone()
            results.append({**dict(sale), "items": items, "voided": dict(voided) if voided else None})
            if len(results) >= limit:
                break
        return results


def set_sale_receipt_id(sale_id: int, sales_receipt_id: str):
    """
    Saved the moment the Sales Receipt is created - on a cash basis this is
    the only QuickBooks artifact a sale ever produces (see
    finance_utils.post_sale_to_quickbooks), so a sale is "fully
    logged" once this is set.
    """
    with get_conn() as conn:
        conn.execute("UPDATE sales SET quickbooks_sales_receipt_id = ? WHERE id = ?", (sales_receipt_id, sale_id))


def record_refund(item_id: int, amount: float, reason: str, actor_id: int):
    """
    Logs a refund against a specific item's sale - used by /finance refund.
    Deliberately doesn't touch items.sale_price (the original sale still
    happened; refunds net out separately in get_pallet_financials via
    finance_transactions, the same "correction as a new entry, not an
    overwrite" approach as reverse_sale below), so the sale history stays
    intact even after a refund.
    """
    item = get_item(item_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO finance_transactions (pallet_id, item_id, type, amount, note, actor_id, created_at) "
            "VALUES (?, ?, 'refund', ?, ?, ?, ?)",
            (item["pallet_id"], item_id, amount, reason, actor_id, _now()),
        )
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Refund recorded: ${amount:.2f} ({reason})", _now()),
        )


def record_expense(pallet_id: int, amount: float, reason: str, actor_id: int, item_id: int = None):
    """
    Logs a cost against a pallet (packaging, listing fees, etc.) - used by
    /finance expense. item_id is optional since an expense often isn't tied
    to any one item (e.g. a box of shipping supplies for the whole pallet).
    """
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO finance_transactions (pallet_id, item_id, type, amount, note, actor_id, created_at) "
            "VALUES (?, ?, 'expense', ?, ?, ?, ?)",
            (pallet_id, item_id, amount, reason, actor_id, _now()),
        )


def reverse_sale(item_id: int, reason: str, actor_id: int) -> float:
    """
    Undoes an item's recorded sale (e.g. it turned out to be a duplicate
    entry, or the sale fell through) - used by /finance reverse-sale.
    Clears items.sale_price/sale_platform back to NULL so the item no
    longer counts as "priced" or contributes to revenue_so_far, but first
    logs a 'reversal' finance_transactions row recording what was reversed
    (and why), so that history survives even though the live item.sale_price
    field itself is now empty. Returns the price that was reversed, or None
    if the item had no sale price set to begin with (the caller should treat
    that as a no-op, not silently succeed).
    """
    item = get_item(item_id)
    old_price = item.get("sale_price")
    if old_price is None:
        return None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO finance_transactions (pallet_id, item_id, type, amount, note, actor_id, created_at) "
            "VALUES (?, ?, 'reversal', ?, ?, ?, ?)",
            (item["pallet_id"], item_id, old_price, reason, actor_id, _now()),
        )
        conn.execute(
            "UPDATE items SET sale_price = NULL, sale_platform = NULL, updated_at = ?, sale_recorded_at = NULL WHERE id = ?",
            (_now(), item_id),
        )
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Sale reversed: was ${old_price:.2f} ({reason})", _now()),
        )
    return old_price


def void_logged_sale(sale_id: int, reason: str, actor_id: int) -> list:
    """
    Undoes a sale that was LOGGED (/finance log-sale or the #ebay-orders
    Confirm & log sale - i.e. it has sales/sale_items rows, and normally a
    QuickBooks Sales Receipt) - the whole sale, every item in it, since one
    Sales Receipt covers them all. Used by /finance reverse-sale once the
    Sales Receipt has been voided in QuickBooks by hand.

    In one database transaction: marks the sale voided (voided_sales),
    moves its sale_items rows to voided_sale_items (nothing is deleted
    outright - see that table's comment), and for every item that still
    has a sale price, logs a 'reversal' finance_transactions row and clears
    the price - exactly what reverse_sale() does for an unlogged price.
    Afterwards each item can be logged in a new sale. Returns
    [{'item_id', 'reversed_price'}]; raises ValueError if the sale doesn't
    exist or was already voided.
    """
    now = _now()
    with get_conn() as conn:
        sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            raise ValueError(f"No sale #{sale_id}.")
        if conn.execute("SELECT 1 FROM voided_sales WHERE sale_id = ?", (sale_id,)).fetchone():
            raise ValueError(f"Sale #{sale_id} was already reversed.")
        rows = conn.execute("SELECT * FROM sale_items WHERE sale_id = ?", (sale_id,)).fetchall()
        conn.execute(
            "INSERT INTO voided_sales (sale_id, reason, quickbooks_sales_receipt_id, voided_by, voided_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sale_id, reason, sale["quickbooks_sales_receipt_id"], actor_id, now),
        )
        results = []
        for row in rows:
            conn.execute(
                "INSERT INTO voided_sale_items (id, sale_id, item_id, allocated_price, purchase_price, cogs_amount, "
                "voided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["id"], row["sale_id"], row["item_id"], row["allocated_price"], row["purchase_price"],
                 row["cogs_amount"], now),
            )
            conn.execute("DELETE FROM sale_items WHERE id = ?", (row["id"],))
            item = conn.execute("SELECT * FROM items WHERE id = ?", (row["item_id"],)).fetchone()
            old_price = item["sale_price"]
            if old_price is not None:
                conn.execute(
                    "INSERT INTO finance_transactions (pallet_id, item_id, type, amount, note, actor_id, created_at) "
                    "VALUES (?, ?, 'reversal', ?, ?, ?, ?)",
                    (item["pallet_id"], item["id"], old_price, f"Sale #{sale_id} reversed: {reason}", actor_id, now),
                )
                conn.execute(
                    "UPDATE items SET sale_price = NULL, sale_platform = NULL, updated_at = ?, sale_recorded_at = NULL "
                    "WHERE id = ?",
                    (now, item["id"]),
                )
            conn.execute(
                "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
                "VALUES (?, NULL, ?, ?, ?, ?)",
                (item["id"], item["status"], actor_id,
                 f"Logged sale #{sale_id} reversed ({reason})" + (f" - was ${old_price:.2f}" if old_price is not None else ""),
                 now),
            )
            results.append({"item_id": item["id"], "pallet_id": item["pallet_id"], "reversed_price": old_price})
        return results


def get_finance_transactions(pallet_id: int, limit: int = 20) -> list:
    """Most recent refunds/expenses/reversals for a pallet, newest first -
    what /finance history shows."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ft.*, i.item_number FROM finance_transactions ft
               LEFT JOIN items i ON i.id = ft.item_id
               WHERE ft.pallet_id = ? ORDER BY ft.created_at DESC LIMIT ?""",
            (pallet_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# -------------------------------------------------------- pallet_costs ----

def add_pallet_cost(pallet_id: int, cost_type: str, amount: float, source: str,
                     description: str = None, item_id: int = None,
                     quickbooks_txn_id: str = None, actor_id: int = None) -> int:
    """
    Records one itemized cost against a pallet - see pallet_costs' own
    CREATE TABLE comment for why this is separate from record_expense()
    above. cost_type is 'credit_card', 'shipping', 'supplies', or 'misc';
    source is 'quickbooks', 'pirateship', or 'manual'. Returns the new row's id.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pallet_costs
               (pallet_id, item_id, cost_type, amount, description, source, quickbooks_txn_id, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pallet_id, item_id, cost_type, amount, description, source, quickbooks_txn_id, actor_id, _now()),
        )
        return cur.lastrowid


def get_pallet_costs(pallet_id: int) -> list:
    """Every itemized cost row for a pallet, newest first - what /finance
    pallet-summary's breakdown is built from."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pallet_costs WHERE pallet_id = ? ORDER BY created_at DESC", (pallet_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_pallet_cost_breakdown(pallet_id: int) -> dict:
    """cost_type -> total amount for a pallet, e.g. {'credit_card': 120.0,
    'shipping': 34.50} - types with no rows yet are simply absent, not 0."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT cost_type, SUM(amount) AS total FROM pallet_costs WHERE pallet_id = ? GROUP BY cost_type",
            (pallet_id,),
        ).fetchall()
        return {r["cost_type"]: r["total"] for r in rows}


def has_quickbooks_txn_been_allocated(quickbooks_txn_id: str) -> bool:
    """True if this QuickBooks transaction already has a pallet_costs row
    (allocated to an existing pallet) - checked before allocating again, so
    the same charge can never be double-counted."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM pallet_costs WHERE quickbooks_txn_id = ? LIMIT 1", (quickbooks_txn_id,)
        ).fetchone()
        return row is not None


# ------------------------------------------------- awaiting_pallet_charges --

def create_awaiting_pallet_charge(quickbooks_txn_id: str, amount: float, merchant: str,
                                   txn_date: str, allocated_by: int, source: str = "quickbooks",
                                   invoice_photo_path: str = None) -> int:
    """A charge allocated to "New Pallet (not arrived yet)" - see
    awaiting_pallet_charges' own CREATE TABLE comment. Returns the new row's id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO awaiting_pallet_charges
               (quickbooks_txn_id, amount, merchant, txn_date, allocated_by, source,
                invoice_photo_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (quickbooks_txn_id, amount, merchant, txn_date, allocated_by, source,
             invoice_photo_path, _now()),
        )
        return cur.lastrowid


def create_manual_invoice_charge(amount: float, submitted_by: int) -> int:
    """A manually-submitted invoice (see cogs/finance.py's #submit-invoices
    handler) - the QuickBooks API being unavailable means there's no real
    QuickBooks transaction to key this off of, so a synthetic, permanently
    unique reference stands in for quickbooks_txn_id (NOT NULL UNIQUE).
    Otherwise behaves exactly like a QuickBooks-sourced charge - same
    "awaiting a pallet" board, same claim-at-pallet-creation flow - just
    never pushes a QuickBooks expense on claim (see claim_awaiting_pallet_charge)."""
    import uuid
    synthetic_id = f"manual-{uuid.uuid4().hex}"
    return create_awaiting_pallet_charge(
        synthetic_id, amount, merchant=None, txn_date=_now()[:10],
        allocated_by=submitted_by, source="manual",
    )


def set_awaiting_pallet_charge_message(charge_id: int, message_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE awaiting_pallet_charges SET message_id = ? WHERE id = ?", (message_id, charge_id))


def set_awaiting_pallet_charge_invoice_photo(charge_id: int, invoice_photo_path: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE awaiting_pallet_charges SET invoice_photo_path = ? WHERE id = ?",
            (invoice_photo_path, charge_id),
        )


def get_awaiting_pallet_charge(charge_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM awaiting_pallet_charges WHERE id = ?", (charge_id,)).fetchone()
        return dict(row) if row else None


def get_awaiting_pallet_charge_by_txn(quickbooks_txn_id: str):
    """Looks up an awaiting_pallet_charges row by its source QuickBooks
    transaction (claimed or not) - used to detect "this charge was already
    sent to New Pallet" so the Allocate button can't file the same charge
    there twice."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM awaiting_pallet_charges WHERE quickbooks_txn_id = ?", (quickbooks_txn_id,)
        ).fetchone()
        return dict(row) if row else None


def get_unclaimed_pallet_charges() -> list:
    """Every not-yet-claimed charge, oldest first - what a new pallet's
    creation flow offers to attach (pallet_setup.py)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM awaiting_pallet_charges WHERE claimed = 0 ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def claim_awaiting_pallet_charge(charge_id: int, pallet_id: int, actor_id: int) -> dict:
    """
    Moves an awaiting_pallet_charges row onto a real, just-created pallet:
    copies it into pallet_costs (cost_type/source matching how the charge
    got here - 'credit_card'/'quickbooks' or 'invoice'/'manual') and marks
    the awaiting_pallet_charges row claimed. Returns the charge dict (as it
    was before claiming) so the caller can push the matching QuickBooks
    entry (an Expense for a quickbooks-sourced charge, a Journal Entry for
    a manual one - see cogs/pallet_setup.py's _claim_charges_for_pallet)
    with the now-known pallet name, and remove its #awaiting-pallet-charges
    card.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM awaiting_pallet_charges WHERE id = ?", (charge_id,)).fetchone()
        charge = dict(row) if row else None
        if not charge or charge["claimed"]:
            return charge

        is_quickbooks = charge["source"] == "quickbooks"
        cost_type = "credit_card" if is_quickbooks else "invoice"
        description = charge["merchant"] or (None if is_quickbooks else "Manually submitted invoice")
        conn.execute(
            """INSERT INTO pallet_costs
               (pallet_id, cost_type, amount, description, source, quickbooks_txn_id, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pallet_id, cost_type, charge["amount"], description, charge["source"],
             charge["quickbooks_txn_id"] if is_quickbooks else None, actor_id, _now()),
        )
        conn.execute(
            "UPDATE awaiting_pallet_charges SET claimed = 1, claimed_pallet_id = ?, claimed_at = ? WHERE id = ?",
            (pallet_id, _now(), charge_id),
        )
    return charge


# ------------------------------------------------------------ quickbooks --

def save_quickbooks_connection(realm_id: str, access_token: str, refresh_token: str,
                                access_token_expires_at: str, actor_id: int = None):
    """
    Upserts the single-row OAuth connection - called both at initial
    /finance connect-quickbooks time and on every subsequent token refresh
    (QuickBooks rotates refresh_token on every use, so the new one must be
    saved each time or the next refresh would fail with the old, dead one).
    """
    with get_conn() as conn:
        now = _now()
        conn.execute(
            """INSERT INTO quickbooks_connection
                   (id, realm_id, access_token, refresh_token, access_token_expires_at, connected_by, connected_at, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   realm_id = excluded.realm_id, access_token = excluded.access_token,
                   refresh_token = excluded.refresh_token,
                   access_token_expires_at = excluded.access_token_expires_at,
                   updated_at = excluded.updated_at""",
            (realm_id, access_token, refresh_token, access_token_expires_at, actor_id, now, now),
        )


def get_quickbooks_connection():
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM quickbooks_connection WHERE id = 1").fetchone()
        return dict(row) if row else None


def has_seen_quickbooks_txn(quickbooks_txn_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM quickbooks_seen_charges WHERE quickbooks_txn_id = ?", (quickbooks_txn_id,)
        ).fetchone()
        return row is not None


def mark_quickbooks_txn_seen(quickbooks_txn_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO quickbooks_seen_charges (quickbooks_txn_id, first_seen_at) VALUES (?, ?)",
            (quickbooks_txn_id, _now()),
        )


def has_seen_ebay_order(ebay_order_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM ebay_seen_orders WHERE ebay_order_id = ?", (ebay_order_id,)
        ).fetchone()
        return row is not None


def mark_ebay_order_seen(ebay_order_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ebay_seen_orders (ebay_order_id, first_seen_at) VALUES (?, ?)",
            (ebay_order_id, _now()),
        )


# ---------------------------------------------------------- eBay finances --
# The read-only eBay mirror tables (see their CREATE TABLE comments in
# init_db) - written ONLY by /ebay sync-finances (ebay_finances.sync), read
# by /ebay net-report. Nothing here ever touches an existing bot table.

_EBAY_FIN_TXN_FIELDS = (
    "transaction_date", "transaction_status", "booking_entry", "amount", "total_fee_amount",
    "total_fee_basis_amount", "ebay_collected_tax", "order_id", "fee_type", "transaction_memo",
    "references_json", "line_fees_json", "payout_id",
)


def upsert_ebay_fin_transaction(txn: dict):
    """Inserts one parsed Finances API transaction (ebay_finances.
    parse_transaction's shape), or refreshes it in place if already synced -
    eBay updates a transaction's payout status over time, so a re-sync
    should reflect that rather than keep the first-seen snapshot."""
    now = _now()
    values = [txn.get(f) for f in _EBAY_FIN_TXN_FIELDS]
    updates = ", ".join(f"{f} = excluded.{f}" for f in _EBAY_FIN_TXN_FIELDS)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO ebay_fin_transactions (transaction_id, transaction_type, "
            f"{', '.join(_EBAY_FIN_TXN_FIELDS)}, first_synced_at, last_synced_at) "
            f"VALUES (?, ?, {', '.join('?' * len(_EBAY_FIN_TXN_FIELDS))}, ?, ?) "
            f"ON CONFLICT (transaction_id, transaction_type) DO UPDATE SET {updates}, "
            f"last_synced_at = excluded.last_synced_at",
            [txn["transaction_id"], txn["transaction_type"], *values, now, now],
        )


_EBAY_ORDER_FIELDS = (
    "order_date", "price_subtotal", "price_discount", "delivery_cost", "delivery_discount",
    "adjustment", "order_total", "total_marketplace_fee", "total_fee_basis_amount", "cancel_state",
)
_EBAY_LINE_FIELDS = (
    "ebay_order_id", "sku", "legacy_item_id", "title", "quantity", "line_item_cost", "shipping_cost",
)


def upsert_ebay_order_breakdown(order: dict):
    """Inserts/refreshes one parsed Fulfillment API order
    (ebay_finances.parse_order_breakdown's shape) and its line items, in one
    transaction. Only ever writes ebay_order_breakdowns/ebay_order_lines."""
    now = _now()
    order_updates = ", ".join(f"{f} = excluded.{f}" for f in _EBAY_ORDER_FIELDS)
    line_updates = ", ".join(f"{f} = excluded.{f}" for f in _EBAY_LINE_FIELDS)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO ebay_order_breakdowns (ebay_order_id, {', '.join(_EBAY_ORDER_FIELDS)}, "
            f"first_synced_at, last_synced_at) VALUES (?, {', '.join('?' * len(_EBAY_ORDER_FIELDS))}, ?, ?) "
            f"ON CONFLICT (ebay_order_id) DO UPDATE SET {order_updates}, last_synced_at = excluded.last_synced_at",
            [order["order_id"], *[order.get(f) for f in _EBAY_ORDER_FIELDS], now, now],
        )
        for line in order["lines"]:
            line_values = [order["order_id"], *[line.get(f) for f in _EBAY_LINE_FIELDS[1:]]]
            conn.execute(
                f"INSERT INTO ebay_order_lines (line_item_id, {', '.join(_EBAY_LINE_FIELDS)}, "
                f"first_synced_at, last_synced_at) VALUES (?, {', '.join('?' * len(_EBAY_LINE_FIELDS))}, ?, ?) "
                f"ON CONFLICT (line_item_id) DO UPDATE SET {line_updates}, last_synced_at = excluded.last_synced_at",
                [line["line_item_id"], *line_values, now, now],
            )


def get_all_ebay_order_lines() -> list:
    """Every synced order line joined with its order's breakdown - the
    report's starting point (ebay_finances.build_item_rows)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.*, o.order_date, o.price_subtotal, o.price_discount, o.delivery_cost,
                      o.delivery_discount, o.adjustment, o.order_total, o.total_marketplace_fee,
                      o.cancel_state
               FROM ebay_order_lines l
               JOIN ebay_order_breakdowns o ON o.ebay_order_id = l.ebay_order_id
               ORDER BY o.order_date, l.line_item_id"""
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_ebay_fin_transactions() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM ebay_fin_transactions ORDER BY transaction_date").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["references"] = json.loads(d["references_json"]) if d["references_json"] else []
            d["line_fees"] = json.loads(d["line_fees_json"]) if d["line_fees_json"] else {}
            result.append(d)
        return result


def get_item_by_ebay_listing_id(ebay_item_id: str):
    """The item whose confirmed eBay listing ID (ebay_listing_data.
    ebay_item_id, set by /ebay import-results / Confirm Listed) matches -
    the fallback match for an order line with no usable SKU."""
    if not ebay_item_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT i.* FROM items i JOIN ebay_listing_data eld ON eld.item_id = i.id
               WHERE eld.ebay_item_id = ? ORDER BY i.id DESC LIMIT 1""",
            (str(ebay_item_id),),
        ).fetchone()
        return dict(row) if row else None


def get_ebay_platform_items_with_sale_price(pallet_id: int = None) -> list:
    """Items recorded as sold on eBay with a sale price - used to list ones
    the eBay data couldn't be matched to. Read-only."""
    with get_conn() as conn:
        query = "SELECT * FROM items WHERE sale_price IS NOT NULL AND sale_platform = 'eBay' AND status != ?"
        params = [STATUS_DELETED]
        if pallet_id is not None:
            query += " AND pallet_id = ?"
            params.append(pallet_id)
        return [dict(r) for r in conn.execute(query + " ORDER BY pallet_id, item_number", params).fetchall()]


def get_cached_category_aspects(category_id: str):
    """(aspects list, fetched_at ISO string) for a category, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT aspects_json, fetched_at FROM ebay_category_aspects WHERE category_id = ?", (str(category_id),)
        ).fetchone()
        return (json.loads(row["aspects_json"]), row["fetched_at"]) if row else None


def save_category_aspects(category_id: str, aspects: list):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ebay_category_aspects (category_id, aspects_json, fetched_at) VALUES (?, ?, ?)",
            (str(category_id), json.dumps(aspects), _now()),
        )


def save_item_aspect_suggestion(item_id: int, category_id: str, specifics: dict):
    item = get_item(item_id)
    if not item:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO item_aspect_suggestions "
            "(item_id, item_created_at, category_id, specifics_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, item["created_at"], str(category_id), json.dumps(specifics), _now()),
        )


def get_item_aspect_suggestion(item_id: int):
    """{'category_id', 'specifics'} for this exact item, or None - ignores a
    row left from a different item that once had the same ID."""
    item = get_item(item_id)
    if not item:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM item_aspect_suggestions WHERE item_id = ? AND item_created_at = ?",
            (item_id, item["created_at"]),
        ).fetchone()
        return {"category_id": row["category_id"], "specifics": json.loads(row["specifics_json"])} if row else None


def record_ebay_order_automation(ebay_order_id: str, item_id: int, action: str) -> bool:
    """Records that the order automation took `action` for this order +
    item. Returns True if this is the first time (go ahead), False if it
    was already recorded (skip - e.g. don't re-post the same alert)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ebay_order_automation_log (ebay_order_id, item_id, action, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ebay_order_id, item_id, action, _now()),
        )
        return cur.rowcount == 1


def has_ebay_order_automation(ebay_order_id: str, item_id: int, action: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM ebay_order_automation_log WHERE ebay_order_id = ? AND item_id = ? AND action = ?",
            (ebay_order_id, item_id, action),
        ).fetchone() is not None


# ------------------------------------------------------ eBay fee expenses --
# See ebay_fee_expenses.py. Reads the eBay mirror tables; writes only the
# fee tables above plus pallet_costs (via add_pallet_cost) on a confirmed
# posting.

def get_ebay_order_lines_for_order(ebay_order_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.*, o.order_date FROM ebay_order_lines l
               JOIN ebay_order_breakdowns o ON o.ebay_order_id = l.ebay_order_id
               WHERE l.ebay_order_id = ? ORDER BY l.line_item_id""",
            (ebay_order_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_ebay_order_ids_since(order_date_from: str) -> list:
    """eBay order IDs (from synced order breakdowns) placed on/after the
    given YYYY-MM-DD, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ebay_order_id FROM ebay_order_breakdowns WHERE substr(order_date, 1, 10) >= ? ORDER BY order_date",
            (order_date_from,),
        ).fetchall()
        return [r["ebay_order_id"] for r in rows]


def get_ebay_fee_source_transactions(ebay_order_id: str, listing_ids: list) -> list:
    """Every synced eBay transaction tied to this order - by orderId, or an
    ORDER_ID reference - plus listing-level charges (an ITEM_ID reference)
    for any of `listing_ids`. Same shape as get_all_ebay_fin_transactions."""
    found = {}
    for txn in get_all_ebay_fin_transactions():
        refs = txn.get("references") or []
        if txn.get("order_id") == ebay_order_id or any(
            r["type"] == "ORDER_ID" and r["id"] == ebay_order_id for r in refs
        ) or (not txn.get("order_id") and any(r["type"] == "ITEM_ID" and r["id"] in listing_ids for r in refs)):
            found[(txn["transaction_id"], txn["transaction_type"])] = txn
    return list(found.values())


def get_posted_ebay_fee_keys() -> set:
    with get_conn() as conn:
        return {(r["transaction_id"], r["transaction_type"])
                for r in conn.execute("SELECT transaction_id, transaction_type FROM ebay_fee_postings")}


def get_carded_ebay_fee_keys() -> set:
    with get_conn() as conn:
        return {(r["transaction_id"], r["transaction_type"])
                for r in conn.execute("SELECT transaction_id, transaction_type FROM ebay_fee_card_txns")}


def mark_ebay_fee_txns_carded(ebay_order_id: str, keys: list):
    now = _now()
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO ebay_fee_card_txns (transaction_id, transaction_type, ebay_order_id, carded_at) "
            "VALUES (?, ?, ?, ?)",
            [(tid, ttype, ebay_order_id, now) for tid, ttype in keys],
        )


def record_ebay_fee_posting(ebay_order_id: str, charges: list, quickbooks_je_id: str, actor_id: int,
                            pallet_costs: list):
    """
    One confirmed "Confirm & log fees" click, in a single database
    transaction: a ledger row per eBay charge (`charges`: dicts with
    transaction_id/transaction_type/category/amount) plus the matching
    pallet_costs rows (`pallet_costs`: dicts with pallet_id/item_id/
    cost_type/amount/description) - all or nothing. Raises
    sqlite3.IntegrityError if any charge was already posted.
    """
    now = _now()
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO ebay_fee_postings (transaction_id, transaction_type, ebay_order_id, category, amount, "
            "quickbooks_je_id, posted_by, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(c["transaction_id"], c["transaction_type"], ebay_order_id, c["category"], c["amount"],
              quickbooks_je_id, actor_id, now) for c in charges],
        )
        conn.executemany(
            "INSERT INTO pallet_costs (pallet_id, item_id, cost_type, amount, description, source, "
            "quickbooks_txn_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, 'ebay', ?, ?, ?)",
            [(p["pallet_id"], p["item_id"], p["cost_type"], p["amount"], p["description"],
              quickbooks_je_id, actor_id, now) for p in pallet_costs],
        )


def get_ebay_fee_card_start_date(today: str) -> str:
    """The first day fee cards were posted automatically - set to `today`
    the first time this is asked, unchanged after."""
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO ebay_fee_card_state (id, start_date) VALUES (1, ?)", (today,))
        return conn.execute("SELECT start_date FROM ebay_fee_card_state WHERE id = 1").fetchone()["start_date"]


def sum_pallet_costs_from_source(pallet_id: int, source: str) -> float:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM pallet_costs WHERE pallet_id = ? AND source = ?",
            (pallet_id, source),
        ).fetchone()["total"] or 0.0


# ------------------------------------------- eBay payouts / refunds / account charges --
# See ebay_books.py. Mirror writes come only from ebay_finances.sync; the
# booking writes below are each one database transaction.

_EBAY_PAYOUT_FIELDS = ("payout_status", "amount", "payout_date", "transaction_count",
                       "instrument_nickname", "instrument_last4")


def upsert_ebay_payout(payout: dict):
    now = _now()
    values = [payout.get(f) for f in _EBAY_PAYOUT_FIELDS]
    updates = ", ".join(f"{f} = excluded.{f}" for f in _EBAY_PAYOUT_FIELDS)
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO ebay_payouts (payout_id, {', '.join(_EBAY_PAYOUT_FIELDS)}, first_synced_at, last_synced_at) "
            f"VALUES (?, {', '.join('?' * len(_EBAY_PAYOUT_FIELDS))}, ?, ?) "
            f"ON CONFLICT (payout_id) DO UPDATE SET {updates}, last_synced_at = excluded.last_synced_at",
            [payout["payout_id"], *values, now, now],
        )


def get_ebay_payout(payout_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ebay_payouts WHERE payout_id = ?", (payout_id,)).fetchone()
        return dict(row) if row else None


def list_ebay_payouts(since_date: str = None, limit: int = 50) -> list:
    """Newest first; since_date (YYYY-MM-DD) filters on payout_date."""
    with get_conn() as conn:
        if since_date:
            rows = conn.execute(
                "SELECT * FROM ebay_payouts WHERE substr(payout_date, 1, 10) >= ? ORDER BY payout_date DESC LIMIT ?",
                (since_date, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ebay_payouts ORDER BY payout_date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_ebay_fin_transactions_for_payout(payout_id: str) -> list:
    return [t for t in get_all_ebay_fin_transactions() if t.get("payout_id") == payout_id]


def get_ebay_fee_postings_by_key() -> dict:
    """{(transaction_id, transaction_type): posting row} for every booked eBay charge."""
    with get_conn() as conn:
        return {(r["transaction_id"], r["transaction_type"]): dict(r)
                for r in conn.execute("SELECT * FROM ebay_fee_postings")}


def get_ebay_payout_deposit(payout_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ebay_payout_deposits WHERE payout_id = ?", (payout_id,)).fetchone()
        return dict(row) if row else None


def record_ebay_payout_deposit(payout_id: str, quickbooks_txn_id: str, quickbooks_txn_type: str, amount: float,
                               bank_account_id: str, actor_id: int):
    """Raises sqlite3.IntegrityError if this payout was already recorded."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ebay_payout_deposits (payout_id, quickbooks_txn_id, quickbooks_txn_type, amount, "
            "bank_account_id, posted_by, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (payout_id, quickbooks_txn_id, quickbooks_txn_type, amount, bank_account_id, actor_id, _now()),
        )


def is_ebay_book_carded(kind: str, card_key: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM ebay_book_cards WHERE kind = ? AND card_key = ?", (kind, card_key)
        ).fetchone() is not None


def mark_ebay_book_carded(kind: str, card_keys: list):
    now = _now()
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO ebay_book_cards (kind, card_key, carded_at) VALUES (?, ?, ?)",
            [(kind, k, now) for k in card_keys],
        )


def record_ebay_refund_posting(ebay_order_id: str, charges: list, quickbooks_je_id: str, actor_id: int,
                               refunds: list):
    """
    One confirmed eBay refund card, in a single database transaction: a
    ebay_fee_postings row per eBay transaction (category 'refunds') plus a
    finance_transactions 'refund' row per item share (`refunds`: dicts with
    pallet_id/item_id/amount/note) so the pallet's net revenue drops by
    what was refunded. A credit back to the seller is a negative amount.
    Raises sqlite3.IntegrityError if any transaction was already posted.
    """
    now = _now()
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO ebay_fee_postings (transaction_id, transaction_type, ebay_order_id, category, amount, "
            "quickbooks_je_id, posted_by, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(c["transaction_id"], c["transaction_type"], ebay_order_id, c["category"], c["amount"],
              quickbooks_je_id, actor_id, now) for c in charges],
        )
        conn.executemany(
            "INSERT INTO finance_transactions (pallet_id, item_id, type, amount, note, actor_id, created_at) "
            "VALUES (?, ?, 'refund', ?, ?, ?, ?)",
            [(r["pallet_id"], r["item_id"], r["amount"], r["note"], actor_id, now) for r in refunds],
        )
        for r in refunds:
            if r.get("item_id"):
                conn.execute(
                    "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
                    "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
                    (r["item_id"], r["item_id"], actor_id, f"eBay refund recorded: ${r['amount']:.2f}", now),
                )


def get_sale_for_item(item_id: int):
    """The logged sale (sales row) an item currently belongs to, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.* FROM sales s JOIN sale_items si ON si.sale_id = s.id WHERE si.item_id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None


def get_pallet_profit_inputs(pallet_id: int) -> dict:
    """
    Read-only inputs for /finance pallet-profit (ebay_books.pallet_profit):
    itemized costs by type, refunds, every item's status / sale price /
    listing price / dates. Nothing here changes any number.
    """
    with get_conn() as conn:
        costs = {r["cost_type"]: r["total"] for r in conn.execute(
            "SELECT cost_type, COALESCE(SUM(amount), 0) AS total FROM pallet_costs WHERE pallet_id = ? GROUP BY cost_type",
            (pallet_id,),
        )}
        items = [dict(r) for r in conn.execute(
            """SELECT i.id, i.item_number, i.status, i.sale_price, i.sale_platform, i.created_at,
                      i.sale_recorded_at, eld.price AS list_price
               FROM items i LEFT JOIN ebay_listing_data eld ON eld.item_id = i.id
               WHERE i.pallet_id = ? AND i.status != ?""",
            (pallet_id, STATUS_DELETED),
        )]
    return {"costs_by_type": costs, "items": items}


# ---------------------------------------------------------- eBay price sync --

def link_ebay_listing(item_id: int, listing: dict) -> str:
    """
    Records that `item_id` is live on eBay as listing['listing_id'] - only
    ever filling in what's missing:
      'created'  - the item had no listing details; saved eBay's title,
                   category, condition and price with the listing ID
      'linked'   - listing details existed without a listing ID; ID added
      'already'  - already recorded with this same ID (nothing changed)
      'conflict' - already recorded with a DIFFERENT ID (left alone)
    """
    now = _now()
    with get_conn() as conn:
        row = conn.execute("SELECT ebay_item_id FROM ebay_listing_data WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO ebay_listing_data (item_id, ebay_title, category_id, condition_id, price,
                       listing_format, ebay_item_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, (listing.get("title") or "")[:80], listing.get("category_id") or "",
                 listing.get("condition_id") or "", listing.get("price") or 0.0,
                 "Auction" if listing.get("listing_type") == "Chinese" else "FixedPrice",
                 listing["listing_id"], now, now),
            )
            outcome = "created"
        elif not row["ebay_item_id"]:
            conn.execute("UPDATE ebay_listing_data SET ebay_item_id = ?, updated_at = ? WHERE item_id = ?",
                         (listing["listing_id"], now, item_id))
            outcome = "linked"
        elif str(row["ebay_item_id"]) == str(listing["listing_id"]):
            return "already"
        else:
            return "conflict"
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), NULL, ?, ?)",
            (item_id, item_id, f"Linked to eBay listing {listing['listing_id']}", now),
        )
        return outcome


def items_for_price_sync() -> list:
    """Listed items with a live fixed-price eBay listing: item row plus
    ebay_item_id and the bot's current price."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT i.*, eld.ebay_item_id, eld.price AS list_price FROM items i
               JOIN ebay_listing_data eld ON eld.item_id = i.id
               WHERE i.status = ? AND eld.ebay_item_id IS NOT NULL AND eld.ebay_item_id != ''
                 AND COALESCE(eld.listing_format, 'FixedPrice') = 'FixedPrice'
               ORDER BY i.id""",
            (STATUS_LISTED,),
        )]


def update_listing_price(item_id: int, new_price: float, old_price: float, note: str, actor_id: int = None):
    """Sets the item's listing price (only that field) and records the change
    in the item's history."""
    now = _now()
    with get_conn() as conn:
        conn.execute("UPDATE ebay_listing_data SET price = ?, updated_at = ? WHERE item_id = ?",
                     (new_price, now, item_id))
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id,
             f"{note}: ${old_price:.2f} -> ${new_price:.2f}" if old_price is not None else f"{note}: ${new_price:.2f}",
             now),
        )


# ------------------------------------------------------- web dashboard reads --

def web_status_counts() -> dict:
    """{status: n} across every pallet (deleted items left out)."""
    with get_conn() as conn:
        return {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE status != ? GROUP BY status", (STATUS_DELETED,))}


def web_search_items(statuses: list = None, pallet_id: int = None, q: str = None, limit: int = 60,
                     offset: int = 0) -> list:
    """Items for the web inventory/review/listing lists, newest first, with
    their pallet name and listing title/price. Read-only."""
    where, params = ["i.status != ?"], [STATUS_DELETED]
    if statuses:
        where.append(f"i.status IN ({','.join('?' * len(statuses))})")
        params += list(statuses)
    if pallet_id:
        where.append("i.pallet_id = ?")
        params.append(pallet_id)
    if q:
        like = f"%{q.strip()}%"
        where.append("(i.raw_description LIKE ? OR i.ai_title LIKE ? OR eld.ebay_title LIKE ? "
                     "OR CAST(i.item_number AS TEXT) = ?)")
        params += [like, like, like, q.strip().lstrip("#")]
    sql = (f"""SELECT i.*, p.name AS pallet_name, eld.ebay_title, eld.price AS list_price, eld.ebay_item_id
               FROM items i JOIN pallets p ON p.id = i.pallet_id
               LEFT JOIN ebay_listing_data eld ON eld.item_id = i.id
               WHERE {' AND '.join(where)} ORDER BY i.updated_at DESC, i.id DESC LIMIT ? OFFSET ?""")
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params + [limit, offset])]


def get_item_events(item_id: int, limit: int = 50) -> list:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM item_events WHERE item_id = ? ORDER BY id DESC LIMIT ?", (item_id, limit))]


# ------------------------------------------------------- Stripe checkouts --

def active_stripe_holds(item_ids: list) -> dict:
    """{item_id: session_id} for items held by a checkout that's still open
    (or paid and still processing). Expired holds don't count."""
    if not item_ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT h.item_id, h.session_id FROM stripe_holds h
                JOIN stripe_checkouts c ON c.session_id = h.session_id
                WHERE h.item_id IN ({','.join('?' * len(item_ids))})
                  AND (h.expires_at > ? OR c.status IN ('processing', 'paid', 'attention'))""",
            [*item_ids, _now()],
        ).fetchall()
        return {r["item_id"]: r["session_id"] for r in rows}


def create_stripe_checkout(session_id: str, source: str, item_ids: list, url: str, delivery: str, expires_at: str,
                           created_by: int = None):
    """Saves a new checkout and holds its items, in one transaction. Raises
    sqlite3.IntegrityError if another checkout grabbed one of the items."""
    now = _now()
    with get_conn() as conn:
        conn.execute("DELETE FROM stripe_holds WHERE expires_at <= ? AND session_id IN "
                     "(SELECT session_id FROM stripe_checkouts WHERE status IN ('open', 'expired', 'failed'))", (now,))
        conn.execute(
            "INSERT INTO stripe_checkouts (session_id, source, status, item_ids, url, delivery, expires_at, created_by, "
            "created_at, updated_at) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
            (session_id, source, json.dumps(item_ids), url, delivery, expires_at, created_by, now, now),
        )
        conn.executemany("INSERT INTO stripe_holds (item_id, session_id, expires_at) VALUES (?, ?, ?)",
                         [(i, session_id, expires_at) for i in item_ids])


def get_stripe_checkout(session_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM stripe_checkouts WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["item_ids"] = json.loads(d["item_ids"])
        return d


def update_stripe_checkout(session_id: str, **fields):
    allowed = {"status", "amount_total", "amount_tax", "amount_shipping", "stripe_fee", "sale_id", "sold_marked",
               "fees_booked", "notified", "note"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE stripe_checkouts SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ? WHERE session_id = ?",
            [*fields.values(), _now(), session_id],
        )


def release_stripe_holds(session_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM stripe_holds WHERE session_id = ?", (session_id,))


def list_stripe_checkouts(statuses: list = None, limit: int = 50) -> list:
    with get_conn() as conn:
        if statuses:
            rows = conn.execute(
                f"SELECT * FROM stripe_checkouts WHERE status IN ({','.join('?' * len(statuses))}) "
                "ORDER BY created_at DESC LIMIT ?", [*statuses, limit]).fetchall()
        else:
            rows = conn.execute("SELECT * FROM stripe_checkouts ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["item_ids"] = json.loads(d["item_ids"])
        out.append(d)
    return out


def record_stripe_event(event_id: str, event_type: str) -> bool:
    """True the first time an event is seen, False for a resend."""
    with get_conn() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO stripe_events (event_id, event_type, received_at) VALUES (?, ?, ?)",
                           (event_id, event_type, _now()))
        return cur.rowcount == 1


# ---------------------------------------------------------- web sessions --

def create_web_session(token_hash: str, discord_user_id: int, display_name: str, csrf_token: str,
                       expires_at: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (_now(),))
        conn.execute(
            "INSERT INTO web_sessions (token_hash, discord_user_id, display_name, csrf_token, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token_hash, discord_user_id, display_name, csrf_token, _now(), expires_at),
        )


def get_web_session(token_hash: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM web_sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None


def delete_web_session(token_hash: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))


# ---------------------------------------------------- #accounting worklist --

def save_accounting_entry(item_id: int, channel_id: int, message_id: int, platform: str):
    item = get_item(item_id)
    if not item:
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounting_worklist "
            "(item_id, item_created_at, channel_id, message_id, platform, posted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, item["created_at"], channel_id, message_id, platform, _now()),
        )


def get_accounting_entry(item_id: int):
    """This exact item's #accounting worklist row, or None (never a row
    left from a different item that once had the same ID)."""
    item = get_item(item_id)
    if not item:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounting_worklist WHERE item_id = ? AND item_created_at = ?",
            (item_id, item["created_at"]),
        ).fetchone()
        return dict(row) if row else None


def mark_accounting_entry_logged(item_id: int, sale_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE accounting_worklist SET sale_id = ?, logged_at = ? WHERE item_id = ?",
            (sale_id, _now(), item_id),
        )


# ---------------------------------------------------------- partner ledger --
# See partner_ledger.py. Plain records - nothing here moves money.

def add_partner(discord_user_id: int, display_name: str, added_by: int) -> dict:
    """Adds a partner, or re-activates one who was removed (keeping their
    history). Returns the partner row."""
    with get_conn() as conn:
        existing = conn.execute("SELECT * FROM partners WHERE discord_user_id = ?", (discord_user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE partners SET active = 1, display_name = ? WHERE id = ?",
                         (display_name, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO partners (discord_user_id, display_name, active, added_by, added_at) VALUES (?, ?, 1, ?, ?)",
                (discord_user_id, display_name, added_by, _now()),
            )
        return dict(conn.execute("SELECT * FROM partners WHERE discord_user_id = ?", (discord_user_id,)).fetchone())


def deactivate_partner(discord_user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE partners SET active = 0 WHERE discord_user_id = ? AND active = 1", (discord_user_id,))
        return cur.rowcount == 1


def get_partner_by_discord_id(discord_user_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM partners WHERE discord_user_id = ?", (discord_user_id,)).fetchone()
        return dict(row) if row else None


def get_partners(active_only: bool = True) -> list:
    with get_conn() as conn:
        query = "SELECT * FROM partners" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in conn.execute(query).fetchall()]


def add_partner_ledger_entry(partner_id: int, entry_type: str, amount: float, note: str, entry_date: str,
                             created_by: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO partner_ledger (partner_id, entry_type, amount, note, entry_date, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (partner_id, entry_type, round(amount, 2), note, entry_date, created_by, _now()),
        )
        return cur.lastrowid


def get_partner_ledger_entry(entry_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM partner_ledger WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None


def void_partner_ledger_entry(entry_id: int, reason: str, voided_by: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE partner_ledger SET voided_at = ?, voided_by = ?, void_reason = ? WHERE id = ? AND voided_at IS NULL",
            (_now(), voided_by, reason, entry_id),
        )
        return cur.rowcount == 1


def get_partner_ledger(partner_id: int = None, include_voided: bool = False, limit: int = None) -> list:
    """Ledger entries (newest first) joined with the partner's name."""
    query = ("SELECT l.*, p.display_name, p.discord_user_id FROM partner_ledger l "
             "JOIN partners p ON p.id = l.partner_id WHERE 1 = 1")
    params = []
    if partner_id is not None:
        query += " AND l.partner_id = ?"
        params.append(partner_id)
    if not include_voided:
        query += " AND l.voided_at IS NULL"
    query += " ORDER BY l.entry_date DESC, l.id DESC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def count_item_refunds_and_expenses(item_ids: list) -> int:
    """How many /finance refund or /finance expense rows are tied to any of
    these items - /ebay net-report flags these, since eBay's own refund/fee
    numbers may already cover the same money. Read-only."""
    if not item_ids:
        return 0
    placeholders = ",".join("?" * len(item_ids))
    with get_conn() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM finance_transactions WHERE type IN ('refund', 'expense') "
            f"AND item_id IN ({placeholders})",
            list(item_ids),
        ).fetchone()["n"]


def start_ebay_finance_sync(transactions_from: str, orders_from: str, actor_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ebay_finance_sync_log (started_at, transactions_from, orders_from, actor_id) VALUES (?, ?, ?, ?)",
            (_now(), transactions_from, orders_from, actor_id),
        )
        return cur.lastrowid


def finish_ebay_finance_sync(sync_id: int, transaction_count: int, order_count: int, error: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE ebay_finance_sync_log SET finished_at = ?, transaction_count = ?, order_count = ?, error = ? WHERE id = ?",
            (_now(), transaction_count, order_count, error, sync_id),
        )


def get_last_successful_ebay_finance_sync():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ebay_finance_sync_log WHERE finished_at IS NOT NULL AND error IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def set_shipping_info(item_id: int, recipient_name: str, address_line1: str, address_line2: str,
                       city: str, state: str, postal_code: str, country: str, actor_id: int = None):
    """
    Used by /finance set-shipping-info for non-eBay ("Other" platform) sales
    - decoupled from record_item_sale the same way price-recording is
    decoupled from the operational Mark as Sold click, since a buyer's
    address often isn't known until after the sale price is agreed on.
    Feeds /pirate-ship export-batch (see pirate_ship_csv.py); eBay sales
    never need this, since Pirate Ship pulls those directly via its own
    native eBay integration.

    Structured fields (address_line1/city/state/postal_code/country) replace
    the old single freeform shipping_address text block - that column is
    left alone here (only ever set by old code) so previously-captured
    addresses aren't lost; pirate_ship_csv.py falls back to it for rows that
    predate this structured form.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE items SET recipient_name = ?, address_line1 = ?, address_line2 = ?,
               city = ?, state = ?, postal_code = ?, country = ?, updated_at = ? WHERE id = ?""",
            (recipient_name, address_line1, address_line2, city, state, postal_code, country, _now(), item_id),
        )
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Shipping info recorded for {recipient_name}", _now()),
        )


def get_unexported_other_platform_sales():
    """
    Items sold (status = sold, not yet shipped) on any platform other than
    eBay, that haven't gone out in a /pirate-ship export-batch yet - what
    that command exports. eBay sales are excluded regardless of export
    status, since Pirate Ship pulls those directly via its own native eBay
    integration and never needs this CSV.

    Each row also carries resolved pirate_ship_weight_lb/length_in/width_in/
    height_in columns for pirate_ship_csv.py to fill in the export's
    Weight/Length/Width/Height columns: ebay_listing_data's weight/dims (set
    on Queue Review approval, human-confirmed - see EbayListingModal in
    item_flow.py) take priority when present, since most other-platform
    sales never went through that flow and only have the AI's automated-
    review estimate (items.ai_suggested_*) to fall back on.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT items.*,
                      COALESCE(eld.weight_lb, items.ai_suggested_weight_lb) AS pirate_ship_weight_lb,
                      COALESCE(eld.length_in, items.ai_suggested_length_in) AS pirate_ship_length_in,
                      COALESCE(eld.width_in, items.ai_suggested_width_in) AS pirate_ship_width_in,
                      COALESCE(eld.height_in, items.ai_suggested_height_in) AS pirate_ship_height_in
               FROM items
               LEFT JOIN ebay_listing_data eld ON eld.item_id = items.id
               WHERE items.status = ? AND items.pirate_ship_exported = 0
               AND items.sale_platform IS NOT NULL AND LOWER(items.sale_platform) != 'ebay'
               ORDER BY items.pallet_id, items.item_number""",
            (STATUS_SOLD,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_pirate_ship_exported(item_ids: list):
    with get_conn() as conn:
        conn.executemany(
            "UPDATE items SET pirate_ship_exported = 1 WHERE id = ?",
            [(item_id,) for item_id in item_ids],
        )


def get_buyer_data_purge_candidates(days: int):
    """
    Shipped items whose buyer data (recipient_name/shipping_address/the
    structured address fields) is still present and whose shipped_at is
    older than `days` days ago - what /pirate-ship purge-buyer-data
    previews and, on confirm, clears. Only non-eBay sales ever have this
    data in the first place (see set_shipping_info), so eBay sales never
    show up here.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM items
               WHERE status = ? AND shipped_at IS NOT NULL AND shipped_at < ?
               AND (recipient_name IS NOT NULL OR shipping_address IS NOT NULL OR address_line1 IS NOT NULL)
               ORDER BY pallet_id, item_number""",
            (STATUS_SHIPPED, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def purge_buyer_data(item_ids: list, actor_id: int = None):
    """
    Clears recipient_name/shipping_address and every structured address
    field on the given items - inventory identity, sale price/platform,
    and the audit trail itself (this purge is logged as its own event) are
    never touched. Irreversible once run; the caller should always show a
    preview and require an explicit confirm before calling this.
    """
    with get_conn() as conn:
        for item_id in item_ids:
            conn.execute(
                """UPDATE items SET recipient_name = NULL, shipping_address = NULL,
                   address_line1 = NULL, address_line2 = NULL, city = NULL, state = NULL,
                   postal_code = NULL, country = NULL, updated_at = ? WHERE id = ?""",
                (_now(), item_id),
            )
            conn.execute(
                "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
                "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
                (item_id, item_id, actor_id, "Buyer shipping data purged (retention policy)", _now()),
            )


def get_photo_purge_candidates(days: int):
    """
    Sold/shipped items whose sold_at is older than `days` days ago and
    still have R2-hosted photo URLs on file - what /admin purge-old-photos
    previews and, on confirm, clears. The retention window exists so
    photos stay available for a buffer after a sale in case of a return/
    refund (see config.PHOTO_RETENTION_DAYS_AFTER_SALE); after that,
    nothing left in this bot still needs a durable public URL for these
    photos (the listing's done, Pirate Ship doesn't use photos), so
    keeping them in R2 indefinitely is just paying storage for nothing.
    Local photo_urls (disk copies) are untouched either way - only the R2
    copy is ever a purge candidate.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM items
               WHERE status IN (?, ?) AND sold_at IS NOT NULL AND sold_at < ?
               AND photo_public_urls IS NOT NULL
               ORDER BY pallet_id, item_number""",
            (STATUS_SOLD, STATUS_SHIPPED, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_photo_public_urls(item_ids: list, actor_id: int = None):
    """
    Clears photo_public_urls (the R2-hosted links) for the given items -
    call this AFTER the matching R2 objects have actually been deleted
    (see r2_storage.delete_photos), never before, so this column never
    claims a URL is gone when the object might still exist (or vice versa).
    Local photo_urls (disk paths) are left alone, so a Discord item card
    still renders if one's ever reposted; only the durable, publicly-
    linkable R2 copy is gone. Irreversible once run; the caller should
    always show a preview and require an explicit confirm before calling
    this, same as purge_buyer_data.
    """
    with get_conn() as conn:
        for item_id in item_ids:
            conn.execute(
                "UPDATE items SET photo_public_urls = NULL, updated_at = ? WHERE id = ?",
                (_now(), item_id),
            )
            conn.execute(
                "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
                "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
                (item_id, item_id, actor_id, "R2-hosted photos purged (retention policy)", _now()),
            )


def get_items_with_photo_url_prefix(prefix: str) -> list:
    """
    Items whose photo_public_urls contains at least one URL starting with
    `prefix` (normalized the same way r2_storage.upload_photo builds a URL -
    a trailing slash stripped, then exactly one added back). Used by /admin
    rewrite-photo-url-base to preview/apply a one-time correction across
    already-stored URLs after fixing a wrong R2_PUBLIC_URL_BASE in .env -
    changing that env var only affects newly-uploaded photos going forward,
    since each URL is written once into the database at upload time, never
    rebuilt from config on read.
    """
    normalized_prefix = prefix.rstrip("/") + "/"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, item_number, photo_public_urls FROM items WHERE photo_public_urls IS NOT NULL"
        ).fetchall()
    matches = []
    for row in rows:
        urls = json.loads(row["photo_public_urls"] or "[]")
        if any(u and u.startswith(normalized_prefix) for u in urls):
            matches.append(dict(row))
    return matches


def rewrite_photo_url_prefix(old_prefix: str, new_prefix: str, actor_id: int = None) -> int:
    """
    Rewrites every stored photo_public_urls entry starting with old_prefix
    to start with new_prefix instead - a plain string substitution, safe
    only because the object keys themselves (the path after the domain) are
    identical between old and new - see get_items_with_photo_url_prefix.
    Only touches photo_public_urls; local photo_urls (disk paths) and every
    other item field are untouched. Irreversible; the caller should always
    show a preview and require an explicit confirm before calling this,
    same as clear_photo_public_urls/purge_buyer_data. Returns how many
    items were updated (an item's whole URL list is rewritten together, so
    this isn't a per-photo count).
    """
    normalized_old = old_prefix.rstrip("/") + "/"
    normalized_new = new_prefix.rstrip("/") + "/"
    items = get_items_with_photo_url_prefix(old_prefix)
    now = _now()
    with get_conn() as conn:
        for item in items:
            urls = json.loads(item["photo_public_urls"] or "[]")
            rewritten = [
                normalized_new + u[len(normalized_old):] if u and u.startswith(normalized_old) else u
                for u in urls
            ]
            conn.execute(
                "UPDATE items SET photo_public_urls = ?, updated_at = ? WHERE id = ?",
                (json.dumps(rewritten), now, item["id"]),
            )
            conn.execute(
                "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
                "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
                (item["id"], item["id"], actor_id, "R2 photo URL base corrected (fixed R2_PUBLIC_URL_BASE)", now),
            )
    return len(items)


def set_finance_message(pallet_id: int, message_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE pallets SET finance_message_id = ? WHERE id = ?", (message_id, pallet_id))


def save_ai_review(item_id: int, title: str, description: str, flags: str,
                    suggested_category: str = None, suggested_price: float = None,
                    suggested_weight_lb: float = None, suggested_length_in: float = None,
                    suggested_width_in: float = None, suggested_height_in: float = None,
                    backend_failed: bool = False):
    """
    suggested_category/suggested_price/suggested_weight_lb/suggested_*_in
    are Automated Review's own guesses (see ai_review.SYSTEM_PROMPT) -
    suggested_category is a short free-text product-type phrase (e.g.
    "cordless drill"), not an eBay category name or ID; item_flow.py
    resolves it against eBay's real taxonomy (ebay_taxonomy.search()) to
    pre-select a real leaf category at Queue Review time. suggested_price
    is a rough estimate from the model's general knowledge, not real market
    data, and the weight/dimensions are a rough visual estimate, not an
    actual measurement. All of these are just pre-fills for Queue Review's
    approval flow - never authoritative,
    always human-editable/overridable before Approve.

    backend_failed mirrors ai_review._fallback's own "ai_backend_failed"
    marker (True only on a fallback result, never on a real one from
    either backend) - sets/clears items.ai_review_failed, which
    ai_retry_backlog_loop (cogs/item_flow.py) polls to automatically retry
    this item once the backend's reachable again.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE items SET ai_title = ?, ai_description = ?, ai_flags = ?,
               ai_suggested_category = ?, ai_suggested_price = ?, ai_suggested_weight_lb = ?,
               ai_suggested_length_in = ?, ai_suggested_width_in = ?, ai_suggested_height_in = ?,
               ai_review_failed = ?, updated_at = ? WHERE id = ?""",
            (title, description, flags, suggested_category, suggested_price, suggested_weight_lb,
             suggested_length_in, suggested_width_in, suggested_height_in,
             int(backend_failed), _now(), item_id),
        )


def update_description(item_id: int, new_description: str):
    """
    Also clears ai_review_failed - a manual edit (Queue Review's Edit
    button) already fixed the description by hand, so there's nothing
    left for ai_retry_backlog_loop to automatically retry, and an
    unattended retry overwriting this edit later would be a real bug.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET ai_description = ?, ai_review_failed = 0, updated_at = ? WHERE id = ?",
            (new_description, _now(), item_id),
        )


def get_items_needing_ai_retry(limit: int) -> list:
    """
    Items still sitting in Queue Review whose last AI review was a fallback
    (ai_review_failed, set by save_ai_review) - oldest first, capped at
    `limit` so ai_retry_backlog_loop never processes an entire overnight
    backlog in one tick. Deliberately scoped to STATUS_QUEUE_REVIEW only,
    same as Queue Review's own manual "Re-review (AI)" button - an item
    already approved/listed/sold is never touched by this.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE status = ? AND ai_review_failed = 1 "
            "ORDER BY updated_at ASC LIMIT ?",
            (STATUS_QUEUE_REVIEW, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_stale_listed_items(days_threshold: int):
    """Items still STATUS_LISTED for >= days_threshold days that haven't been alerted on yet."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM items
               WHERE status = ? AND stale_alert_sent = 0
               AND listed_at IS NOT NULL
               AND julianday('now') - julianday(listed_at) >= ?""",
            (STATUS_LISTED, days_threshold),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_stale_alert_sent(item_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE items SET stale_alert_sent = 1 WHERE id = ?", (item_id,))


def get_pallet_summary(pallet_id: int):
    """Quick counts by status per pallet - used by /pallet-list."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE pallet_id = ? GROUP BY status",
            (pallet_id,),
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


# -------------------------------------------------------------- DANGER ZONE --

def get_all_category_ids() -> list[int]:
    """Every pallet category_id ever created - used by /db-wipe to clean up
    Discord after the database itself is wiped."""
    with get_conn() as conn:
        rows = conn.execute("SELECT category_id FROM pallets").fetchall()
        return [r["category_id"] for r in rows]


def wipe_database():
    """
    Drops and recreates every pallet/item-scoped table, permanently erasing
    all pallets, items, event history, and financial records. This is called
    ONLY after the multi-step confirmation in admin_tools.py (typed
    confirmation phrase + role + Discord Administrator permission) - this
    function itself does no confirmation of its own and will wipe
    immediately when called, so nothing should call it directly outside
    that confirmed flow.

    Deliberately NOT dropped - infrastructure/company-level state that has
    nothing to do with any specific pallet's data, and would just have to be
    redone/reconnected for no reason after a reset:
      - shared_channels: the one-time /setup shared-channels channel IDs
      - quickbooks_connection: the OAuth token for the connected company
      - quickbooks_seen_charges: poll dedup ledger, tied to the bank
        connection, not to any pallet
      - ebay_seen_orders: same idea as quickbooks_seen_charges, for the
        eBay order auto-sync poll loop - tied to the eBay connection, not
        to any pallet
      - ebay_fin_transactions / ebay_order_breakdowns / ebay_order_lines /
        ebay_finance_sync_log: the read-only mirror of eBay's own records
        (/ebay sync-finances) - eBay-account-level, not pallet data. They
        hold no item IDs (matching happens at report time, and only to
        items created before the order - see
        ebay_finances.match_line_to_item), so they can't attach to a
        re-created pallet's items after a wipe.
    """
    with get_conn() as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS item_events;
            DROP TABLE IF EXISTS item_holds;
            DROP TABLE IF EXISTS ebay_listing_data;
            DROP TABLE IF EXISTS ebay_batches;
            DROP TABLE IF EXISTS ebay_category_usage;
            DROP TABLE IF EXISTS finance_transactions;
            DROP TABLE IF EXISTS pallet_costs;
            DROP TABLE IF EXISTS awaiting_pallet_charges;
            DROP TABLE IF EXISTS items;
            DROP TABLE IF EXISTS channel_map;
            DROP TABLE IF EXISTS pallets;
            """
        )
    init_db()  # immediately recreate empty tables so the bot keeps working
