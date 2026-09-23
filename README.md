# 4 Guys 1 Pallet — Pallet Tracking Bot

A Discord bot that turns pallet intake into a role-gated pipeline, with a
live, auto-updating cost/profit card per pallet:

```
#pallet-discussion (per pallet, live finance/status card pinned)
#data-entry (per pallet)
        ↓
[ SHARED across every pallet ]
#automated-review (AI) → #queue-review → #awaiting-listing → #listed → #sold
                                                    ↓               ↑      ↓
                                          #pending-ebay-upload ------      #10-day-alerts
```

Only **#pallet-discussion** and **#data-entry** are created per pallet.
Everything from Automated Review onward is ONE channel shared by every
pallet - this is deliberate, to keep total channel count from scaling with
the number of pallets you run (Discord servers cap at 500 channels total).
Every item's card always shows which pallet it belongs to.

## What this bot does

- Posts a **"Start New Pallet"** button. Clicking it creates a category with
  **#pallet-discussion** (pinned live finance card) and **#data-entry**.
- In **Data Entry**, the Data Entry role posts a photo + short note per item.
  The bot logs it, deletes the original message, and sends it into the
  shared Automated Review channel. Got several of the exact same physical
  item? Start the note with `3x ` (e.g. `3x cordless drill, new in box`) -
  or just say `3 of the same`/`3 of these` anywhere in it - and one
  submission logs 3 separate items instead of one, each independently
  tracked (its own price, sale, and shipping) from that point on. A single
  AI review call covers all of them (they're identical by definition), not
  one per item. `/item duplicate` (see "Admin commands" below) does the
  same split manually, for an item already sitting in Queue Review.
- In **Automated Review** (shared, bot-only), a vision model identifies the
  item, drafts a title/description (ending with a short standard
  liquidation-sale disclaimer - sold as-is, not individually tested for
  full functionality, buyer should review photos for minor cosmetic wear),
  flags anything inconsistent between the note and the photo, and suggests
  an eBay category (a short product-type phrase like "cordless drill",
  matched against eBay's real ~18,000-category taxonomy - see
  `ebay_taxonomy.py` - rather than a fixed list), a starting price, and a
  rough shipping weight/dimensions (from a visual estimate off the photo) -
  Claude's cloud API by default, or a local Ollama model (see "Running with
  the Ollama backend" below) if you'd rather avoid per-item API cost.
  **The suggested price and weight/dimensions are rough guesses, not real
  market data or actual measurements** - they're only ever pre-fills Queue
  Review can accept or change, never treated as final (see "Known
  limitations" below). **Photos are never edited or regenerated.** This
  step is optional - see "Running without AI review" below.
- In **Queue Review** (shared), the Queue Review role approves, edits, or
  rejects (sends back to that item's own pallet's Data Entry channel).
  Approving walks through a few steps to capture everything needed to list
  the item on eBay later: a condition dropdown (`config.EBAY_CONDITIONS` -
  New / New other / New with defects / Used / For parts, with "New other"
  sorted to the top since most liquidation items land there), then either a
  category step *or*, when Automated Review suggested a category, an
  automatic skip past it - the AI's guess is matched against eBay's real
  taxonomy and applied, shown as text, with a "Change category" button if
  you disagree. The manual category step itself is a quick-pick dropdown of
  categories this team has actually used before (sorted by proven success,
  see "Known limitations" below) plus a "🔍 Search categories" button
  that searches all ~18,000 real eBay categories by keyword - not a fixed
  dropdown, since no static list could cover everything a liquidation
  pallet might contain. Then a format dropdown (Fixed Price or Auction - decided per item, not a global switch;
  Auction adds one more step for the listing duration: 3/5/7/10 days), then
  a short form for title, price/starting bid, shipping weight (lb) and
  packaged dimensions (L x W x H, in inches) - all pre-filled with the AI's
  suggestions when there are any, always editable - and freeform item
  specifics (brand/size/color/etc). Title/category/condition/format/price/
  weight/dimensions are all required - weight/dimensions feed the Pirate
  Ship export (eBay's own CSV upload doesn't carry shipping data at all,
  see below); specifics can be filled in later.
  Description and photos are reused as-is from Data Entry. (None of these
  dropdowns pre-check an option: Discord's mobile client doesn't reliably register a
  tap on an option that's already marked selected, so the "recommended"
  option is only ever sorted first or applied automatically - never a
  checkmark you have to re-tap.)
- **Awaiting Listing** (shared) is where Listing Management actually lists
  the item, via whichever of three buttons fits:
  - **Add to eBay Batch** - appends the item (using the eBay data captured
    above) as a row to a local CSV matching eBay's newer AI-prefill bulk
    listing template (SKU/photos/title/category/aspects - see "eBay CSV
    format" below), no API call. Moves the item to **#pending-ebay-upload**
    until an admin runs `/ebay export-batch` to grab the file, upload it via
    Seller Hub's Reports tab, and (once eBay actually shows it live)
    `/ebay confirm-listed` to move it into #listed.
  - **List on eBay (API)** - the direct eBay API path. Only shows up once
    `EBAY_ENABLED` is true (see "Running without the eBay API" below);
    currently a stub pending eBay developer API approval.
  - **Mark Listed (Other)** - unchanged manual path for FB Marketplace,
    website, or anywhere else - moves straight to #listed. For these,
    **Finance Management** can also run `/finance set-shipping-info` once a
    buyer's address is known, feeding `/pirate-ship export-batch` (see
    below) - eBay sales never need this, since Pirate Ship pulls those
    directly via its own native eBay integration.
- **Listed** (shared) items get a "Mark as Sold" button - a single click,
  no form, no price prompt. Operational speed was the whole point of
  removing price entry from this button.
- **Sold** (shared) items get a "Mark as Shipped" button (stays in the same
  channel, just checks it off).
- A background job checks every 12 hours for anything sitting in Listed for
  10+ days and pings the shared **10-Day Alerts** channel, naming the pallet.

## Financial tracking: decoupled from the operational buttons

Two new roles handle cost/profit tracking **completely separately** from the
Data Entry → Sold pipeline, so nobody doing warehouse work ever has to stop
and type a number:

- **Purchase Management** sets a pallet's total cost with `/finance setprice`.
- **Finance Management** does everything Purchase can, plus:
  - `/finance record-sale` - records (or corrects) an item's actual sale
    price + platform, whenever the price is actually known. Not tied to the
    Mark as Sold click at all.
  - `/finance refund` - logs a refund against an item's sale. Doesn't touch
    the original sale price (that sale still happened) - refunds net out
    separately against revenue everywhere it's shown.
  - `/finance expense` - logs a cost against a pallet (packaging, listing
    fees, etc.), optionally tied to one item.
  - `/finance reverse-sale` - undoes an item's recorded sale (duplicate
    entry, a sale that fell through) - clears its sale price but keeps a
    record of what was reversed and why.
  - `/finance history` - lists a pallet's recent refunds/expenses/reversals.
  - `/finance set-shipping-info` - opens a two-step form for a buyer's
    recipient name and structured address (address lines, city, state,
    postal code, country) on a non-eBay sale, feeding `/pirate-ship
    export-batch`. Decoupled from record-sale too, since the address often
    isn't known until after the price is agreed on.
  - `/finance override-count` / `/finance clear-count-override` - manually
    corrects the "items received" figure if the real/accounting count needs
    to differ from what's been logged in Data Entry (e.g. junk that was
    never entered).
  - `/finance summary` - posts a fresh, non-pinned copy of the same numbers.
  - `/finance pallet-summary [pallet_name]` / `/finance overview` - see
    "QuickBooks Online integration" below (useful with or without
    QuickBooks connected - the cost-by-type breakdown and pallet-progress
    counts don't require it, only the account balance does).

Every pallet's **#pallet-discussion** channel gets a pinned card, posted as
the very first message, that shows: cost, items received, cost-per-item,
revenue so far, items priced, profit/loss vs. cost, a cost-recovery
progress bar, and an item count per pipeline stage. This card **edits
itself in place** every time something relevant changes - a new item is
logged, an item moves stage, a price is set or corrected, or the received
count is overridden. You never have to run a command to see current status;
just look at the pinned message.

## QuickBooks Online integration (optional)

Connects the bot to QuickBooks Online for automatic credit-card cost
tracking and Pirate Ship shipping-cost allocation. Entirely optional -
leave `QUICKBOOKS_CLIENT_ID`/`QUICKBOOKS_CLIENT_SECRET` blank in `.env` to
run without it, exactly as before this feature existed. **Read/allocate/
report only, by deliberate design** - nothing here can initiate a payment,
transfer, or card action (freeze, limit change, etc), and no partner
profit-distribution/payout calculation is built (a possible future feature,
not built without an explicit spec).

- **Connecting**: `/finance connect-quickbooks` (Pallet Admin) walks
  through the OAuth authorization URL, then has you paste the resulting
  redirect URL back in (the bot has no web server to receive it directly -
  the page failing to load after you approve access is expected). The
  refresh token is persisted in the database and auto-refreshed before
  every ~1 hour access-token expiry.
- **Credit card tracking**: every `QUICKBOOKS_POLL_MINUTES` (default 20),
  the bot checks the ONE configured `QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID`
  account for new charges and posts each one to `#credit-card-charges`
  with an **Allocate to a pallet** button - pick an active pallet (adds the
  charge to that pallet's cost basis and pushes a matching categorized
  expense back into QuickBooks, tagged with the pallet's name for
  traceability) or **New Pallet (not arrived yet)** (files it in
  `#awaiting-pallet-charges` until a real pallet is created, at which point
  its creation flow offers to attach any unclaimed charges - more than one
  at once, e.g. the purchase price plus a separate deposit/fee).
- **Cost basis is additive**: a pallet's total cost is `/finance setprice`'s
  manual lump sum PLUS every itemized charge/shipping cost allocated
  through this integration (`pallet_costs` table, one row per cost,
  broken down by type) - not a replacement, so `/finance setprice` keeps
  working exactly as it always has for pallets that never touch these
  newer workflows.
- **Pirate Ship shipping costs**: `/finance import-pirateship <csv>`
  (Pallet Admin) uploads a Pirate Ship order/shipping export; rows are
  matched to a specific sold item via the same `pallet-<id>-item-<number>`
  reference already used internally for eBay/Pirate Ship exports, and that
  shipping cost is allocated to the item's pallet automatically. Pirate
  Ship's exact export column names weren't available to pin down when this
  was built, so matching is deliberately flexible (scans every column for
  the reference and for a cost-looking value/header) rather than assuming
  one fixed layout; unmatched rows get a manual pallet-picker in the
  response instead of being silently dropped or guessed at.
- **Reporting**: `/finance pallet-summary [pallet_name]` shows one
  pallet's cost basis broken down by type, revenue, and profit/margin.
  `/finance overview` shows the connected QuickBooks account balance,
  month-to-date spend/revenue across the whole business, and how many
  pallets are in progress vs. fully sold out. The pallet's own live status
  card also auto-posts a plain warning message (not pinned, not spammy -
  only when a mutation actually crosses a threshold) if its running cost
  basis exceeds its manifest retail value estimate (sum of every item's
  AI-suggested price - a rough guess, used because it's the only
  "what should this be worth" figure the bot has without a human entering
  one by hand) or, once fully sold out, its realized margin drops below
  `QUICKBOOKS_MARGIN_WARNING_THRESHOLD_PCT` (default 20%).
- Run `/setup shared-channels` again (safe to re-run) to create
  `#credit-card-charges` and `#awaiting-pallet-charges` if you're adding
  this to a server that already ran that command before this feature
  existed - it only creates whatever's still missing.

## Important limitation: no Vendoo API, and eBay/FB Marketplace both use CSV batches

Neither eBay nor Facebook Marketplace offer a listing-creation API this bot
can call directly (Vendoo doesn't offer one at all) - **Add to eBay Batch**
and **Add to FB Marketplace Batch** both work by accumulating a CSV and
handing it to each platform's own bulk-upload tool instead. Both are the
primary path today. eBay additionally has a direct-API path (**List on eBay
(API)**) that's wired in but stubbed out (see `ebay_api.py`) until the eBay
developer API application is approved - see "Running without the eBay API"
below. **Mark Listed (Other)** is the manual fallback for anywhere else
entirely (a website, in person, etc.) after listing by hand.

---

## 1. Discord server setup (do this first, once)

### Create the roles
In Server Settings → Roles, create these six roles (exact names matter -
they're referenced in `config.py`):
- `Data Entry`
- `Queue Review`
- `Listing Management`
- `Purchase Management`
- `Finance Management`
- `Pallet Admin`

Queue Review, Listing Management, Purchase Management, and Finance
Management are all screen-based work - none of them require being
physically present for intake, so remote members can hold any of these
roles freely.

### Create the hub channel
Create a text channel named exactly `new-pallet-tracking` (matches
`HUB_CHANNEL_NAME` in `config.py`). This is where the "Start New Pallet"
button lives - it doesn't need to be inside any category.

### Create the bot application
1. Go to https://discord.com/developers/applications → **New Application**
2. Under **Bot**, click **Reset Token** and copy it - this is your `DISCORD_BOT_TOKEN`
3. Under **Bot**, enable these **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
4. Under **OAuth2 → URL Generator**, check scopes `bot` and `applications.commands`,
   and under Bot Permissions check: Manage Channels, Manage Roles, Send Messages,
   Manage Messages, Attach Files, Embed Links, Read Message History, Use Slash
   Commands. Copy the generated URL and open it to invite the bot to your server.
5. **Important:** in Server Settings → Roles, drag the bot's role **above** all
   six roles you created above - Discord won't let a bot manage permissions for
   roles ranked higher than its own.

### Get your Server ID
Enable Developer Mode (User Settings → Advanced), then right-click your
server icon → Copy Server ID. This is your `DISCORD_GUILD_ID`.

### Get an Anthropic API key (optional - can be added later)
Only needed for AI-drafted descriptions with the default cloud backend. The
bot runs fine without it - items skip straight from Data Entry to Queue
Review with the raw note. Sign up at https://console.anthropic.com, create a
key, add it to `.env` as `ANTHROPIC_API_KEY`, restart the bot. Billed
separately from any Claude.ai subscription - pay-as-you-go based on usage.
(There's also a local, free alternative - see "Running with the Ollama
backend" below.)

---

## 2. Running the bot

The easiest way: run `start_bot.bat` (Windows - double-click it, or make a
Desktop shortcut to it) or `./start_bot.sh` (Linux/macOS). Either one
creates the virtual environment and installs/updates dependencies
automatically, creates `.env` from `.env.example` and tells you to fill it
in if it's missing, checks `DISCORD_BOT_TOKEN` is actually set before
trying to start, and keeps the window open with the real error (instead of
flashing shut) if the bot exits - offering to restart it right there. Run
it again any time you want to start the bot, including after pulling
updates.

To do the same thing by hand instead:

```bash
cd pallet-bot
python -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: fill in DISCORD_BOT_TOKEN and DISCORD_GUILD_ID.
# ANTHROPIC_API_KEY can be left blank for now.

python bot.py
```

The bot refuses to start a second instance against the same data directory
(a lock file under `data/`, released automatically even if the process
crashes) - if `python bot.py` exits immediately saying another process
already holds the lock, an existing instance is still running against this
same `data/` directory somewhere.

Once it's running and logged in, run these slash commands **once, in this
order**:

```
/setup shared-channels    (in any channel - creates the shared pipeline category/channels)
/setup hub                (in #new-pallet-tracking - posts the Start New Pallet button)
/setup info-channel       (in any channel - creates #information with a guide to the bot)
```

`/setup shared-channels` must be run before anyone clicks **Start New
Pallet** - the button will refuse and tell you to run it first if you
forget. `/setup info-channel` is optional but recommended - see
"The #information channel" below.

### The #information channel

`/setup info-channel` creates (or reuses) a read-only **#information**
channel - visible to everyone regardless of role, but only a Pallet Admin
can post in it - and posts a multi-part guide (`information_content.py`)
covering what the bot does, the pipeline stage by stage, what each role can
do, and every slash command grouped by category (`/ebay`, `/finance`,
`/pirate-ship`, pallet/item management, `/admin`/`/setup`). Point anyone new
at it before they ask "how does this work?" in chat.

Safe to re-run any time - it clears its own previous guide messages first
(anything anyone else posted is left alone) and reposts fresh, so running it
again after a feature change keeps the guide accurate.

### Running without AI review

Leave `ANTHROPIC_API_KEY` blank in `.env` (and leave `AI_REVIEW_BACKEND` at
its default, `anthropic`). Every item submitted in Data Entry skips the AI
step and goes straight to Queue Review with the raw note as-is (Automated
Review still gets a short "passed through" message for a visible record).
To turn it on later: add the key to `.env` and restart - no code changes
needed.

### Running with the Ollama backend

An alternative to the cloud API: Automated Review can run entirely against
a local [Ollama](https://ollama.com) server instead, with no API key and no
per-item cost.

1. Install Ollama and pull a vision-capable model (`moondream` is the
   default - small and fast, good for a first try):
   ```bash
   ollama pull moondream
   ```
2. Make sure `ollama serve` is running (the default install usually starts
   it automatically) - it listens on `http://localhost:11434` by default.
3. In `.env`, set `AI_REVIEW_BACKEND=ollama`. `OLLAMA_BASE_URL` and
   `OLLAMA_VISION_MODEL` only need setting if you're not using the defaults
   above (a different port/host, or a different pulled model).
4. Restart the bot.

Ollama's own default context window (2048 tokens) is easy to exceed once
the system prompt, note, and an encoded image are all in one request -
`OLLAMA_NUM_CTX` (default `4096`, see `.env.example`) is passed as a
per-request option to give real headroom. Raise it if you ever see a
"request (N tokens) exceeds the available context size" error, meaning
images/prompts have grown past the current window.

**Expect a real quality/speed trade-off.** `moondream` is a ~1.8B-parameter
model built mainly for image captioning, not a general instruction-following
model like Claude - titles/descriptions will read rougher, mismatch-flagging
is less reliable, and depending on your hardware, generation may be similar
to or slower than the cloud call despite running locally. Queue Review's
**Edit** button exists for exactly this kind of touch-up. If a review ever
fails (Ollama not running, model not pulled, bad JSON out of the model), it
falls back to the raw submitted note, same as any other AI hiccup - nothing
gets stuck.

Switch back to the cloud model any time by setting `AI_REVIEW_BACKEND=anthropic`
(or removing the line - that's the default) and restarting - no code changes
needed either way.

### Running without the eBay API

Leave `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_DEV_ID`, and `EBAY_USER_TOKEN`
blank in `.env` (the default). **List on eBay (API)** simply won't appear on
Awaiting Listing cards - use **Add to eBay Batch** instead, which needs no
API access at all. Once eBay's developer API application is approved and all
four values are set, restart the bot and the API button reappears - though
`ebay_api.py` itself still needs a real integration written before it does
anything.

### eBay CSV format

The eBay batch CSV (`ebay_csv.py`) matches eBay's newer **AI-powered
Prefill Listing** bulk tool, not the older classic File Exchange "Add"
template - eBay's own template file is called
`eBay-taxonomy-mapping-template_US`. It's a 3-step workflow on eBay's side:

1. Upload the CSV **Add to eBay Batch** builds (via `/ebay export-batch`)
   through the **Reports** tab in Seller Hub. Every column in it is
   individually optional per eBay's own template instructions - there's no
   equivalent of the old "every row rejected without X" failures, so
   nothing here blocks `/ebay export-batch` from running.
2. eBay processes the file and returns a downloadable **recommendations**
   file with its own AI-suggested category, title, description, and item
   aspects for each item - this may take a few minutes; check the upload's
   status in Seller Hub. In practice, eBay's own AI leaves price, condition,
   quantity, and format blank for each item - that's what `/ebay
   fill-recommendations` is for (see below).
3. Run `/ebay fill-recommendations`, attaching that downloaded file - this
   bot fills in price/quantity/condition/format/duration for every item it
   recognizes, straight from what Queue Review already captured, and hands
   you back the completed file.
4. Review the result (eBay's suggestions AND this bot's fills), then
   re-upload that **same file** via the Reports tab to actually create the
   listings or drafts.

The file itself has an unusual, fixed shape eBay's own template requires
("do not change any formatting in the file") - two `#INFO` metadata rows
before the real header row, then exactly five columns: `Custom Label
(SKU)`, `Item Photo URL` (every R2-hosted photo URL for the item, up to
eBay's 24-photo cap, pipe-separated), `Title`, `Category` (free text per
eBay's own instructions - this bot fills in the real eBay category name
Queue Review resolved via `ebay_taxonomy.py`, which can only help eBay's
own suggestion), and `Aspects` (item specifics as pipe-separated
`Key=Value` pairs, e.g. `Brand=DeWalt|Voltage=20V`).

None of price/condition/shipping/weight needs to be (or is) in this upload
CSV - but Queue Review still captures price/condition/weight/dimensions
during approval regardless (see above), since it's useful data either way,
weight/dimensions specifically feed the Pirate Ship CSV export (see below),
and - critically - it's exactly what `/ebay fill-recommendations` uses to
fill in eBay's returned recommendations file automatically.

**`/ebay fill-recommendations`** (`ebay_recommendations.py`) takes eBay's
downloaded recommendations file (a `.xlsm`, a different, classic-style
per-item template eBay uses internally for this - `Template=eBay-
listings-template_EBAY_US`, one sheet per matched category named
`Cat-<CategoryName>...`) and, for every row whose `Custom Label (SKU)`
matches one of this bot's items, fills in Start price/Quantity/Condition
ID/Format/Duration from `ebay_listing_data` - never overwriting a cell
that already has something in it, whether that's eBay's own suggestion or
something already typed in by hand. `EBAY_ITEM_LOCATION`/
`EBAY_SHIPPING_SERVICE` (optional, see "Installation") fill Location/
Shipping service 1 option too, if set. Known limitation: saving through
this tool drops Excel's dropdown pick-lists (openpyxl doesn't support
re-saving them) - the data itself is unaffected, but a cell without its
dropdown UI anymore just needs a value typed directly if you want to
change it. Macros are preserved.

### FB Marketplace CSV format

The FB Marketplace batch CSV (`fb_marketplace_csv.py`) matches Facebook's
own "Use a spreadsheet for multiple listings on Facebook Marketplace" bulk
tool - a much simpler one-step upload than eBay's (no processing/
recommendations round trip): **Add to FB Marketplace Batch** accumulates
rows, `/fb-marketplace export-batch` hands you the file, you upload it
directly through Facebook's bulk listing tool, and it's live.

Columns: `Title` and `Price` reuse the exact same data captured for every
approved item during Queue Review's listing form (`ebay_listing_data`
isn't actually eBay-specific despite the table name - every approved item
gets a title/price captured there regardless of which platform it
eventually sells on), `Description` falls back from the AI-generated
description to the raw Data Entry note, and `Photo URL` (the first
R2-hosted photo URL, if any) is included even though it's not one of the
three columns Facebook's own docs list as required, since a listing with
no photo isn't realistically sellable. **Facebook's docs are explicit that
changing/removing the Title/Price/Description headers breaks the upload**
- if you check the file against a real template downloaded from Facebook
and the `Photo URL` header doesn't match what Facebook expects (or it
supports more than one photo per row and you want to use that), that's the
one column safe to rename/extend in `fb_marketplace_csv.py`. Facebook
auto-predicts each listing's category from Title/Description, so there's
no category column here at all - review its guesses once the upload's
live, from the listings screen.

Since Facebook doesn't hand back a results file the way eBay's Seller Hub
does, there's no `/fb-marketplace import-results` equivalent - once the
CSV is actually live, run `/fb-marketplace confirm-listed` by hand to move
those items out of `#pending-fb-marketplace-upload` into `#listed`.

### Running without R2

Leave `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET_NAME`, and `R2_PUBLIC_URL_BASE` blank in `.env` (the default).
Data Entry still saves photos locally either way - that's what Discord item
cards render from - it just skips uploading a second copy to R2, and the
eBay CSV batch's `Item Photo URL` column stays blank (see the known
limitation below). Once all five values are set, restart the bot: every
photo saved from then on gets uploaded to R2 automatically, no other setup
needed. Photos saved before R2 was configured are not retroactively
uploaded.

### Running it 24/7 on your friend's server

```ini
# /etc/systemd/system/pallet-bot.service
[Unit]
Description=4 Guys 1 Pallet Discord Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/pallet-bot
ExecStart=/path/to/pallet-bot/venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable pallet-bot
sudo systemctl start pallet-bot
```

**If the bot goes offline and comes back** (server reset, crash, manual
restart - whatever), every button on every item still mid-pipeline keeps
working. On every startup the bot reads the database for anything sitting
in Queue Review, Awaiting Listing, Listed, or Sold and re-registers its
buttons before anyone can click them. Nothing to configure - this just
works. New slash commands can take a minute or two to show up in Discord's
UI after a restart (a Discord client caching quirk, not a bug here).

---

## 3. How to use it day to day

1. A Pallet Admin runs `/setup shared-channels` once, ever (skip if already done).
2. Anyone clicks **Start New Pallet** in `#new-pallet-tracking`, enters a
   name (e.g. `Pallet-2026-014`) and optional notes. This creates
   `#pallet-discussion` (with the pinned live status card) and `#data-entry`.
3. **Purchase Management** runs `/finance setprice` inside the new pallet's category
   to record what was paid.
4. **Data Entry** posts one message per item in that pallet's `#data-entry`:
   attach photo(s), type a short note in the same message, send. The card
   automatically updates - "Items Received" ticks up.
5. Within a few seconds it reappears in the shared `#queue-review`, labeled
   with its pallet, with an AI-suggested title/description (with a short
   liquidation disclaimer appended) and any flags.
6. **Queue Review** clicks **Edit** or **Reject / Send Back** (returns to
   that pallet's own Data Entry), or **Approve** - which asks for the eBay
   condition (dropdown, "New other" sorted to the top), then the eBay
   category - applied automatically (with a "Change category" button) when
   the AI suggested one, otherwise a dropdown of your most-used categories -
   then Fixed Price or Auction (Auction adds a duration step: 3/5/7/10
   days), then opens a form for eBay
   title, price/starting bid (pre-filled with the AI's price estimate when
   there is one - always double-check it, it's a general-knowledge guess,
   not real market data), and item specifics
   (title/category/condition/format/price are required; specifics can be
   left blank).
7. Approved items land in the shared `#awaiting-listing`. **Listing
   Management** picks one of four buttons:
   - **Add to eBay Batch** - appends the item to the local eBay CSV batch, no
     API call. Moves it to `#pending-ebay-upload` to wait for someone to
     actually upload that CSV.
   - **Add to FB Marketplace Batch** - same idea, for Facebook's own CSV
     bulk-listing tool. Moves it to `#pending-fb-marketplace-upload`.
   - **List on eBay (API)** - only shown if the eBay developer API is
     configured and enabled (see "Running without the eBay API" above).
   - **Mark Listed (Other)** - for anywhere else entirely, listed manually.
8. Items added to a batch: a Pallet Admin runs `/ebay export-batch` or
   `/fb-marketplace export-batch` whenever ready, uploads the attached CSV
   on that platform, then once it actually shows those listings live, runs
   `/ebay confirm-listed` or `/fb-marketplace confirm-listed` to move them
   from `#pending-ebay-upload`/`#pending-fb-marketplace-upload` into
   `#listed`.
9. However it got there, `#listed` items get a **Mark as Sold** button - one
   click, no price prompt.
10. **Finance Management** runs `/finance record-sale` (in that pallet's
    category, any time - immediately or days later) to log the actual price.
    The pinned card updates: revenue, profit/loss, and cost-recovery bar.
    For a non-eBay sale, once the buyer's address is known, also run
    `/finance set-shipping-info` - that feeds `/pirate-ship export-batch`
    later. eBay sales don't need this at all.
11. Once shipped, click **Mark as Shipped** in `#sold`.
12. If anything sits in `#listed` for 10+ days, `#10-day-alerts` pings,
    naming the pallet.
13. Anytime, a Pallet Admin runs `/pirate-ship export-batch` to grab a
    shipping CSV for whatever non-eBay sales are waiting, for Pirate Ship's
    batch order import.
14. Anytime, check the pinned message at the top of a pallet's
    `#pallet-discussion` for full current status, or run `/finance summary`
    for a fresh non-pinned copy.

---

## 4. Admin commands (Pallet Admin role)

- `/item delete <item_number>` - run inside a pallet's category. Soft-deletes
  the item (best-effort deletes its current card from whichever channel it's
  in) but keeps the full row and event history in the database for audit.
- `/item duplicate <item_number> <count>` - run inside a pallet's category,
  for an item still sitting in Queue Review. Clones it into `count - 1` new,
  independently-tracked item cards (same note, AI title/description/
  category/price/weight/dimensions, and photos - reusing the same R2 URLs
  rather than re-uploading identical content), each posted fresh to Queue
  Review. The manual counterpart to Data Entry's `3x ...` shorthand above,
  for when that wasn't used or the count changes later. Capped at 25 new
  items per run as a sanity check against a typo.
- `/pallet archive` - run inside a pallet's category. Asks for confirmation,
  then deletes that pallet's own channels (`#pallet-discussion`,
  `#data-entry`). All item data stays in the database permanently - this
  only removes the Discord side. If items are still mid-pipeline, they stay
  visible in the shared channels (still labeled by pallet) even after the
  pallet's own category is gone.
- `/pallet list [include_archived]` - shows every pallet with item counts by
  stage and active/archived status.
- `/admin db-wipe` - **irreversible.** Requires both the Pallet Admin role AND real
  Discord Administrator permission on the server (two independent locks).
  Opens a form requiring you to type `DELETE EVERYTHING` exactly. On
  confirmation: deletes every pallet's category/channels, purges messages
  from the shared pipeline channels (best-effort - Discord won't bulk-delete
  messages older than 14 days), and drops/recreates every database table.
  The shared pipeline channels themselves are NOT deleted, since they're
  reusable infrastructure - only their message history is cleared.
- `/ebay export-batch` - downloads the accumulated eBay CSV batch as a
  Discord attachment, archives and clears it so the next **Add to eBay
  Batch** click starts a fresh file, and records a durable, numbered batch
  snapshot (`/ebay batches`/`/ebay batch`) of exactly which items went out
  in it. Every column in this newer prefill-template CSV is optional per
  eBay's own template, so nothing blocks this from running - see "eBay CSV
  format" above.
- `/ebay fill-recommendations <recommendations_file>` - attach the file
  eBay's Seller Hub gave back after processing your export-batch upload;
  this bot fills in Start price/Quantity/Condition ID/Format/Duration for
  every item it recognizes, straight from Queue Review data, and hands
  back the completed file to review and re-upload - see "eBay CSV format"
  above.
- `/ebay category-search <query>` - looks up real eBay leaf category IDs by
  keyword against eBay's own official taxonomy (`ebay_taxonomy.py`, ~18,000
  categories) - mainly for finding an ID to pass to `/ebay retry-item`'s
  `category_id`.
- `/ebay retry-item <item_number> [category_id] [condition_id] [specifics]
  [weight_lb] [dimensions]` - run inside a pallet's category. Re-queues an
  item into the current live CSV so the next `/ebay export-batch` picks it
  up again, with corrected data applied fresh. Pass `category_id` (look one
  up via `/ebay category-search`) to correct the category shown in the CSV,
  `specifics` as `Key=Value, Key2=Value2` to add/fix item specifics (feeds
  the CSV's `Aspects` column) - merged into whatever specifics are already
  saved, not a full replacement - and/or `condition_id` and `weight_lb`/
  `dimensions` (as `"12 x 8 x 4"`, in inches) to correct data captured
  during Queue Review that doesn't appear in this CSV but still feeds the
  Pirate Ship export and your own records. Correcting any one of these
  always preserves whatever weight/dimensions were already saved, even if
  this particular retry doesn't touch them.
  Skipping a needed correction just resubmits the same bad data and fails
  identically.
- `/ebay import-results <batch_id> <results_csv>` - reconciles a batch
  against the results/report CSV Seller Hub gives you after processing an
  upload. Column names in eBay's results CSVs vary, so this matches them
  loosely (a SKU-like column, a listing-ID-like column, an optional status/
  error column) rather than expecting one fixed layout. Rows it can
  confidently match as succeeded move that item to Listed and record the
  real eBay item ID; anything else (no listing ID, an explicit error, an
  unrecognized SKU) is reported back unresolved instead of guessed at - the
  response lists exactly what wasn't resolved.
- `/ebay batches` - lists recent batches with how many items in each are
  still waiting on a result. `/ebay batch <batch_id>` shows one batch's
  still-pending items.
- `/ebay confirm-listed [item_number]` - run inside a pallet's category.
  The fully-manual fallback to `/ebay import-results`: confirms that item
  (or, with no `item_number`, every item in that pallet still waiting)
  actually went live on eBay, moving it from `#pending-ebay-upload` to
  `#listed`, for anyone who'd rather just check Seller Hub directly than
  download/upload a results CSV.
- `/fb-marketplace export-batch` - downloads the accumulated FB Marketplace
  CSV batch as a Discord attachment and archives + clears it so the next
  **Add to FB Marketplace Batch** click starts a fresh file - see "FB
  Marketplace CSV format" above.
- `/fb-marketplace confirm-listed [item_number]` - run inside a pallet's
  category. The only way to move an item out of
  `#pending-fb-marketplace-upload`: confirms that item (or, with no
  `item_number`, every item in that pallet still waiting) actually went
  live on Facebook, moving it to `#listed`. There's no results-CSV
  reconciliation equivalent to `/ebay import-results` here - Facebook
  doesn't give one back.
- `/pirate-ship export-batch` - exports every sold item on a non-eBay
  platform ("Other"/FB Marketplace/website, recorded via `/finance
  record-sale`) that hasn't been exported yet, as a CSV for Pirate Ship's
  batch/spreadsheet order import, then marks them exported so a re-run
  doesn't duplicate them. Recipient name/address come from `/finance
  set-shipping-info`; items missing that get flagged in the response but
  are still included. Weight/package dimensions reuse the same data
  captured on Queue Review's eBay listing form (see above) - the
  human-confirmed value from approval when the item went through that flow,
  else Automated Review's AI estimate; still left blank for items that
  predate both. eBay sales are never included - Pirate Ship pulls those
  directly via its own native eBay integration.
- `/admin backup-now` - creates and verifies a local backup immediately (see
  "Backups" below); `/admin backups` lists recent ones with size and age.
- `/admin bind-role <role_name> <role>` / `/admin unbind-role <role_name>` /
  `/admin role-bindings` - optional role-ID bindings (see "Role bindings" below),
  editable live from Discord, no restart needed.
- `/pirate-ship purge-buyer-data [days] [confirm]` - previews (default) or,
  with `confirm:True`, clears recipient name/address from shipped items
  past `BUYER_DATA_RETENTION_DAYS` (default 90) days, and redacts matching
  rows in already-exported Pirate Ship CSV archives. Inventory identity,
  sale price, and audit history are never touched - only buyer contact
  info. CSVs/Discord attachments downloaded before a purge still have the
  old data; that needs separate manual cleanup.
- `/finance connect-quickbooks` - see "QuickBooks Online integration" above.
- `/finance import-pirateship <csv_file>` - see "QuickBooks Online
  integration" above.

## Role bindings

By default every permission check matches a role by **name** (e.g. a role
literally called "Pallet Admin") - simple, no setup needed, but it breaks
if that role ever gets renamed in Discord. `/admin bind-role <role_name> <role>`
binds one of this bot's six roles (Data Entry, Queue Review, Listing
Management, Purchase Management, Finance Management, Pallet Admin) to a
specific Discord role's ID instead, so a rename no longer matters. This is
entirely optional, takes effect immediately (no restart), and is stored in
`SETTINGS_PATH` (default `data/settings.json`) - `/admin unbind-role` reverts to
matching by name, and `/admin role-bindings` shows the current state. If a bound
role is later deleted, checks for it automatically fall back to matching
by name again rather than breaking outright.

## Backups

A verified zip snapshot (database, photos, eBay/Pirate Ship CSV archives)
is created automatically every `BACKUP_INTERVAL_HOURS` (default 24) while
the bot is running, and saved under `BACKUP_DIR` (default `backups/`).
`BACKUP_KEEP_COUNT`/`BACKUP_MAX_AGE_DAYS` (defaults: 14 snapshots / 14
days) bound how many pile up - whichever limit is hit first prunes the
oldest. The database is captured with SQLite's own backup API, not a plain
file copy, so a backup taken mid-write is still a consistent, valid
snapshot. Every backup's manifest is hash-checked right after it's
created, and again before any restore - a backup that fails that check is
never restored from, and a newly-created one that fails it is discarded
immediately rather than left on disk looking valid.

Backups contain real business/customer data - don't upload them anywhere
public. Restoring one is deliberately **not** a Discord command (it's the
one operation here that can put stale data back in place of current data)
- run it from the command line with the bot stopped:

```
python backup.py verify path/to/backup.zip
python backup.py restore path/to/backup.zip --destination path/to/new-data
```

Restore only ever writes into a new, empty directory - it refuses to touch
one that already has files in it, so a bad restore can't overwrite a
working installation. The restored directory has the same layout as
`data/` (`pallet_tracker.db`, `photos/`, `ebay_batch_archive/`,
`pirate_ship_exports/`); point `DATABASE_PATH`/`PHOTO_DIR`/etc at it, or
move its contents into your real `data/` directory, then restart the bot.

---

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

An early, incremental suite (not exhaustive) covering: core `database.py`
behavior (pallets/items, financials including refunds/expenses/reversals,
structured shipping addresses, buyer-data retention, the eBay batch
lifecycle), `ai_review.py`'s fallback shape and timeout path, `ebay_csv.py`/
`fb_marketplace_csv.py`/`pirate_ship_csv.py`'s row-building (including the
legacy-address fallback and buyer-data redaction), `ebay_results.py`'s
flexible results-CSV parsing, `runtime_settings.py`'s role-ID bindings,
`quickbooks.py`'s OAuth token exchange/refresh (rotation included) and API
request shaping (all against a fake `aiohttp.ClientSession`, no real
network calls), the credit-card charge → pallet allocation workflow and
awaiting-pallet-charge claiming (cogs/finance.py, cogs/pallet_setup.py),
`pirate_ship_import.py`'s flexible CSV column-scanning, the new financial
reporting queries (cost-by-type breakdown, month-to-date spend/revenue,
pallet in-progress/sold-out counts, the auto-flag early warnings), the FB
Marketplace batch/confirm-listed workflow (cogs/item_flow.py,
cogs/fb_marketplace.py), `discord_resilience.py`'s exception coverage
(including the exact DNS-failure class that exposed the bug it fixes), and
a regression guard that fails if a `discord.ui.SelectOption` in
`cogs/item_flow.py` is ever
marked `default=True` again - that reintroduces a real bug where Discord's
mobile client won't register a tap on an already-checked option. Every
test uses a throwaway temp database/file
paths (see `tests/conftest.py`) - running the suite never touches your
real `.env`, database, or local files. `.github/workflows/tests.yml` runs
this on every push.

---

## 5. Database and future migration

All state lives in a single SQLite file at `data/pallet_tracker.db` (see
`database.py`). Every function that touches the database lives in that one
file, so migrating to Postgres/MySQL later only means rewriting
`database.py`'s internals.

Tables:
- **pallets** - one row per pallet, with cost, items-received override,
  archive status, and the pinned finance message ID
- **channel_map** - per-pallet channel IDs (discussion, data-entry only)
- **shared_channels** - the one-row-per-stage mapping for the shared
  pipeline channels, set once by `/setup shared-channels`
- **items** - one row per physical item: status, AI-generated text (including
  its suggested eBay category name and rough price estimate), sale
  price/platform, non-eBay shipping recipient/address, local + R2 public
  photo URLs, and every relevant timestamp
- **ebay_listing_data** - one row per item, captured during Queue Review
  approval: eBay title, category ID, condition, format (fixed price or
  auction) + duration, price (or auction starting bid), and item specifics
  (stored as JSON, since which attributes apply varies by category)
- **ebay_category_usage** - one row per eBay category ID ever picked, with a
  running count - lets Queue Review's quick-pick category dropdown sort
  previously-used categories by actual usage (and, more importantly,
  proven success - see "Known limitations" below)
- **item_events** - an append-only audit log of every status change and
  price recording, including deletions
- **finance_transactions** - refunds/expenses/reversals logged via
  `/finance refund`/`/finance expense`/`/finance reverse-sale`
- **pallet_costs** - itemized costs allocated to a pallet from the
  QuickBooks/Pirate Ship workflows (one row per cost, typed as
  `credit_card`/`shipping`/`supplies`/`misc`) - additive on top of
  `pallets.pallet_cost`, not a replacement; see "QuickBooks Online
  integration" above
- **awaiting_pallet_charges** - QuickBooks charges allocated to "New
  Pallet (not arrived yet)" before that pallet exists, until a real
  pallet's creation flow claims them into `pallet_costs`
- **quickbooks_seen_charges** - every QuickBooks transaction ID the
  credit-card poller has ever shown in Discord, so re-polling never posts
  the same charge twice
- **quickbooks_connection** - the single-row OAuth token for the one
  connected QuickBooks company, auto-refreshed before every expiry

The eBay CSV batch itself is **not** in the database - it's a plain CSV file
at `data/ebay_batch.csv` (see `ebay_csv.py`), archived to
`data/ebay_batch_archive/` on each `/ebay export-batch`. The FB Marketplace
batch works the same way, at `data/fb_marketplace_batch.csv` / `data/
fb_marketplace_batch_archive/` (see `fb_marketplace_csv.py`). Pirate Ship
exports work similarly but with nothing accumulating between runs - each
`/pirate-ship export-batch` queries the database directly and writes
straight to `data/pirate_ship_exports/` (see `pirate_ship_csv.py`).

### Backing this up
Since this is the only copy of your item/financial history, set up a
periodic backup of the `data/` folder (database, all saved item photos, the
eBay batch CSV + its archive, and the Pirate Ship export archive) - a daily
`cron` job copying it elsewhere, or syncing to cloud storage, is enough at
this scale.

---

## 6. Known limitations / things to watch

- **No live sync with Vendoo.** Hard platform limitation, not a bug here.
- **AI review costs money per item on the default `anthropic` backend**
  (Claude API usage). Cheap at your current volume; monitor usage on the
  Anthropic console if it scales up. Switch to the local `ollama` backend
  (see "Running with the Ollama backend" above) to avoid this entirely, at
  the cost of noticeably rougher output quality.
- **The bot must stay running** for the pipeline to move - if it's offline
  when someone posts in Data Entry, that message just sits there until the
  bot is back up (no retroactive backlog scan in this version).
- **Role names must match `config.py` exactly.**
- **Buttons survive restarts** - see the "Running it 24/7" section above.
- **Double-click protection** - each stage-advancing action checks the
  item's current status first, so near-simultaneous clicks get a clear
  "already moved on" message instead of duplicating anything.
- **`/setup shared-channels` is a one-time setup step.** If you ever need to
  move or recreate the shared channels, you'd need to update the
  `shared_channels` table manually (there's no command for this yet, since
  it should rarely be needed). If you're adding this after already running
  the bot, run it again to create just the new `#pending-ebay-upload`
  channel - it skips any channel that already exists.
- **The eBay CSV batch's photo URLs depend on R2 being configured.** Without
  `R2_ENABLED` (see "Running without R2" above), `Item Photo URL` is left
  blank in the exported CSV - add photos yourself before or after
  uploading. With R2 configured, it's filled in automatically (every
  photo, pipe-separated, up to eBay's 24-photo cap).
- **`/ebay confirm-listed` is a manual step.** There's no live eBay API to
  automatically detect that an uploaded CSV batch was actually processed, so
  someone has to check Seller Hub and confirm it by hand.
- **eBay category selection uses eBay's own official taxonomy, not a
  hand-typed list.** `ebay_categories.json` (loaded by `ebay_taxonomy.py`)
  is generated from eBay's published category export (~18,000 real leaf
  categories) - every ID it can return is guaranteed real and listable, so
  there's no more equivalent of the old error-87 ("category selected is
  not a leaf category") failures this used to need hand-fixing. Both
  Automated Review's AI suggestion and Queue Review's manual "🔍 Search
  categories" step search this data by keyword (`ebay_taxonomy.search()`)
  rather than picking from a small fixed list - there are simply too many
  real eBay categories to hand the AI a fixed enum or fit in a single
  Discord dropdown (25-option cap). `/ebay category-search <query>` looks
  the same data up on demand, e.g. to find an ID for `/ebay retry-item
  <item_number> category_id:<corrected id>`. Since even a real category ID
  can turn out to be a poor fit or underperform for this business, Queue
  Review's quick-pick dropdown still sorts by what's actually **confirmed
  working**: once an item in a category is confirmed live (via `/ebay
  confirm-listed` or a matched success row in `/ebay import-results`), that
  category gets a "✅ " label and sorts ahead of everything else, including
  categories picked more often but never confirmed - the team naturally
  converges on proven-working categories over time without anyone tracking
  this by hand. To refresh `ebay_categories.json` from a newer eBay
  taxonomy export, see the regeneration notes in `ebay_taxonomy.py`'s
  module docstring.
- **Automated Review's suggested price is a general-knowledge guess, not
  real market data.** The model has no access to actual eBay sold listings
  for the item - it's only ever a Queue Review pre-fill, always editable,
  never authoritative. **Possible future improvement:** pull real "sold"
  comps via eBay's Browse API for a market-data-backed estimate instead
  (not implemented - would need its own eBay API credentials/calls beyond
  what `config.EBAY_ENABLED` currently gates).
- **Automated Review's estimated weight/dimensions is a rough visual guess,
  not an actual measurement.** The model only ever sees the item's photo -
  it's a starting point pre-filled on the eBay listing form at Queue Review
  approval, always human-confirmed or corrected before Approve, never
  treated as authoritative. A wrong value here has a real dollar cost via
  the Pirate Ship export (wrong weight/dims → wrong shipping label cost),
  even though it no longer affects eBay's own CSV upload directly. Items
  approved before weight/dimensions became required fields have neither a
  human-confirmed nor an AI-estimated value, and show up with blank
  weight/dims in a Pirate Ship export until corrected via `/ebay retry-item
  weight_lb:... dimensions:...`.
- `/finance set-shipping-info` captures a structured address (address lines,
  city, state, postal code, country) as of the fields themselves, but none
  of it is validated against a real address database - a typo'd city or zip
  saves exactly as typed. Items shipped before this structured form existed
  only have the old freeform address blob, which is still split into
  address lines best-effort for
  the CSV (city/state/zip are left blank for those older rows).
- **QuickBooks watches exactly one credit-card account, by design.** If
  your QuickBooks company has other connected accounts unrelated to this
  pallet business, they're never touched - `QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID`
  is a deliberate single-account scope, not a current limitation to widen
  later.
- **Pirate Ship CSV matching is flexible/best-guess, not pinned to a known
  column layout.** No real Pirate Ship export sample was available when
  this was built, so `pirate_ship_import.py` scans every column for a
  `pallet-<id>-item-<number>` reference and a cost-looking value/header
  rather than trusting one fixed schema. If a real export's columns don't
  match well in practice, tighten the matching in that module rather than
  guessing further - don't widen it speculatively.
- **No automatic partner profit distribution.** Flagged as a possible
  future feature, deliberately not built without an explicit spec for how
  payouts/splits should actually work.
- **QuickBooks integration is read/allocate/report only, on purpose.**
  Nothing in `quickbooks.py` (or anywhere else) can initiate a payment,
  transfer, or card action (freeze, limit change, etc) - a deliberate
  safety boundary, not an oversight to revisit.
- **FB Marketplace's CSV columns beyond Title/Price/Description aren't
  confirmed against a real downloaded template.** Facebook's own help docs
  list those three as required; `Photo URL` was added here as a reasonable
  best guess (only the first photo, since Facebook's exact multi-photo
  convention isn't confirmed either) since a listing with no photo isn't
  realistically sellable. If Facebook's bulk uploader rejects or ignores
  that column, or a real template shows a different header, fix the header
  name (and/or the single-photo assumption) in `fb_marketplace_csv.py` -
  never touch the `Title`/`Price`/`Description` headers, Facebook's docs
  are explicit that changing those breaks the upload entirely.
- **No results-file reconciliation for FB Marketplace, unlike
  `/ebay import-results`.** Facebook's bulk tool doesn't hand back a
  processed results file the way eBay's Seller Hub does, so
  `/fb-marketplace confirm-listed` (checking Facebook by hand) is the only
  way to move an item out of `#pending-fb-marketplace-upload`.
