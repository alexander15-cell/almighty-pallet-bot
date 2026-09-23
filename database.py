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
    or removed entirely via /item-delete, which sets status to "deleted"
    rather than actually removing the row - so the audit trail survives)

Note: pallet cost and item sale price ARE tracked here again (Purchase
Management sets cost via /setprice, Finance Management records sale prices
via /finance record-sale) - this is a deliberate reversal of an earlier
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
STATUS_PENDING_EBAY_UPLOAD = "pending_ebay_upload"  # added to the eBay CSV batch, not yet confirmed live
STATUS_LISTED = "listed"
STATUS_SOLD = "sold"
STATUS_SHIPPED = "shipped"
STATUS_REJECTED = "rejected"  # sent back to data entry for redo
STATUS_DELETED = "deleted"    # soft-deleted via /item-delete - row kept for audit trail


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
            -- via /setup-shared-channels and used by every pallet, instead of
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
                ai_suggested_category TEXT,          -- eBay category NAME (from config.EBAY_CATEGORIES) Automated Review guessed, pre-selects the Queue Review dropdown
                ai_suggested_price  REAL,             -- rough AI price guess (general knowledge, not real market data) - pre-fills the Queue Review price field, always human-editable
                submitted_by        INTEGER,
                current_message_id  INTEGER,            -- message representing item in its CURRENT channel
                listed_at           TEXT,
                sold_at             TEXT,
                sale_price          REAL,
                sale_platform       TEXT,
                recipient_name      TEXT,               -- non-eBay ("Other" platform) sales only - for the Pirate Ship CSV export
                shipping_address    TEXT,               -- legacy freeform address (pre-structured-fields items) - kept for old rows, see address_line1 etc. below
                address_line1       TEXT,               -- non-eBay ("Other" platform) sales only - structured shipping address for the Pirate Ship CSV export
                address_line2       TEXT,
                city                TEXT,
                state               TEXT,
                postal_code         TEXT,
                country             TEXT,
                pirate_ship_exported INTEGER DEFAULT 0,  -- set once this item's shipping info has gone out in a /pirate-ship export-batch, so it isn't exported twice
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
                set_by          INTEGER,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            -- One row per eBay category ID that's ever been picked during
            -- Queue Review approval. Lets the category select menu in
            -- item_flow.py sort config.EBAY_CATEGORIES by actual usage
            -- (most-picked first) instead of a fixed order.
            CREATE TABLE IF NOT EXISTS ebay_category_usage (
                category_id     TEXT PRIMARY KEY,
                use_count       INTEGER NOT NULL DEFAULT 0
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
    Used by /item-delete. Keeps the row (and its full item_events history)
    but marks it deleted, so admin cleanup never silently erases audit trail.
    Does not touch Discord - the caller is responsible for deleting the
    actual message, since that requires knowing which channel it's in.
    """
    update_status(item_id, STATUS_DELETED, actor_id=actor_id, note="Deleted by admin")


def get_item_by_pallet_and_number(pallet_id: int, item_number: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM items WHERE pallet_id = ? AND item_number = ?",
            (pallet_id, item_number),
        ).fetchone()
        return dict(row) if row else None


def get_items_by_status_for_pallet(pallet_id: int, status: str):
    """Used by /ebay confirm-listed's bulk (whole-pallet) mode."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE pallet_id = ? AND status = ? ORDER BY item_number",
            (pallet_id, status),
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------- eBay listing data --

def save_ebay_listing_data(item_id: int, ebay_title: str, category_id: str, condition_id: str,
                            price: float, item_specifics: dict, listing_format: str = "FixedPrice",
                            auction_duration: str = None, actor_id: int = None):
    """
    Upserts the eBay fields captured for this item during Queue Review
    approval. Keyed one-to-one on item_id, so re-approving (or editing later)
    just overwrites the previous values rather than accumulating rows.

    listing_format is "FixedPrice" or "Auction" (see EbayFormatSelectView in
    item_flow.py); `price` holds either the fixed price or the auction
    starting bid depending on which, matching how eBay's own *StartPrice
    field is reused for both. auction_duration (an eBay *Duration value like
    "Days_7") is only meaningful when listing_format is "Auction".
    """
    with get_conn() as conn:
        now = _now()
        conn.execute(
            """INSERT INTO ebay_listing_data
                   (item_id, ebay_title, category_id, condition_id, price, listing_format,
                    auction_duration, item_specifics, set_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET
                   ebay_title = excluded.ebay_title,
                   category_id = excluded.category_id,
                   condition_id = excluded.condition_id,
                   price = excluded.price,
                   listing_format = excluded.listing_format,
                   auction_duration = excluded.auction_duration,
                   item_specifics = excluded.item_specifics,
                   set_by = excluded.set_by,
                   updated_at = excluded.updated_at""",
            (item_id, ebay_title, category_id, condition_id, price, listing_format, auction_duration,
             json.dumps(item_specifics), actor_id, now, now),
        )


def get_ebay_listing_data(item_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ebay_listing_data WHERE item_id = ?", (item_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["item_specifics"] = json.loads(data["item_specifics"]) if data["item_specifics"] else {}
        return data


def record_ebay_category_use(category_id: str):
    """Bumps this category's pick count - called once per successful Queue
    Review approval, so the category select menu can sort by actual usage."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ebay_category_usage (category_id, use_count) VALUES (?, 1)
               ON CONFLICT(category_id) DO UPDATE SET use_count = use_count + 1""",
            (category_id,),
        )


def get_ebay_category_counts() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT category_id, use_count FROM ebay_category_usage").fetchall()
        return {r["category_id"]: r["use_count"] for r in rows}


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
      - cost_per_item: pallet_cost / items_received
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
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM items WHERE pallet_id = ? GROUP BY status",
            (pallet_id,),
        ).fetchall()

    items_received = pallet.get("items_received_override")
    if items_received is None:
        items_received = received_row["n"]

    cost = pallet.get("pallet_cost")
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
        conn.execute(
            "UPDATE items SET sale_price = ?, sale_platform = ?, updated_at = ? WHERE id = ?",
            (price, platform, _now(), item_id),
        )
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Sale price recorded: ${price:.2f} on {platform}", _now()),
        )


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
            "UPDATE items SET sale_price = NULL, sale_platform = NULL, updated_at = ? WHERE id = ?",
            (_now(), item_id),
        )
        conn.execute(
            "INSERT INTO item_events (item_id, from_status, to_status, actor_id, note, timestamp) "
            "VALUES (?, NULL, (SELECT status FROM items WHERE id = ?), ?, ?, ?)",
            (item_id, item_id, actor_id, f"Sale reversed: was ${old_price:.2f} ({reason})", _now()),
        )
    return old_price


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
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM items
               WHERE status = ? AND pirate_ship_exported = 0
               AND sale_platform IS NOT NULL AND LOWER(sale_platform) != 'ebay'
               ORDER BY pallet_id, item_number""",
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


def set_finance_message(pallet_id: int, message_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE pallets SET finance_message_id = ? WHERE id = ?", (message_id, pallet_id))


def save_ai_review(item_id: int, title: str, description: str, flags: str,
                    suggested_category: str = None, suggested_price: float = None):
    """
    suggested_category/suggested_price are Automated Review's own guesses
    (see ai_review.SYSTEM_PROMPT) - suggested_category is an eBay category
    NAME from config.EBAY_CATEGORIES (not an ID; item_flow.py's category
    select maps it to an ID at Queue Review time), and suggested_price is a
    rough estimate from the model's general knowledge, not real market data.
    Both are just pre-fills for Queue Review's approval flow - never
    authoritative, always human-editable/overridable before Approve.
    """
    with get_conn() as conn:
        conn.execute(
            """UPDATE items SET ai_title = ?, ai_description = ?, ai_flags = ?,
               ai_suggested_category = ?, ai_suggested_price = ?, updated_at = ? WHERE id = ?""",
            (title, description, flags, suggested_category, suggested_price, _now(), item_id),
        )


def update_description(item_id: int, new_description: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET ai_description = ?, updated_at = ? WHERE id = ?",
            (new_description, _now(), item_id),
        )


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
    Drops and recreates every table, permanently erasing all pallets, items,
    and event history. This is called ONLY after the multi-step confirmation
    in admin_tools.py (typed confirmation phrase + role + Discord Administrator
    permission) - this function itself does no confirmation of its own and
    will wipe immediately when called, so nothing should call it directly
    outside that confirmed flow.
    """
    with get_conn() as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS item_events;
            DROP TABLE IF EXISTS ebay_listing_data;
            DROP TABLE IF EXISTS ebay_category_usage;
            DROP TABLE IF EXISTS items;
            DROP TABLE IF EXISTS channel_map;
            DROP TABLE IF EXISTS pallets;
            """
        )
    init_db()  # immediately recreate empty tables so the bot keeps working
