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

# ---- Web dashboard (webapp/, cogs/web_dashboard.py) ----
# A website alongside Discord, on the same database - sign in with Discord,
# same roles. Off unless WEB_DASHBOARD_ENABLED=true. Serve it to the internet
# through a Cloudflare Tunnel pointed at http://WEB_HOST:WEB_PORT, and set
# WEB_BASE_URL to the public https address (e.g. https://dash.example.com).
WEB_DASHBOARD_ENABLED = os.getenv("WEB_DASHBOARD_ENABLED", "false").strip().lower() == "true"
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1").strip()
WEB_PORT = int(os.getenv("WEB_PORT") or "8080")
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:8080").strip()
# Discord Developer Portal -> your bot's application -> OAuth2: the Client
# ID (same as the application ID) and Client Secret, and add
# WEB_BASE_URL + "/oauth/callback" under Redirects.
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
# Largest single photo upload accepted from the phone intake page (MB).
WEB_MAX_UPLOAD_MB = int(os.getenv("WEB_MAX_UPLOAD_MB") or "15")

# ---- Stripe website checkout (stripe_api.py, stripe_shop.py) ----
# Website sales through Stripe's hosted Checkout page, with Stripe Tax.
# Needs the web dashboard on too (WEB_DASHBOARD_ENABLED) - the website calls
# WEB_BASE_URL/shop/checkout and Stripe calls WEB_BASE_URL/stripe/webhook.
# Use TEST keys (sk_test_...) until everything's been tried end to end.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_API_VERSION = os.getenv("STRIPE_API_VERSION", "").strip()  # blank = the account's default
STRIPE_ENABLED = bool(STRIPE_SECRET_KEY)
# Where Stripe sends the buyer afterwards (pages on the website).
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "https://4guys1palletoverstock.com/checkout/success").strip()
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "https://4guys1palletoverstock.com/cart").strip()
# Websites allowed to start a checkout from the browser (comma-separated origins).
STRIPE_SHOP_ORIGINS = tuple(o.strip().rstrip("/") for o in
                            os.getenv("STRIPE_SHOP_ORIGINS", "https://4guys1palletoverstock.com").split(",") if o.strip())
# Shared secret the website's SERVER sends (header X-Shop-Key) when it
# forwards a cart to /shop/checkout - lets the bot trust its X-Client-IP
# header for per-shopper rate limiting. Same value in the website's env.
STRIPE_SHOP_SERVER_KEY = os.getenv("STRIPE_SHOP_SERVER_KEY", "").strip()
# Shipping charged at checkout, in dollars (0 = free shipping).
STRIPE_SHIPPING_PRICE = float(os.getenv("STRIPE_SHIPPING_PRICE") or "0")
STRIPE_SHIPPING_LABEL = os.getenv("STRIPE_SHIPPING_LABEL", "Free shipping").strip()
# Stripe Tax product tax code - General Tangible Goods.
STRIPE_TAX_CODE = os.getenv("STRIPE_TAX_CODE", "txcd_99999999").strip()
# How long a website checkout holds its items (Stripe's minimum is 30), and
# how long a payment link sent from the dashboard stays open (max 24).
STRIPE_HOLD_MINUTES = max(30, min(int(os.getenv("STRIPE_HOLD_MINUTES") or "30"), 1440))
STRIPE_LINK_HOURS = max(1, min(int(os.getenv("STRIPE_LINK_HOURS") or "24"), 24))
# QuickBooks: the Website Sale product/service (income account "Website
# Sales"), the liability Stripe-collected sales tax is owed to (e.g.
# "Indiana Department of Revenue Payable"), and where Stripe's processing
# fee goes (blank = the eBay fees account).
QUICKBOOKS_WEBSITE_SALES_ITEM_ID = os.getenv("QUICKBOOKS_WEBSITE_SALES_ITEM_ID", "").strip()
QUICKBOOKS_SALES_TAX_PAYABLE_ACCOUNT_ID = os.getenv("QUICKBOOKS_SALES_TAX_PAYABLE_ACCOUNT_ID", "").strip()
QUICKBOOKS_STRIPE_FEES_ACCOUNT_ID = os.getenv("QUICKBOOKS_STRIPE_FEES_ACCOUNT_ID", "").strip()

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

# How often cogs/item_flow.py's ai_retry_backlog_loop checks for items whose
# first AI review failed (backend unreachable, timed out, bad JSON - see
# ai_review._fallback) and automatically retries them - the unattended
# version of Queue Review's own "Re-review (AI)" button, for whenever the
# backend (e.g. a local Ollama server on another machine) was down and
# nobody's there to click it once it's back. AI_RETRY_BATCH_SIZE caps how
# many it retries per tick, so a backlog that piled up overnight doesn't
# hammer the backend (or blow through AI_TIMEOUT_SECONDS * N) all at once.
AI_RETRY_POLL_MINUTES = int(os.getenv("AI_RETRY_POLL_MINUTES") or "15")
AI_RETRY_BATCH_SIZE = int(os.getenv("AI_RETRY_BATCH_SIZE") or "10")

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
    "ebay-orders",
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
    "listed": [ROLE_LISTING_MGMT],
    "sold": [ROLE_LISTING_MGMT],
    "hold": [ROLE_LISTING_MGMT, ROLE_QUEUE_REVIEW],
    "10-day-alerts": [ROLE_LISTING_MGMT, ROLE_FINANCE_MGMT],
    "credit-card-charges": [ROLE_FINANCE_MGMT, ROLE_PURCHASE_MGMT],
    "awaiting-pallet-charges": [ROLE_FINANCE_MGMT, ROLE_PURCHASE_MGMT],
    "submit-invoices": [ROLE_FINANCE_MGMT, ROLE_PURCHASE_MGMT],
    "accounting": [ROLE_FINANCE_MGMT],
    "ebay-orders": [ROLE_FINANCE_MGMT],
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
        "(with eBay title/category/condition/price already captured). Click **List on eBay** "
        "to put it live on eBay through the API, **Edit Details** to fix the title/price/"
        "item specifics first, or **Mark Listed (Other)** once you've listed it somewhere "
        "else entirely."
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
        "Only used if the business has a credit card account in QuickBooks "
        "(QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID) - there isn't one right now, so nothing "
        "shows up here. When set, each new card charge appears with a select menu to "
        "pick which pallet it belongs to (or \"New Pallet (not arrived yet)\"), and "
        "allocating it categorizes the charge in QuickBooks too."
    ),
    "awaiting-pallet-charges": (
        "Invoices from #submit-invoices (and card charges, if a card account is ever "
        "set up) sit here until a matching pallet is created - Start New Pallet then "
        "offers to attach any unclaimed ones shown here. Claiming one expenses it in "
        "QuickBooks automatically (Cost of goods sold, paid from the bank account). The "
        "**Confirm logged in QuickBooks** button (**Finance Management role**) just "
        "clears a card you've already handled by hand."
    ),
    "submit-invoices": (
        "**Purchase Management role** posts here. One message per invoice: attach the "
        "invoice (photo or PDF) and type just the dollar amount (e.g. \"125.50\") in the "
        "same message, then send. The bot logs it and removes your message - it then "
        "shows up in #awaiting-pallet-charges until Start New Pallet attaches it to the "
        "matching pallet, which also expenses it in QuickBooks automatically (Cost of "
        "goods sold, paid from the bank account)."
    ),
    "accounting": (
        "**Finance Management role.** Items land here as soon as they're marked sold, "
        "with the platform picked at that time - a to-do list. eBay sales are logged with "
        "**Confirm & log sale** on their #ebay-orders card; everything else with "
        "`/finance log-sale` (single item or a bundle across pallets). Either way it books "
        "one QuickBooks Sales Receipt (eBay Sales / In-Person Sales / Other Sales) and the "
        "entry here turns ✅. Cash basis: there's no cost entry at sale time - the "
        "pallet's cost was already expensed when it was bought."
    ),
    "finance-audit-log": (
        "Read-only record of every QuickBooks entry this bot has posted - logged sales "
        "(Sales Receipt numbers), eBay fee entries, claimed invoices, and reversals. "
        "Visible to everyone; only the bot posts here."
    ),
    "finance-dashboard": (
        "A single pinned message the bot keeps updated - bank and Undeposited Funds "
        "balances, this year's sales/costs/net income from QuickBooks, month-to-date "
        "revenue/spend, and pallets in progress vs. sold out. Visible to everyone; "
        "refreshes after every sale, expense or cost change and every 20 minutes. "
        "Admins can force it with `/finance refresh-dashboard`."
    ),
    "ebay-orders": (
        "New eBay orders show up here automatically, matched to a tracked item by SKU or "
        "eBay listing ID, with a **Confirm & log sale** button - nothing is sent to "
        "QuickBooks until you click it. Once an order has shipped (or is 5 days old), an "
        "**eBay fees** card follows with eBay's fees and shipping label; confirming it "
        "books them as expenses. Refunds get a **↩️ eBay refund** card, store/listing "
        "charges a monthly **🧾 account charges** card, and each payout a **🏦 payout** "
        "card that records the Bank Deposit once everything in it is logged. Missing "
        "card? `/ebay order-card order_id:<id>`."
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
# Optional second copy of every verified backup - point it at a folder that
# Google Drive for desktop (or OneDrive/Dropbox) syncs, e.g.
# "G:\My Drive\Pallet Bot Backups", and the backups show up in the cloud.
# Blank = off. The copy folder keeps its own BACKUP_COPY_KEEP_COUNT newest
# backups. A copy that fails is logged; the local backup still counts.
BACKUP_COPY_DIR = (os.getenv("BACKUP_COPY_DIR") or "").strip()
BACKUP_COPY_KEEP_COUNT = int(os.getenv("BACKUP_COPY_KEEP_COUNT") or "30")

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
    "listed", "sold", "shipped",
    "on_hold", "rejected", "deleted",
]

# ---- eBay (optional direct API path - see ebay_api.py) ----
# App ID (client_id) and Cert ID (client_secret) from developer.ebay.com ->
# your application's keys. EBAY_REFRESH_TOKEN comes from the OAuth consent
# flow you complete once by hand (eBay's "Sign in with eBay" / Get a Token
# page) - it's long-lived (~18 months, not rotated on use), and is what
# ebay_api.py's get_access_token() exchanges for a short-lived (~2 hour)
# access token on demand. There's no in-Discord "connect" command for this
# (unlike /finance connect-quickbooks) since the refresh token is obtained
# once, outside the bot, and just pasted into .env.
EBAY_APP_ID = os.getenv("EBAY_APP_ID")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID")
EBAY_REFRESH_TOKEN = os.getenv("EBAY_REFRESH_TOKEN")

# "production" (default) or "sandbox" - selects which eBay API/identity
# hostnames ebay_api.py talks to. Use "sandbox" with sandbox credentials
# (a separate keyset/token from developer.ebay.com's Sandbox tab) to test
# without creating real listings.
EBAY_ENVIRONMENT = os.getenv("EBAY_ENVIRONMENT", "production").strip().lower()

# eBay's Sell API marketplace identifier - "EBAY_US" for the US site, the
# only one this bot's categories/taxonomy (ebay_taxonomy.py) are built for.
EBAY_MARKETPLACE_ID = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")

# The Inventory API needs a merchantLocationKey identifying a real inventory
# location already created on this seller account (Seller Hub, or eBay's
# Account API createInventoryLocation - one-time setup, not something this
# bot does for you). Required to publish an offer;
# create_listing() raises a clear error naming this if it's blank.
EBAY_MERCHANT_LOCATION_KEY = os.getenv("EBAY_MERCHANT_LOCATION_KEY", "").strip()

# Business Policy IDs. Find these via eBay's
# Account API (GET /sell/account/v1/fulfillment_policy, .../payment_policy,
# .../return_policy) or Seller Hub > Business Policies - each policy's ID is
# shown alongside its name. Required to publish an offer; create_listing()
# raises a clear error naming whichever of these is blank.
EBAY_FULFILLMENT_POLICY_ID = os.getenv("EBAY_FULFILLMENT_POLICY_ID", "").strip()
EBAY_PAYMENT_POLICY_ID = os.getenv("EBAY_PAYMENT_POLICY_ID", "").strip()
EBAY_RETURN_POLICY_ID = os.getenv("EBAY_RETURN_POLICY_ID", "").strip()

# Whether the eBay API ("List on eBay" on Awaiting Listing cards, order sync,
# fees, category search, ...) is usable at all. False until EBAY_APP_ID/
# EBAY_CERT_ID/EBAY_REFRESH_TOKEN are all set; until then that button doesn't
# even show up (/ebay status shows what's missing). EBAY_MERCHANT_LOCATION_KEY and the
# three policy IDs above are checked separately, at call time (not here),
# since a missing one should fail with a specific "which value is missing"
# message rather than silently hiding the button the same way as a missing
# OAuth credential would.
EBAY_ENABLED = bool(EBAY_APP_ID and EBAY_CERT_ID and EBAY_REFRESH_TOKEN)

# How often (minutes) the eBay order auto-sync poller checks for new
# orders via the Fulfillment API (cogs/ebay.py's order_poll_loop). Needs
# EBAY_ENABLED AND EBAY_REFRESH_TOKEN to carry the sell.fulfillment scope
# (a wider re-consent than the sell.inventory scope listing creation
# needs - see README's eBay API section) - the loop no-ops entirely
# without it, same as the credit-card poller without a configured account.
EBAY_ORDER_POLL_MINUTES = int(os.getenv("EBAY_ORDER_POLL_MINUTES") or "20")

# eBay order automation (cogs/ebay.py's _reconcile_ebay_order_statuses,
# run on every order poll above): moves a tracked item to Sold when its
# eBay order comes in, and to Shipped once eBay shows that line shipped -
# no Mark as Sold / Mark as Shipped clicks needed for eBay sales. A
# cancelled order, or a sale of an item that's on hold, only ever posts a
# one-time alert to #ebay-orders - nothing is reversed automatically, and
# logging the sale to QuickBooks always stays the human "Confirm & log
# sale" click. Set false to go back to the fully manual buttons.
EBAY_ORDER_AUTOMATION = os.getenv("EBAY_ORDER_AUTOMATION", "true").strip().lower() == "true"
# How far back each poll looks for orders eBay has changed (paid,
# shipped, cancelled) - re-checking an order is harmless, since every
# automatic move first checks the item's current status.
EBAY_ORDER_AUTOMATION_LOOKBACK_DAYS = int(os.getenv("EBAY_ORDER_AUTOMATION_LOOKBACK_DAYS") or "3")

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

# Condition IDs eBay broadly accepts across most categories - the fallback
# check (ebay_listing_rules.resolved_condition_id) used when eBay's live
# per-category condition lookup (ebay_api.resolve_condition_id_with_live_check)
# fails. eBay's own official ConditionEnum reference is explicit
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

# Auction durations (fixed-price listings are always Good 'Til Cancelled),
# stored in eBay's classic "Days_N" form and translated for the Inventory API
# in ebay_api.py. Shown as a Discord select menu, only when the reviewer
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

# ---- Removed eBay CSV / Facebook Marketplace batch paths ----
# Every eBay listing goes through the API now. These two folders are only
# kept so backups (backup.py) still include the CSV files those removed
# paths archived, if any exist - nothing writes to them anymore.
EBAY_BATCH_ARCHIVE_DIR = os.getenv("EBAY_BATCH_ARCHIVE_DIR", "data/ebay_batch_archive")
FB_MARKETPLACE_BATCH_ARCHIVE_DIR = os.getenv("FB_MARKETPLACE_BATCH_ARCHIVE_DIR", "data/fb_marketplace_batch_archive")

# Fallback weight (ebay_listing_rules.estimated_weight_lb), only used when an
# item has no real captured weight_lb (older items approved before weight
# was required). Required once a Calculated-shipping
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

# Keyword -> eBay Aspect "Type" value, used to fill Type when an item has
# no Type in its own item_specifics. Matched against the item's title
# (lowercased) - first match wins - see ebay_listing_rules.infer_type;
# ebay_api._build_aspects falls back to "Does Not Apply" when nothing here
# matches.
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

# ---- Pirate Ship CSV export (see pirate_ship_csv.py) ----
# Only for "Other"-platform sales (website, etc.) that were sold outside
# eBay - eBay sales don't need this, since Pirate Ship pulls those directly
# via its own native eBay integration. There's no separate "add to batch"
# step: /pirate-ship export-batch queries
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
# so /finance log-sale posts ONLY a Sales Receipt at sale time. No per-item
# cost/COGS figure is entered or tracked anywhere - the full cost was
# already recognized at purchase time, so there's nothing left to book (or
# track) at sale time.
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
# One Product/Service per sales channel, so the P&L splits Sales into eBay
# Sales / In-Person Sales / Other Sales. Needed because QuickBooks books a
# Sales Receipt line to its Item's own income account and ignores the
# per-line account override above. Each Item's income account is set in
# QuickBooks (eBay Sale -> eBay Sales 98, In-Person Sale -> In-Person Sales
# 114, Other Sale -> Other Sales 115). Blank = keep using
# QUICKBOOKS_CASH_SALES_ITEM_ID for that channel, exactly as before.
# "Other" covers every platform that isn't eBay or In Person.
QUICKBOOKS_EBAY_SALES_ITEM_ID = os.getenv("QUICKBOOKS_EBAY_SALES_ITEM_ID", "").strip()
QUICKBOOKS_IN_PERSON_SALES_ITEM_ID = os.getenv("QUICKBOOKS_IN_PERSON_SALES_ITEM_ID", "").strip()
QUICKBOOKS_OTHER_SALES_ITEM_ID = os.getenv("QUICKBOOKS_OTHER_SALES_ITEM_ID", "").strip()
QUICKBOOKS_COGS_ACCOUNT_ID = os.getenv("QUICKBOOKS_COGS_ACCOUNT_ID", "48")
QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID = os.getenv("QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID", "24")
# The business bank account (Old National checking once it's set in .env):
# deposit target for sales logged as already_deposited=True, the credit side
# of a claimed invoice's Journal Entry (cogs/pallet_setup.py), and the bank
# balance on /finance overview and #finance-dashboard. The default 8 is the
# old "Cash" account - set QUICKBOOKS_BANK_ACCOUNT_ID to the real bank
# account's ID (/finance list-accounts); /ebay status flags it if not.
QUICKBOOKS_BANK_ACCOUNT_ID = os.getenv("QUICKBOOKS_BANK_ACCOUNT_ID", "8")

# ---- eBay fee expenses (see ebay_fee_expenses.py) ----
# An eBay sale's Sales Receipt books the buyer's full order total into
# Undeposited Funds, but eBay deposits less - it keeps its selling fees,
# eBay-bought shipping labels, and ad fees. Each "Confirm & log fees" card
# in #ebay-orders posts a Journal Entry: debit these expense accounts,
# credit QUICKBOOKS_UNDEPOSITED_FUNDS_ACCOUNT_ID, so what's left matches the
# real payout. Deliberately NO default for the fees account - this is real
# money, and the right account only exists in your own QuickBooks company.
# Find IDs with /finance list-accounts account_type:"All account types".
# Until QUICKBOOKS_EBAY_FEES_ACCOUNT_ID is set, the button refuses to post.
QUICKBOOKS_EBAY_FEES_ACCOUNT_ID = os.getenv("QUICKBOOKS_EBAY_FEES_ACCOUNT_ID", "").strip()
# Optional - eBay shipping labels / promoted-listing ad fees go to the fees
# account above unless these name a separate account.
QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID = os.getenv("QUICKBOOKS_EBAY_SHIPPING_ACCOUNT_ID", "").strip()
QUICKBOOKS_EBAY_ADS_ACCOUNT_ID = os.getenv("QUICKBOOKS_EBAY_ADS_ACCOUNT_ID", "").strip()
# Background job that refreshes eBay's fee data and posts the cards. Needs
# the sell.finances scope on EBAY_REFRESH_TOKEN (same as /ebay sync-finances).
EBAY_FEE_CARDS = os.getenv("EBAY_FEE_CARDS", "true").strip().lower() == "true"
EBAY_FEE_SYNC_MINUTES = int(os.getenv("EBAY_FEE_SYNC_MINUTES") or "60")
# A card waits until the order's items are Shipped (labels are usually
# bought by then) or this many days have passed, so one card covers the
# fees AND the label instead of two cards per order.
EBAY_FEE_CARD_DELAY_DAYS = int(os.getenv("EBAY_FEE_CARD_DELAY_DAYS") or "5")
# Cards are only posted automatically for orders placed on/after this date
# (YYYY-MM-DD). Blank = the day the feature first runs, so past orders don't
# flood #ebay-orders - use /ebay fee-cards since:<date> for those.
EBAY_FEE_CARDS_START_DATE = os.getenv("EBAY_FEE_CARDS_START_DATE", "").strip()

# ---- eBay price sync (price_sync.py) ----
# Copies each Listed item's live eBay price into the bot, so a price changed
# on eBay (Seller Hub, eBay's price suggestions, ...) also changes it in
# Discord, on the web dashboard, the website checkout (Stripe) and the
# pallet's value still to sell. Fixed-price listings only.
EBAY_PRICE_SYNC = os.getenv("EBAY_PRICE_SYNC", "true").strip().lower() == "true"
EBAY_PRICE_SYNC_MINUTES = max(15, int(os.getenv("EBAY_PRICE_SYNC_MINUTES") or "60"))
# The website catalog (GET /shop/catalog - ebay_catalog.py) is served from
# the last complete active-listings pass; it's refreshed every
# EBAY_CATALOG_SYNC_MINUTES and the catalog answers 503 once that pass is
# older than EBAY_CATALOG_MAX_AGE_MINUTES.
EBAY_CATALOG_SYNC_MINUTES = max(5, int(os.getenv("EBAY_CATALOG_SYNC_MINUTES") or "20"))
EBAY_CATALOG_MAX_AGE_MINUTES = max(15, int(os.getenv("EBAY_CATALOG_MAX_AGE_MINUTES") or "60"))
# Disposable test items the website may check out WITHOUT a live eBay
# listing (bot price, quantity 1) - honored only while STRIPE_SECRET_KEY is a
# sk_test_ key, so it can never sell anything for real. Comma-separated SKUs.
SHOP_TEST_SKUS = frozenset(s.strip().lower() for s in (os.getenv("SHOP_TEST_SKUS") or "").split(",") if s.strip())

# ---- eBay refunds, account charges and payouts (see ebay_books.py) ----
# Refunds/returns: the income account a refund is booked against (debited).
# Blank = eBay Sales (QUICKBOOKS_EBAY_SALES_ACCOUNT_ID) for eBay refunds and
# Sales (QUICKBOOKS_SALES_INCOME_ACCOUNT_ID) for /finance refund. Point it at
# "Refunds & discounts to customers" to see refunds on their own P&L line.
QUICKBOOKS_REFUNDS_ACCOUNT_ID = os.getenv("QUICKBOOKS_REFUNDS_ACCOUNT_ID", "").strip()
# Post a card in #ebay-orders for each eBay payout that's reached the bank,
# which records the QuickBooks Bank Deposit from Undeposited Funds into
# QUICKBOOKS_BANK_ACCOUNT_ID. Same start date as the fee cards.
EBAY_PAYOUT_CARDS = os.getenv("EBAY_PAYOUT_CARDS", "true").strip().lower() == "true"

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
