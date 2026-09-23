"""
Central configuration for the pallet tracking bot.
Edit these values to match your actual Discord server setup.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---- Secrets (set these in a .env file, never hardcode them) ----
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # optional - only used by the "anthropic" backend below
# Every int()/float() env var below uses `os.getenv("X") or "default"`, not
# os.getenv("X", "default") - the two-arg form only falls back when the key
# is entirely ABSENT from .env, not when it's present but left blank (e.g.
# "DISCORD_GUILD_ID=" from a freshly-copied .env.example), which crashed
# the bot at startup with a ValueError instead of using the default.
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or "0")  # your server's ID

# ---- Automated Review (AI) backend ----
# "anthropic" (default) - Claude's cloud vision API, requires ANTHROPIC_API_KEY.
# "ollama"    - a local Ollama server (https://ollama.com), no API key or
#               per-item cost, but noticeably lower quality/speed than the
#               cloud model (see ai_review.py). Switch back any time by
#               changing this value in .env and restarting.
AI_REVIEW_BACKEND = os.getenv("AI_REVIEW_BACKEND", "anthropic").strip().lower()

# Only used when AI_REVIEW_BACKEND == "ollama" - a local Ollama server
# running a vision-capable model. No API key needed since it's a local HTTP
# server; just make sure Ollama is running and the model is pulled
# (`ollama pull moondream`) before switching AI_REVIEW_BACKEND to "ollama".
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "moondream")

# Context window (in tokens) for the Ollama backend, passed as a per-request
# "num_ctx" option (see ai_review._call_ollama). Ollama's own default is a
# modest 2048, which is easy to exceed once SYSTEM_PROMPT, the submitted
# note, and an encoded image are all in the same request ("request (N
# tokens) exceeds the available context size (2048 tokens)") - 4096 gives
# real headroom. Raise it further in .env if larger images or longer
# prompts start hitting the same error again; a bigger context window uses
# more of the host machine's RAM/VRAM.
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX") or "4096")

# How long to wait for either AI backend before giving up and falling back
# to the raw submitted note (see ai_review.review_item) - without this, a
# hung network call or an overloaded local Ollama server could block that
# item's review indefinitely. 90s comfortably covers normal cloud latency
# and most local generations; raise it if OLLAMA_NUM_CTX/slower hardware
# means legitimate reviews are getting cut off.
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS") or "90")

# Whether the Automated Review (AI) step is active:
#   - "anthropic" backend: on the moment a real ANTHROPIC_API_KEY is set.
#   - "ollama" backend: always on - there's no key to check for a local
#     server, so this trusts that Ollama is actually running with
#     OLLAMA_VISION_MODEL pulled (see "Running with the Ollama backend" in
#     the README). If it isn't, review_item() fails per-item and falls back
#     to the raw note, same as any other AI hiccup.
#   - anything else: off, so a typo in AI_REVIEW_BACKEND fails safe instead
#     of silently calling the wrong backend.
# While this is False, items skip straight from Data Entry to Queue Review,
# carrying forward the raw note as-is.
if AI_REVIEW_BACKEND == "ollama":
    AI_ENABLED = True
elif AI_REVIEW_BACKEND == "anthropic":
    AI_ENABLED = bool(ANTHROPIC_API_KEY)
else:
    AI_ENABLED = False

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

# ---- Local backups (see backup.py) ----
# Verified zip snapshots of the database + photos + CSV batch archives.
# Created automatically every BACKUP_INTERVAL_HOURS while the bot is
# running (cogs/admin_tools.py) and on demand via /admin backup-now or
# `python backup.py create`. BACKUP_KEEP_COUNT/BACKUP_MAX_AGE_DAYS bound
# how many pile up in BACKUP_DIR - whichever limit is hit first prunes the
# oldest. Backups contain real business/customer data - don't upload them
# anywhere public.
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS") or "24")
BACKUP_KEEP_COUNT = int(os.getenv("BACKUP_KEEP_COUNT") or "14")
BACKUP_MAX_AGE_DAYS = int(os.getenv("BACKUP_MAX_AGE_DAYS") or "14")

# ---- Local photo storage (see workflow photo handling in item_flow.py) ----
PHOTO_DIR = os.getenv("PHOTO_DIR", "data/photos")

# ---- Single-instance lock (see instance_lock.py) ----
INSTANCE_LOCK_PATH = os.getenv("INSTANCE_LOCK_PATH", "data/.bot.lock")

# ---- Non-secret runtime settings editable from Discord (see runtime_settings.py) ----
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "data/settings.json")

# ---- Buyer data retention (see /pirate-ship purge-buyer-data) ----
# How many days after an item ships before its buyer's name/address is
# eligible for removal. Only recipient_name/shipping_address/structured
# address fields are cleared - inventory identity, sale price, and audit
# actors are never touched. Purging is always an explicit admin action
# (preview by default, confirm:True to actually run it), never automatic.
BUYER_DATA_RETENTION_DAYS = int(os.getenv("BUYER_DATA_RETENTION_DAYS") or "90")

# ---- AI review model (only used by the "anthropic" backend) ----
ANTHROPIC_MODEL = "claude-sonnet-5"

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
# narrowed to the ones that actually come up in liquidation/overstock resale.
# Shown as a Discord select menu when approving an item in Queue Review, with
# EBAY_DEFAULT_CONDITION_ID pre-highlighted since most items land there.
EBAY_CONDITIONS = [
    ("1000", "New"),
    ("1500", "New other (see details)"),
    ("1750", "New with defects"),
    ("3000", "Used"),
    ("7000", "For parts or not working"),
]
EBAY_CONDITION_LABELS = dict(EBAY_CONDITIONS)
EBAY_DEFAULT_CONDITION_ID = "1500"  # "New other (see details)" - most liquidation items land here

# eBay File Exchange's *Duration values for an Auction-format listing (fixed-
# price listings always use "GTC" - Good 'Til Cancelled - handled separately
# in ebay_csv.py). Shown as a Discord select menu, only when the reviewer
# picks "Auction" as the format during Queue Review approval.
EBAY_AUCTION_DURATIONS = [
    ("Days_3", "3 days"),
    ("Days_5", "5 days"),
    ("Days_7", "7 days"),
    ("Days_10", "10 days"),
]

# ---- eBay category picker ----
# eBay category selection no longer uses a hand-typed list here at all - see
# ebay_taxonomy.py, which searches eBay's own official ~18,000-leaf category
# export (ebay_categories.json) by keyword. That file replaced a small
# hand-maintained EBAY_CATEGORIES dict that had repeatedly caused real
# upload failures (non-leaf IDs, and once two categories accidentally
# sharing the same ID, which crashed the entire Queue Review approval flow
# outright). Every ID ebay_taxonomy.py can return is real, verified, and a
# genuine listable leaf category straight from eBay's own data - there's
# nothing to hand-configure or keep correcting here anymore.

# ---- eBay CSV batch (Seller Hub bulk upload / File Exchange fallback) ----
# Accumulates one row per item added via "Add to eBay Batch" until an admin
# runs /ebay export-batch, which hands it over as a Discord attachment and
# archives + clears it so the next batch starts clean.
EBAY_BATCH_CSV_PATH = os.getenv("EBAY_BATCH_CSV_PATH", "data/ebay_batch.csv")
EBAY_BATCH_ARCHIVE_DIR = os.getenv("EBAY_BATCH_ARCHIVE_DIR", "data/ebay_batch_archive")

# eBay's bulk upload REQUIRES an item location on every single row (a city/
# state or postal code - whatever your seller account was set up with) -
# without it, eBay rejects every row with "No <Item.Location> exists".
# There's no reasonable default to guess here (it's your business's real
# ship-from location), so /ebay export-batch refuses to export until this
# is set, rather than producing a CSV that's guaranteed to fail on every row.
EBAY_ITEM_LOCATION = os.getenv("EBAY_ITEM_LOCATION", "").strip()

# eBay's bulk upload also REQUIRES at least one shipping service on every
# row (confirmed against a real upload: "Please add at least one valid
# shipping service option to your listing"). Uses eBay's Calculated
# (weight-based) shipping type, not one flat cost for every item - each
# item's own weight/dimensions (captured as required fields on
# EbayListingModal, see item_flow.py) drive the actual per-listing charge,
# so a small light item and a big heavy one aren't priced the same.
# EBAY_SHIPPING_SERVICE must still be one of eBay's own ShippingService
# codes (e.g. "USPSPriority", "USPSGround", "UPSGround") - it says WHICH
# carrier service to calculate with, not a price - and
# EBAY_SHIPPING_PACKAGE_TYPE must be one of eBay's ShippingPackage codes
# (e.g. "PackageThickEnvelope", "IrregularPackage" - what's right depends
# on what you're actually shipping). Neither has a safe default to guess
# any more than EBAY_ITEM_LOCATION did - verify both via eBay's own listing
# flow (start a listing by hand, choose Calculated shipping, see what it's
# called) rather than trusting this comment's examples blindly. All three
# are required before /ebay export-batch will export anything.
EBAY_SHIPPING_TYPE = os.getenv("EBAY_SHIPPING_TYPE", "Calculated").strip()
EBAY_SHIPPING_SERVICE = os.getenv("EBAY_SHIPPING_SERVICE", "").strip()
EBAY_SHIPPING_PACKAGE_TYPE = os.getenv("EBAY_SHIPPING_PACKAGE_TYPE", "").strip()

# ---- Pirate Ship CSV export (see pirate_ship_csv.py) ----
# Only for "Other"-platform sales (FB Marketplace, website, etc.) that were
# sold outside eBay - eBay sales don't need this, since Pirate Ship pulls
# those directly via its own native eBay integration. Unlike the eBay batch,
# there's no separate "add to batch" step: /pirate-ship export-batch queries
# the database directly for sold-but-not-yet-exported "Other" items each
# time it runs, so nothing needs accumulating on disk between exports - only
# where the resulting CSV gets archived for a paper trail.
PIRATE_SHIP_EXPORT_ARCHIVE_DIR = os.getenv("PIRATE_SHIP_EXPORT_ARCHIVE_DIR", "data/pirate_ship_exports")

# ---- Cloudflare R2 image hosting (optional, see r2_storage.py) ----
# Gives each Data Entry photo a durable public URL for the eBay CSV batch's
# PicURL column and anything else that needs to stay valid long-term -
# Discord's own attachment links expire once the originating message is
# deleted, which happens routinely as items move through the pipeline.
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL_BASE = os.getenv("R2_PUBLIC_URL_BASE")  # e.g. https://pub-xxxx.r2.dev - no trailing slash

# Whether photo uploads to R2 are active. False (the normal case until all
# five values above are set) - Data Entry still saves the local copy either
# way (that's what Discord item cards render from), it just skips the R2
# upload/public-URL step, and the eBay CSV's PicURL column stays blank.
R2_ENABLED = bool(
    R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME and R2_PUBLIC_URL_BASE
)

# ---- Database wipe confirmation phrase ----
# The exact text an admin must type to confirm a full database wipe.
# Deliberately not configurable via .env - changing it requires editing code,
# which is itself a small speed bump against wiping by accident.
DB_WIPE_CONFIRMATION_PHRASE = "DELETE EVERYTHING"
