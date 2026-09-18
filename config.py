"""
Central configuration for the pallet tracking bot.
Edit these values to match your actual Discord server setup.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---- Secrets (set these in a .env file, never hardcode them) ----
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # optional - see AI_ENABLED below
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))  # your server's ID

# Whether the Automated Review (AI) step is active. Automatically turns on
# the moment a real ANTHROPIC_API_KEY is added to .env - nothing else needs
# to change. While this is False, items skip straight from Data Entry to
# Queue Review, carrying forward the raw note as-is.
AI_ENABLED = bool(ANTHROPIC_API_KEY)

# ---- Fixed channel name used for the "click to start a new pallet" button ----
HUB_CHANNEL_NAME = "new-pallet-tracking"

# ---- Channel architecture -------------------------------------------------
# Discord servers cap out at 500 channels total. Creating a full set of stage
# channels PER PALLET doesn't scale - a few dozen pallets would burn through
# that fast. So only two channels are created per pallet (inside that
# pallet's own category); everything else is ONE shared channel used by
# every pallet at once, with each item's card clearly labeled by pallet.

# Created fresh inside every new pallet's category:
DISCUSSION_CHANNEL_NAME = "pallet-discussion"  # created first, so it's the top channel in the category
PER_PALLET_CHANNELS = [
    DISCUSSION_CHANNEL_NAME,
    "data-entry",
]

# Created ONCE for the whole server (via /setup-shared-channels), shared by
# every pallet. An item's embed always shows which pallet it belongs to.
SHARED_STAGE_CHANNELS = [
    "automated-review",
    "queue-review",
    "awaiting-listing",
    "pending-ebay-upload",
    "listed",
    "sold",
    "10-day-alerts",
]
SHARED_PIPELINE_CATEGORY_NAME = "Shared Pallet Pipeline"

# Full pipeline order, per-pallet + shared combined, in the order an item
# actually moves through them. Used wherever code needs "all stages" (the
# NewPalletModal only creates PER_PALLET_CHANNELS itself - shared ones must
# already exist via /setup-shared-channels).
STAGE_CHANNELS = ["data-entry"] + SHARED_STAGE_CHANNELS

# ---- Role names. Create these roles in your server ahead of time. ----
ROLE_DATA_ENTRY = "Data Entry"
ROLE_QUEUE_REVIEW = "Queue Review"
ROLE_LISTING_MGMT = "Listing Management"
ROLE_PURCHASE_MGMT = "Purchase Management"
ROLE_FINANCE_MGMT = "Finance Management"
ROLE_ADMIN = "Pallet Admin"

# Everyone who should see general pallet chat and the live finance/status
# card in #pallet-discussion.
DISCUSSION_CHANNEL_ROLES = [
    ROLE_DATA_ENTRY, ROLE_QUEUE_REVIEW, ROLE_LISTING_MGMT,
    ROLE_PURCHASE_MGMT, ROLE_FINANCE_MGMT,
]  # Admin always included automatically

# Which roles can post/act in which channel. Admins implicitly have access
# everywhere (handled in code, not listed here).
CHANNEL_ROLE_PERMISSIONS = {
    "data-entry": [ROLE_DATA_ENTRY],
    "automated-review": [],  # bot-only channel, humans don't need post access
    "queue-review": [ROLE_QUEUE_REVIEW],
    "awaiting-listing": [ROLE_LISTING_MGMT],
    "pending-ebay-upload": [ROLE_LISTING_MGMT],
    "listed": [ROLE_LISTING_MGMT],
    "sold": [ROLE_LISTING_MGMT],
    "10-day-alerts": [ROLE_LISTING_MGMT, ROLE_FINANCE_MGMT],
}

# ---- Onboarding text ----
# Posted (and pinned) as the first message in each channel when it's created,
# and also set as the channel topic, so anyone new can open a channel and
# immediately understand what happens there without asking.
CHANNEL_INFO = {
    "pallet-discussion": (
        "General chat about this specific pallet. Not part of the item pipeline - "
        "nothing here moves automatically. The pinned message above is a live "
        "financial/status card that updates itself as items move and prices are set."
    ),
    "data-entry": (
        "**Data Entry role** posts here. One message per item: attach photo(s) and "
        "type a short note (e.g. \"cordless drill, has case, untested\") in the same "
        "message, then send. The bot logs it, removes it from this channel, and "
        "sends it into the shared Automated Review channel automatically. "
        "If an item comes back here rejected from Queue Review, REPLY to that "
        "specific card with the corrected photo(s)/note - don't post a fresh "
        "message, or it'll be logged as a second, separate item."
    ),
    "automated-review": (
        "Nothing to do here - the AI reviews each incoming item automatically (or "
        "items pass straight through if AI isn't configured) and forwards them to "
        "Queue Review. This channel is shared across every pallet."
    ),
    "queue-review": (
        "**Queue Review role** works here. Every pallet's items waiting for review "
        "land in this one shared channel - check the embed's Pallet field to see "
        "which pallet each item is from. Approve, Edit the description, or Reject "
        "(sends back to that pallet's own Data Entry channel)."
    ),
    "awaiting-listing": (
        "**Listing Management role** works here. These items were approved in Queue Review "
        "(with eBay title/category/condition/price already captured). Click **Add to eBay "
        "Batch** to append it to the CSV batch for eBay's bulk upload tool (or **List on eBay "
        "(API)** if the direct API is enabled), or **Mark Listed (Other)** once you've listed "
        "it manually elsewhere (FB Marketplace, website, etc.)."
    ),
    "pending-ebay-upload": (
        "**Listing Management role** - view only, nothing to click here. Items land here after "
        "**Add to eBay Batch** on an Awaiting Listing card. They're sitting in the CSV batch "
        "waiting for someone to run `/ebay export-batch` and upload it in eBay Seller Hub. Once "
        "eBay actually shows them live, an admin runs `/ebay confirm-listed` to move them into "
        "#listed - there's no live API to detect that automatically."
    ),
    "listed": (
        "**Listing Management role** works here. These items are currently live for "
        "sale somewhere. Click Mark as Sold once one sells."
    ),
    "sold": (
        "Sold items land here. Click Mark as Shipped once it's been packed and sent "
        "to the buyer, just so there's a record of what's shipped vs. sold-but-not-out-yet."
    ),
    "10-day-alerts": (
        "No action needed most of the time - this channel gets an automatic ping "
        "whenever an item has been sitting in Listed for 10+ days, across every "
        "pallet, as a nudge to consider a price drop or refreshed photos."
    ),
}

# ---- Business logic ----
DAYS_BEFORE_STALE_ALERT = 10
STALE_CHECK_INTERVAL_HOURS = 12  # how often the background job checks for stale listings

# ---- Database ----
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/pallet_tracker.db")

# ---- AI review model ----
AI_MODEL = "claude-sonnet-5"

# All possible item status values, in pipeline order - used by /pallet-list
# to display a consistent stage-by-stage count regardless of which stages
# currently have items in them. Must match the STATUS_* constants in database.py.
STAGE_ORDER_FOR_STATUS = [
    "data_entry", "automated_review", "queue_review", "awaiting_listing",
    "pending_ebay_upload", "listed", "sold", "shipped", "rejected", "deleted",
]

# ---- eBay (optional direct API path - see ebay_api.py) ----
EBAY_APP_ID = os.getenv("EBAY_APP_ID")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID")
EBAY_DEV_ID = os.getenv("EBAY_DEV_ID")
EBAY_USER_TOKEN = os.getenv("EBAY_USER_TOKEN")

# Whether the direct eBay API listing path ("List on eBay (API)" on Awaiting
# Listing cards) is usable. False - the normal case, since eBay developer API
# approval is pending - until all four eBay credentials above are set; until
# then that button doesn't even show up, and the only way to get an item onto
# eBay is the CSV batch path (see ebay_csv.py and /ebay export-batch).
EBAY_ENABLED = bool(EBAY_APP_ID and EBAY_CERT_ID and EBAY_DEV_ID and EBAY_USER_TOKEN)

# eBay's Condition ID values (https://developer.ebay.com - ConditionEnum),
# restricted to the ones that actually apply to liquidation-pallet resale.
# Shown as a Discord select menu when approving an item in Queue Review.
EBAY_CONDITIONS = [
    ("1000", "New"),
    ("1500", "New other (see details)"),
    ("2000", "Certified Refurbished"),
    ("2500", "Seller Refurbished"),
    ("3000", "Used"),
    ("4000", "Very Good"),
    ("5000", "Good"),
    ("6000", "Acceptable"),
    ("7000", "For parts or not working"),
]
EBAY_CONDITION_LABELS = dict(EBAY_CONDITIONS)

# ---- eBay CSV batch (Seller Hub bulk upload / File Exchange fallback) ----
# Accumulates one row per item added via "Add to eBay Batch" until an admin
# runs /ebay export-batch, which hands it over as a Discord attachment and
# archives + clears it so the next batch starts clean.
EBAY_BATCH_CSV_PATH = os.getenv("EBAY_BATCH_CSV_PATH", "data/ebay_batch.csv")
EBAY_BATCH_ARCHIVE_DIR = os.getenv("EBAY_BATCH_ARCHIVE_DIR", "data/ebay_batch_archive")

# ---- Database wipe confirmation phrase ----
# The exact text an admin must type to confirm a full database wipe.
# Deliberately not configurable via .env - changing it requires editing code,
# which is itself a small speed bump against wiping by accident.
DB_WIPE_CONFIRMATION_PHRASE = "DELETE EVERYTHING"
