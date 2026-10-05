"""
Central configuration for the pallet tracking bot.
Edit these values to match your actual Discord server setup.
"""
import os
import re
from dotenv import load_dotenv

# FOUR_GUYS_COMBINED is never set by anything in this repo anymore (the
# separate standalone combined_bot.py deployment that used to set it before
# importing config was removed - see cogs/website_shop.py for the
# integrated website-shop feature that replaced it). COMBINED_MODE stays
# permanently False here; combined_intake.py's own COMBINED_MODE branch is
# effectively dead but left in place since it's shared, tested code.
if os.environ.get("FOUR_GUYS_COMBINED") != "1":
    load_dotenv()

COMBINED_MODE = os.environ.get("FOUR_GUYS_COMBINED") == "1"

# ---- Secrets (set these in a .env file, never hardcode them) ----
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # optional - only used by the "anthropic" backend below
# Every int()/float() env var below uses `os.getenv("X") or "default"`, not
# os.getenv("X", "default") - the two-arg form only falls back when the key
# is entirely ABSENT from .env, not when it's present but left blank (e.g.
# "DISCORD_GUILD_ID=" from a freshly-copied .env.example), which crashed
# the bot at startup with a ValueError instead of using the default.
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID") or "0")  # your server's ID

# Recovery deployment: keep every existing Discord message unchanged -
# edits post a new card instead of editing/deleting the old one, background
# loops that would otherwise touch old cards don't start, and destructive
# admin commands refuse to run (see recovery_safety.py). Defaults OFF so
# the bot's normal behavior is unchanged; set to true only when recovering
# an installation that needs its existing message history preserved.
_history_setting = os.getenv("PRESERVE_DISCORD_HISTORY", "false").strip().lower()
if _history_setting not in {"true", "false"}:
    raise ValueError("PRESERVE_DISCORD_HISTORY must be true or false")
PRESERVE_DISCORD_HISTORY = _history_setting == "true"
WEBSITE_SKU_PREFIX = os.getenv("WEBSITE_SKU_PREFIX", "")
if WEBSITE_SKU_PREFIX and not re.fullmatch(r"[A-Z][A-Z0-9]{0,15}-", WEBSITE_SKU_PREFIX):
    raise ValueError("WEBSITE_SKU_PREFIX must be empty or an uppercase prefix such as MAIN-")

# Whether cogs/item_flow.py's send_item_card/send_website_hold_notice
# automatically embed website-contract marker fields ("Website item ID",
# "Website SKU", etc.) on ordinary listed/sold/shipped cards - see
# website_contract.py. This only ever applies OUTSIDE combined mode
# (combined mode's own #website_shop review flow is the sole source of
# truth for what's actually approved for the website - an ordinary card
# there never carries these markers, so nothing could be mistaken for an
# approved one). Defaults OFF so the standalone bot's cards stay exactly
# as they are today; turn it on only if this standalone deployment is
# genuinely feeding its own separate website integration.
WEBSITE_MARKERS_ENABLED = os.getenv("WEBSITE_MARKERS_ENABLED", "false").strip().lower() == "true"

# ---- Website shop integration (cogs/website_shop.py) ----
# Adds a "#website_shop" approval flow to THIS bot - same process, same
# database, same Discord token - for publishing items to a separate
# storefront website, reusing the website's existing Publisher contract/
# transport/durable journal (see publisher/). Defaults OFF; every setting
# below is required only once this is turned on. combined_intake.py/
# combined_delivery.py (see their own comments) enforce that this only ever
# manages items at/above WEBSITE_SHOP_ITEM_ID_FLOOR (never adopts
# historical inventory) and always uses the fixed "FGNEW-" SKU prefix.
WEBSITE_SHOP_ENABLED = os.getenv("WEBSITE_SHOP_ENABLED", "false").strip().lower() == "true"
WEBSITE_SHOP_ITEM_ID_FLOOR = 1_000_000_000
WEBSITE_SHOP_SKU_PREFIX = "FGNEW-"
WEBSITE_SHOP_CHANNEL_ID = int(os.getenv("WEBSITE_SHOP_CHANNEL_ID") or "0")
# Discord user IDs allowed to act in #website_shop (comma-separated) - this
# module's approval store checks a fixed staff list, not a role.
WEBSITE_SHOP_OPERATOR_IDS = tuple(
    v.strip() for v in (os.getenv("WEBSITE_SHOP_OPERATOR_IDS") or "").split(",") if v.strip()
)
WEBSITE_URL = os.getenv("WEBSITE_URL", "")
WEBSITE_SOURCE_ID = os.getenv("WEBSITE_SOURCE_ID", "")
WEBSITE_SECRET = os.getenv("WEBSITE_SECRET", "")
# Separate from WEBSITE_SHOP_ENABLED for a staged rollout: turn the shop
# review flow on first (WEBSITE_SHOP_ENABLED), confirm it behaves, THEN
# also turn this on once ready to actually send approved items to the real
# website.
WEBSITE_PUBLISH_ENABLED = os.getenv("WEBSITE_PUBLISH_ENABLED", "false").strip().lower() == "true"
WEBSITE_SHOP_POLL_SECONDS = int(os.getenv("WEBSITE_SHOP_POLL_SECONDS") or "30")

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

# Created ONCE for the whole server (via /setup shared-channels), shared by
# every pallet. An item's embed always shows which pallet it belongs to.
SHARED_STAGE_CHANNELS = [
    "automated-review",
    "queue-review",
    "awaiting-listing",
    "pending-ebay-upload",
    "pending-fb-marketplace-upload",
    "listed",
    "sold",
    "hold",
    "10-day-alerts",
]
SHARED_PIPELINE_CATEGORY_NAME = "Shared Pallet Pipeline"

# Also created ONCE via /setup shared-channels, in their own Finance
# category (see FINANCE_CATEGORY_NAME) rather than alongside the
# item-pipeline channels - kept in a SEPARATE list rather than folded into
# SHARED_STAGE_CHANNELS since these aren't stages an item moves through
# (resolve_channel_id/is_shared_channels_setup/pipeline status-counting all
# assume SHARED_STAGE_CHANNELS entries correspond to an item status). This
# is where the QuickBooks credit-card-charge -> pallet allocation workflow
# lives (see cogs/finance.py, quickbooks.py), and - now that direct
# QuickBooks access isn't available - the manually-submitted-invoice
# workflow (#submit-invoices) that replaces it - still stored in the same
# shared_channels DB table so the bot can look their IDs up at runtime,
# just outside that item-status list.
FINANCE_SHARED_CHANNELS = [
    "credit-card-charges",
    "awaiting-pallet-charges",
    "submit-invoices",
    "accounting",
    "finance-audit-log",
    "finance-dashboard",
]
FINANCE_CATEGORY_NAME = "Finance"

# Shared channels that are visible to @everyone (read-only - only Pallet
# Admin can post; the bot posts freely regardless, same as #information)
# rather than gated to specific roles via CHANNEL_ROLE_PERMISSIONS below -
# for content the whole team should be able to see without needing a
# finance-specific role, per the original ask ("so everyone is aware of
# our position").
EVERYONE_VIEWABLE_CHANNELS = ["finance-audit-log", "finance-dashboard"]

# A read-only orientation channel explaining how this bot works, posted by
# /setup info-channel (see information_content.py) - created in the same
# shared category as the pipeline stage channels, but visible to @everyone
# regardless of role, since it's meant for anyone new to read before asking.
INFORMATION_CHANNEL_NAME = "information"

# Full pipeline order, per-pallet + shared combined, in the order an item
# actually moves through them. Used wherever code needs "all stages" (the
# NewPalletModal only creates PER_PALLET_CHANNELS itself - shared ones must
# already exist via /setup shared-channels).
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
    "pending-fb-marketplace-upload": [ROLE_LISTING_MGMT],
    "listed": [ROLE_LISTING_MGMT],
    "sold": [ROLE_LISTING_MGMT],
    "hold": [ROLE_LISTING_MGMT, ROLE_QUEUE_REVIEW],
    "10-day-alerts": [ROLE_LISTING_MGMT, ROLE_FINANCE_MGMT],
    "credit-card-charges": [ROLE_FINANCE_MGMT, ROLE_PURCHASE_MGMT],
    "awaiting-pallet-charges": [ROLE_FINANCE_MGMT, ROLE_PURCHASE_MGMT],
    "submit-invoices": [ROLE_FINANCE_MGMT, ROLE_PURCHASE_MGMT],
    "accounting": [ROLE_FINANCE_MGMT],
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
        "message, then send. The bot replies right here with the item number(s) it "
        "assigned - write that number on the box/sticker so it's easy to find again "
        "when it sells - then removes your message and sends it into the shared "
        "Automated Review channel automatically. "
        "Got several of the exact same thing? Start the note with \"3x \" (e.g. "
        "\"3x cordless drill, new in box\") and the bot logs 3 separate items from "
        "this one message, each tracked (and sellable) independently from here on, "
        "each getting its own number - "
        "\"3 of the same\"/\"3 of these\" anywhere in the note works too. "
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
        "which pallet each item is from. Approve, Edit the description, Re-review "
        "(AI) - re-runs the AI pass after an edit or an AI error, straight through "
        "the Automated Review channel and back here - or Reject (sends back to "
        "that pallet's own Data Entry channel)."
    ),
    "awaiting-listing": (
        "**Listing Management role** works here. These items were approved in Queue Review "
        "(with eBay title/category/condition/price already captured). Click **Add to eBay "
        "Batch** or **Add to FB Marketplace Batch** to append it to that platform's CSV batch "
        "(or **List on eBay (API)** if the direct API is enabled), or **Mark Listed (Other)** "
        "once you've listed it manually somewhere else entirely."
    ),
    "pending-ebay-upload": (
        "**Listing Management role** works here. Items land here after **Add to eBay Batch** on "
        "an Awaiting Listing card, sitting in the CSV batch waiting for someone to run "
        "`/ebay export-batch` and upload it in eBay Seller Hub. Once eBay actually shows one live, "
        "tap **Confirm Listed** on its card (or run `/ebay confirm-listed` to confirm a whole "
        "pallet at once) to move it into #listed - there's no live API to detect that automatically."
    ),
    "pending-fb-marketplace-upload": (
        "**Listing Management role** works here. Items land here after **Add to FB Marketplace "
        "Batch** on an Awaiting Listing card, sitting in the CSV batch waiting for someone to run "
        "`/fb-marketplace export-batch` and upload it to Facebook's bulk listing tool. Once it's "
        "actually live, tap **Confirm Listed** on its card (or run `/fb-marketplace confirm-listed` "
        "to confirm a whole pallet at once) to move it into #listed - there's no API to detect "
        "that automatically."
    ),
    "listed": (
        "**Listing Management role** works here. These items are currently live for "
        "sale somewhere. Click Mark as Sold once one sells."
    ),
    "sold": (
        "Sold items land here. Click Mark as Shipped once it's been packed and sent "
        "to the buyer, just so there's a record of what's shipped vs. sold-but-not-out-yet."
    ),
    "hold": (
        "**Listing Management or Queue Review role** works here. Items land here via "
        "`/item hold`, run inside that pallet's category - each card shows why it's held. "
        "Once whatever's blocking it is sorted out, tap **Resolved** to send it right back "
        "to wherever it would normally be sitting at this point in the pipeline."
    ),
    "10-day-alerts": (
        "No action needed most of the time - this channel gets an automatic ping "
        "whenever an item has been sitting in Listed for 10+ days, across every "
        "pallet, as a nudge to consider a price drop or refreshed photos."
    ),
    "credit-card-charges": (
        "New QuickBooks credit card charges show up here automatically (needs "
        "/finance connect-quickbooks first) with a select menu - pick which pallet "
        "the charge belongs to, or \"New Pallet (not arrived yet)\" if it hasn't been "
        "created here yet. Allocating pushes a matching categorized expense back "
        "into QuickBooks too, so the books and this bot stay in sync."
    ),
    "awaiting-pallet-charges": (
        "Charges allocated to \"New Pallet (not arrived yet)\", and manually-submitted "
        "invoices from #submit-invoices, sit here until a matching pallet is actually "
        "created - Start New Pallet will then offer to attach any unclaimed ones shown "
        "here to it. A submitted invoice's card has a **Confirm logged in QuickBooks** "
        "button (**Finance Management role**) - once you've entered it into QuickBooks "
        "by hand, click it to remove the card; it can still be attached to a pallet "
        "later regardless. QuickBooks-sourced charges don't have this button - those "
        "get pushed to QuickBooks automatically when claimed."
    ),
    "submit-invoices": (
        "**Purchase Management role** posts here. One message per invoice: attach the "
        "invoice (photo or PDF) and type just the dollar amount (e.g. \"125.50\") in the "
        "same message, then send. The bot logs it and removes your message - it then "
        "shows up in #awaiting-pallet-charges until Start New Pallet attaches it to the "
        "matching pallet, at which point it's ready for the accountant to enter into "
        "QuickBooks by hand."
    ),
    "accounting": (
        "**Finance Management role.** Items land here as soon as they're marked sold, "
        "with the platform picked at that time. Use `/finance log-sale` to enter the "
        "purchase price and COGS (single item or a bundle across pallets) and book it to "
        "QuickBooks - a Sales Receipt for Facebook/in-person/other sales (QuickBooks' own "
        "Automated Sales Tax adds the tax), plus a Cost of Goods Sold journal entry either "
        "way."
    ),
    "finance-audit-log": (
        "Read-only record of every completed QuickBooks transaction this bot has posted - "
        "logged sales (Sales Receipt + COGS journal entry numbers) and allocated credit-card "
        "charges. Visible to everyone; only the bot posts here."
    ),
    "finance-dashboard": (
        "A single pinned message the bot keeps updated automatically - current account "
        "balances, month-to-date revenue/spend, and pallets in progress vs. sold out. "
        "Visible to everyone; refreshes itself whenever a sale, expense, or cost changes."
    ),
}

# ---- Business logic ----
DAYS_BEFORE_STALE_ALERT = 10
STALE_CHECK_INTERVAL_HOURS = 12  # how often the background job checks for stale listings

# ---- Database ----
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/pallet_tracker.db")

# The website-shop feature's own two sqlite stores (shop_approval.py's
# review store, publisher/journal.py's durable delivery journal) - kept
# alongside the main database so backup.py picks them up in the same
# backup/restore cycle as everything else (see backup.py's own comment).
WEBSITE_SHOP_APPROVAL_DB_PATH = os.path.join(os.path.dirname(DATABASE_PATH), "shop_approval.sqlite")
WEBSITE_SHOP_JOURNAL_DB_PATH = os.path.join(os.path.dirname(DATABASE_PATH), "website_journal.sqlite")

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

# ---- Local invoice storage (see #submit-invoices handling in cogs/finance.py) ----
INVOICE_DIR = os.getenv("INVOICE_DIR", "data/invoices")

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

# ---- R2 photo retention after a sale (see /admin purge-old-photos) ----
# How many days after an item is marked SOLD before its R2-hosted photo
# copies (config.R2_ENABLED) are eligible for removal - a buffer so photos
# stay available in case of a return/refund shortly after the sale. Only
# the R2 (public, durable) copy is ever purged - the local disk copy under
# PHOTO_DIR is untouched, so a Discord item card still renders if one's
# ever reposted. Purging is always an explicit admin action (preview by
# default, confirm:True to actually run it), never automatic, same as
# buyer data retention above.
PHOTO_RETENTION_DAYS_AFTER_SALE = int(os.getenv("PHOTO_RETENTION_DAYS_AFTER_SALE") or "30")

# ---- AI review model (only used by the "anthropic" backend) ----
ANTHROPIC_MODEL = "claude-sonnet-5"

# All possible item status values, in pipeline order - used by /pallet-list
# to display a consistent stage-by-stage count regardless of which stages
# currently have items in them. Must match the STATUS_* constants in database.py.
STAGE_ORDER_FOR_STATUS = [
    "data_entry", "automated_review", "queue_review", "awaiting_listing",
    "pending_ebay_upload", "pending_fb_marketplace_upload", "listed", "sold", "shipped",
    "on_hold", "rejected", "deleted",
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

# Condition IDs eBay broadly accepts across most categories - checked in
# ebay_csv.py before writing *ConditionID on an outbound row. Real per-
# category valid-condition lists vary and would need live eBay API access
# (GetCategoryFeatures - see ebay_api.py, pending dev approval) to check
# properly - but eBay's own official ConditionEnum reference is explicit
# about which ones are safe regardless: only 1000 (New) and 3000 (Used)
# carry the "Most categories support this condition" language. Every other
# ID - including 1500 "New other" (real upload evidence: rejected outright
# for Lights/Lamps categories, error 21916883), 1750 (same error, other
# categories), and the 2000-2030 "Refurbished" tiers (eBay's docs say
# those require a separate seller application/enrollment program this
# account isn't in) - is category-restricted, not broadly safe. Any chosen
# condition not in this set falls back to EBAY_CONDITION_FALLBACK_ID
# rather than risking that rejection.
EBAY_BROADLY_ACCEPTED_CONDITION_IDS = {"1000", "3000"}

# What an invalid/category-restricted condition (see above) falls back to -
# deliberately a SEPARATE constant from EBAY_DEFAULT_CONDITION_ID (which
# stays "1500" for Queue Review's suggested starting pick, since that's
# genuinely what most liquidation items physically are - open box, no
# original packaging). "3000" (Used) is the fallback instead of "1000"
# (New) precisely because these items usually aren't factory-sealed/new -
# claiming Used is more honest than claiming New, and it's one of the two
# IDs eBay's own docs back as broadly accepted.
EBAY_CONDITION_FALLBACK_ID = "3000"

# How many days out from export every listing in an eBay batch CSV gets
# scheduled to start (ebay_csv.py's ScheduleTime column, stamped once at
# export_and_archive() time - not when each item was individually added to
# the batch, since a batch can sit for a while before it's actually
# uploaded, and eBay rejects a ScheduleTime that's already passed by the
# time File Exchange processes it). eBay allows scheduling up to 3 weeks
# out, so 7 days leaves plenty of room.
EBAY_LISTING_SCHEDULE_DELAY_DAYS = 7

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

# ---- eBay CSV batch (classic File Exchange "Add" template - see ebay_csv.py) ----
# Accumulates one row per item added via "Add to eBay Batch" until an admin
# runs /ebay export-batch, which hands it over as a Discord attachment and
# archives + clears it so the next batch starts clean. This is eBay's
# classic File Exchange "Add" template - a single ready-to-upload file built
# entirely from data Queue Review already captured (title/category/
# condition/price/weight/dimensions/specifics), not eBay's newer AI-prefill
# tool's 2-step suggest-then-review round trip (that tool is still available
# by hand if anyone wants it - see /ebay fill-recommendations - just no
# longer what this batch is built for).
EBAY_BATCH_CSV_PATH = os.getenv("EBAY_BATCH_CSV_PATH", "data/ebay_batch.csv")
EBAY_BATCH_ARCHIVE_DIR = os.getenv("EBAY_BATCH_ARCHIVE_DIR", "data/ebay_batch_archive")

# eBay Business Policy names (Seller Hub > Account > Business Policies) -
# written into every row's ShippingProfileName/ReturnProfileName/
# PaymentProfileName columns. Without these (or the older manual shipping
# fields this replaces), eBay rejects an "Add" row outright once a
# Calculated-shipping policy is attached to the account. If any of these
# policies are ever renamed in Seller Hub, update the matching value here to
# match - eBay matches by exact name, not by an internal ID.
EBAY_SHIPPING_PROFILE_NAME = os.getenv("EBAY_SHIPPING_PROFILE_NAME", "Shipping")
EBAY_RETURN_PROFILE_NAME = os.getenv("EBAY_RETURN_PROFILE_NAME", "Returns")
EBAY_PAYMENT_PROFILE_NAME = os.getenv("EBAY_PAYMENT_PROFILE_NAME", "Payment")

# Ship-from ZIP code, written into every row's PostalCode column. Required
# once a Calculated-shipping policy is attached (error 216007, "valid postal
# code") - the *Location column alone isn't enough once policies are in use.
EBAY_SHIP_FROM_POSTAL_CODE = os.getenv("EBAY_SHIP_FROM_POSTAL_CODE", "47380")  # Ridgeville, IN

# Fallback weight (WeightMajor/WeightMinor), only used when an item has no
# real captured weight_lb (Queue Review's weight field is optional - see
# EbayListingModal, item_flow.py). Required once a Calculated-shipping
# policy is attached (error 216121 otherwise). Keyed by a lowercase keyword
# matched against the item's resolved eBay category path (ebay_taxonomy.
# get_path) - first match wins. These are rough ESTIMATES, not real
# per-item weights - if pallet items start getting weighed at intake, wire
# that real per-item weight in instead of leaning on this table.
EBAY_DEFAULT_WEIGHT_BY_CATEGORY_LB = {
    "ceiling fan": 8.0,
    "light fixture": 4.0,
    "chandelier": 6.0,
    "pendant light": 3.0,
    "wall sconce": 2.0,
    "smoke detector": 1.0,
    "carbon monoxide detector": 1.0,
}
EBAY_DEFAULT_WEIGHT_FALLBACK_LB = 2.0  # used when no category keyword above matches at all

# Keyword -> eBay Aspect "Type" value, used to fill C:Type when an item has
# no Type in its own item_specifics. Matched against the item's title
# (lowercased) - first match wins. Unlike C:Brand, there's no safe generic
# placeholder for Type (it's a real search/browse attribute, and a wrong
# guess is worse than a blank one) - see ebay_csv._infer_type, which logs a
# warning and leaves the column blank rather than guessing when nothing
# here matches.
EBAY_TYPE_KEYWORDS = {
    "flush mount": "Flush Mount",
    "pendant": "Pendant",
    "chandelier": "Chandelier",
    "sconce": "Wall Sconce",
    "track light": "Track Lighting",
    "ceiling fan": "Ceiling Fan",
    "table lamp": "Table Lamp",
    "floor lamp": "Floor Lamp",
    "smoke detector": "Smoke Detector",
    "carbon monoxide": "Carbon Monoxide Detector",
}

# ---- Facebook Marketplace CSV batch (see fb_marketplace_csv.py) ----
# Same accumulate-then-export shape as the eBay batch above: one row per item
# added via "Add to FB Marketplace Batch" until an admin runs
# /fb-marketplace export-batch, which hands it over as a Discord attachment
# and archives + clears it so the next batch starts clean. Matches Facebook's
# own bulk-listing CSV template (Title/Price/Description are the columns
# Facebook's help docs list as required; Photo URL is added here too using
# the same R2-hosted public photo links the eBay batch uses - a listing with
# no photo isn't very sellable - but wasn't in Facebook's own required-column
# list, so double check its header name against Facebook's real downloaded
# template and rename it here if theirs differs).
FB_MARKETPLACE_BATCH_CSV_PATH = os.getenv("FB_MARKETPLACE_BATCH_CSV_PATH", "data/fb_marketplace_batch.csv")
FB_MARKETPLACE_BATCH_ARCHIVE_DIR = os.getenv("FB_MARKETPLACE_BATCH_ARCHIVE_DIR", "data/fb_marketplace_batch_archive")

# EBAY_ITEM_LOCATION is written into every outbound batch row's *Location
# column (ebay_csv.py) - eBay rejects a row with no location ("No
# <Item.Location> exists"). Left blank here just means an empty *Location
# column, not a blocked export - fill it in by hand before uploading if so.
#
# EBAY_SHIPPING_SERVICE is unrelated to the outbound batch (that now uses
# EBAY_SHIPPING_PROFILE_NAME above instead of a raw per-row shipping
# service) - it's only used by /ebay fill-recommendations (see
# ebay_recommendations.py) to help fill in eBay's own RETURNED
# recommendations file from its separate AI-prefill tool.
EBAY_ITEM_LOCATION = os.getenv("EBAY_ITEM_LOCATION", "").strip()
EBAY_SHIPPING_SERVICE = os.getenv("EBAY_SHIPPING_SERVICE", "").strip()

# ---- Pirate Ship CSV export (see pirate_ship_csv.py) ----
# Only for "Other"-platform sales (FB Marketplace, website, etc.) that were
# sold outside eBay - eBay sales don't need this, since Pirate Ship pulls
# those directly via its own native eBay integration. Unlike the eBay batch,
# there's no separate "add to batch" step: /pirate-ship export-batch queries
# the database directly for sold-but-not-yet-exported "Other" items each
# time it runs, so nothing needs accumulating on disk between exports - only
# where the resulting CSV gets archived for a paper trail.
PIRATE_SHIP_EXPORT_ARCHIVE_DIR = os.getenv("PIRATE_SHIP_EXPORT_ARCHIVE_DIR", "data/pirate_ship_exports")

# ---- QuickBooks Online (optional, see quickbooks.py) ----
# CLIENT_ID/CLIENT_SECRET come from a registered app at
# https://developer.intuit.com (My Apps -> create an app -> Keys & OAuth).
# ENVIRONMENT is "sandbox" or "production" depending on which set of keys
# you're using - QuickBooks uses a completely separate API host and company
# data for each, so this must match. REDIRECT_URI must be entered EXACTLY
# (including http/https and trailing slash presence) as one of the app's
# "Redirect URIs" in that same Keys & OAuth page - this bot has no web
# server to actually receive that redirect, so /finance connect-quickbooks
# has you paste the resulting (page-may-fail-to-load, that's fine) URL back
# in rather than needing one; http://localhost:8000/callback works fine as
# a placeholder to register, since nothing needs to actually be listening
# there for this to work.
QUICKBOOKS_CLIENT_ID = os.getenv("QUICKBOOKS_CLIENT_ID")
QUICKBOOKS_CLIENT_SECRET = os.getenv("QUICKBOOKS_CLIENT_SECRET")
QUICKBOOKS_ENVIRONMENT = os.getenv("QUICKBOOKS_ENVIRONMENT", "sandbox").strip().lower()
QUICKBOOKS_REDIRECT_URI = os.getenv("QUICKBOOKS_REDIRECT_URI", "http://localhost:8000/callback")

# REQUIRED before the credit-card poller does anything - the QuickBooks
# Account ID (not the last-4 digits; the internal QuickBooks record ID -
# Accounting -> Chart of Accounts -> open the card account -> the "id="
# value in the browser URL) of the ONE credit card account to watch for new
# charges. Deliberately scoped to a single account rather than "every
# connected card" - if the same QuickBooks company also has accounts
# unrelated to this pallet business, those should never show up here asking
# to be allocated to a pallet.
QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID = os.getenv("QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID", "").strip()

# How often the credit-card poller checks QuickBooks for new charges.
QUICKBOOKS_POLL_MINUTES = int(os.getenv("QUICKBOOKS_POLL_MINUTES") or "20")

# Whether the QuickBooks integration is usable at all - the app credentials
# are enough to run /finance connect-quickbooks; the credit-card poller
# additionally needs QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID set (checked
# separately, see cogs/finance.py) since watching zero accounts is the safe
# default until that's deliberately configured.
QUICKBOOKS_ENABLED = bool(QUICKBOOKS_CLIENT_ID and QUICKBOOKS_CLIENT_SECRET)

# A pallet's cost basis (see database.get_pallet_financials) is the legacy
# manual /finance setprice lump sum PLUS every itemized pallet_costs row
# (credit card charges/shipping/supplies allocated via the workflows below)
# - additive, not a replacement, so /finance setprice keeps working exactly
# as it already does for pallets that never touch these newer workflows.
# Used by the low-margin early-warning below.
QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT = float(os.getenv("QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT") or "20")

# ---- Sale logging: FB/in-person Sales Receipt (cash-basis accounting) ----
# See /finance log-sale (cogs/finance.py). These default to the real IDs in
# the connected company as of when this was built (looked up directly via
# QuickBooks' own API, not guessed) - override in .env only if the QuickBooks
# setup changes (a renamed/re-created account, item, or customer).
#
# Cash basis: a pallet's full purchase price is expensed to
# QUICKBOOKS_COGS_ACCOUNT_ID immediately when its charge is claimed (see
# cogs/pallet_setup.py's _claim_charges_for_pallet, cogs/finance.py's
# _handle_allocation_choice) - there is no Inventory asset to relieve later,
# so /finance log-sale posts ONLY a Sales Receipt at sale time (no COGS
# Journal Entry). The per-item cost/COGS figure entered there (pre-filled
# from a matched manifest line - see database.get_item_manifest_cost) is
# still recorded in sale_items for this bot's own profit-per-item reporting
# (database.get_pallet_cogs_logged_total, the finance dashboard) - it just
# never gets pushed to QuickBooks as its own entry anymore, since the cost
# was already recognized at purchase.
#
#   Item "Merchandise Sales (Cash/Local)" -> income account "Sales" (7)
#   Customer "Cash & Local Sales Customer"
#   Account "eBay Sales" (98) - a sub-account of "Sales" (7), used instead
#     of the Item's own default income account for eBay-platform sales
#   Account "Cost of goods sold" (48) - every pallet purchase's expense
#     category (cash basis), not a Journal Entry debit anymore
#   Account "Undeposited funds" (24) - default Sales Receipt deposit target
#     unless a sale is logged as already_deposited
QUICKBOOKS_CASH_SALES_ITEM_ID = os.getenv("QUICKBOOKS_CASH_SALES_ITEM_ID", "2")
QUICKBOOKS_CASH_SALES_CUSTOMER_ID = os.getenv("QUICKBOOKS_CASH_SALES_CUSTOMER_ID", "1")
QUICKBOOKS_SALES_INCOME_ACCOUNT_ID = os.getenv("QUICKBOOKS_SALES_INCOME_ACCOUNT_ID", "7")
QUICKBOOKS_EBAY_SALES_ACCOUNT_ID = os.getenv("QUICKBOOKS_EBAY_SALES_ACCOUNT_ID", "98")
QUICKBOOKS_COGS_ACCOUNT_ID = os.getenv("QUICKBOOKS_COGS_ACCOUNT_ID", "48")
QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID = os.getenv("QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID", "24")
# Only used when a sale is logged as already_deposited=True (cash already
# physically deposited by the time it's entered) - the real bank/cash
# account to deposit to instead of Undeposited Funds. Defaults to "Cash"
# (id 8), the only Bank-type account that exists in the connected company
# as of when this was built; update this once a real checking account is
# connected instead.
QUICKBOOKS_BANK_ACCOUNT_ID = os.getenv("QUICKBOOKS_BANK_ACCOUNT_ID", "8")

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
