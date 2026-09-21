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
  shared Automated Review channel.
- In **Automated Review** (shared, bot-only), a vision model identifies the
  item, drafts a title/description, and flags anything inconsistent between
  the note and the photo - Claude's cloud API by default, or a local Ollama
  model (see "Running with the Ollama backend" below) if you'd rather avoid
  per-item API cost. **Photos are never edited or regenerated.** This step
  is optional - see "Running without AI review" below.
- In **Queue Review** (shared), the Queue Review role approves, edits, or
  rejects (sends back to that item's own pallet's Data Entry channel).
  Approving walks through three steps to capture everything needed to list
  the item on eBay later: a condition dropdown (`config.EBAY_CONDITIONS` -
  New / New other / New with defects / Used / For parts, with "New other"
  pre-selected since most liquidation items land there), a category dropdown
  (`config.EBAY_CATEGORIES`, sorted by how often each has actually been
  picked so your most-used categories stay on top), then a short form for
  title, price, and freeform item specifics (brand/size/color/etc). Only
  title/category/condition/price are required; specifics can be filled in
  later. Description and photos are reused as-is from Data Entry.
- **Awaiting Listing** (shared) is where Listing Management actually lists
  the item, via whichever of three buttons fits:
  - **Add to eBay Batch** - appends the item (using the eBay data captured
    above) as a row to a local CSV matching eBay's Seller Hub bulk-upload /
    File Exchange template, no API call. Moves the item to
    **#pending-ebay-upload** until an admin runs `/ebay export-batch` to
    grab the file, upload it in Seller Hub, and (once eBay actually shows it
    live) `/ebay confirm-listed` to move it into #listed.
  - **List on eBay (API)** - the direct eBay API path. Only shows up once
    `EBAY_ENABLED` is true (see "Running without the eBay API" below);
    currently a stub pending eBay developer API approval.
  - **Mark Listed (Other)** - unchanged manual path for FB Marketplace,
    website, or anywhere else - moves straight to #listed.
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

- **Purchase Management** sets a pallet's total cost with `/setprice`.
- **Finance Management** does everything Purchase can, plus:
  - `/finance record-sale` - records (or corrects) an item's actual sale
    price + platform, whenever the price is actually known. Not tied to the
    Mark as Sold click at all.
  - `/finance override-count` / `/finance clear-count-override` - manually
    corrects the "items received" figure if the real/accounting count needs
    to differ from what's been logged in Data Entry (e.g. junk that was
    never entered).
  - `/finance summary` - posts a fresh, non-pinned copy of the same numbers.

Every pallet's **#pallet-discussion** channel gets a pinned card, posted as
the very first message, that shows: cost, items received, cost-per-item,
revenue so far, items priced, profit/loss vs. cost, a cost-recovery
progress bar, and an item count per pipeline stage. This card **edits
itself in place** every time something relevant changes - a new item is
logged, an item moves stage, a price is set or corrected, or the received
count is overridden. You never have to run a command to see current status;
just look at the pinned message.

## Important limitation: no Vendoo API, and eBay has two paths

Vendoo does not offer a public developer API, so this bot **cannot** push a
listing into Vendoo automatically - use **Mark Listed (Other)** for those
(and FB Marketplace, website, or anywhere else) after listing manually.

eBay is different: this bot ships with a CSV batch fallback (**Add to eBay
Batch**, matching eBay's Seller Hub bulk-upload / File Exchange template)
that works today with no API access at all, plus a direct-API path (**List
on eBay (API)**) that's wired in but stubbed out (see `ebay_api.py`) until
the eBay developer API application is approved - see "Running without the
eBay API" below.

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

Once it's running and logged in, run these slash commands **once, in this
order**:

```
/setup-shared-channels    (in any channel - creates the shared pipeline category/channels)
/setup-hub                (in #new-pallet-tracking - posts the Start New Pallet button)
```

`/setup-shared-channels` must be run before anyone clicks **Start New
Pallet** - the button will refuse and tell you to run it first if you
forget.

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

On first use, the bot automatically creates a second, small model on top of
your pulled one - e.g. `moondream-pallet-bot-ctx4096:latest` - via a
one-line Modelfile (`FROM moondream` + `PARAMETER num_ctx 4096`). This isn't
something you need to set up yourself: vision models like `moondream` don't
reliably honor Ollama's per-request context-size override (a known Ollama
limitation), so the only reliable fix is baking a larger context window
into the model itself. You'll see this extra model in `ollama list` - it's
normal, and safe to `ollama rm` (the bot just recreates it on the next
review). `OLLAMA_NUM_CTX` (default `4096`, see `.env.example`) controls the
size; raise it if you ever see a "request (N tokens) exceeds the available
context size" error again, which just means images/prompts have grown past
the current window.

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

### Running without R2

Leave `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
`R2_BUCKET_NAME`, and `R2_PUBLIC_URL_BASE` blank in `.env` (the default).
Data Entry still saves photos locally either way - that's what Discord item
cards render from - it just skips uploading a second copy to R2, and the
eBay CSV batch's `PicURL` column stays blank (see the known limitation
below). Once all five values are set, restart the bot: every photo saved
from then on gets uploaded to R2 automatically, no other setup needed.
Photos saved before R2 was configured are not retroactively uploaded.

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

1. A Pallet Admin runs `/setup-shared-channels` once, ever (skip if already done).
2. Anyone clicks **Start New Pallet** in `#new-pallet-tracking`, enters a
   name (e.g. `Pallet-2026-014`) and optional notes. This creates
   `#pallet-discussion` (with the pinned live status card) and `#data-entry`.
3. **Purchase Management** runs `/setprice` inside the new pallet's category
   to record what was paid.
4. **Data Entry** posts one message per item in that pallet's `#data-entry`:
   attach photo(s), type a short note in the same message, send. The card
   automatically updates - "Items Received" ticks up.
5. Within a few seconds it reappears in the shared `#queue-review`, labeled
   with its pallet, with an AI-suggested title/description and any flags.
6. **Queue Review** clicks **Edit** or **Reject / Send Back** (returns to
   that pallet's own Data Entry), or **Approve** - which asks for the eBay
   condition (dropdown, defaults to "New other"), then the eBay category
   (dropdown, your most-used categories first), then opens a form for eBay
   title, price, and item specifics (title/category/condition/price are
   required; specifics can be left blank).
7. Approved items land in the shared `#awaiting-listing`. **Listing
   Management** picks one of three buttons:
   - **Add to eBay Batch** - appends the item to the local eBay CSV batch, no
     API call. Moves it to `#pending-ebay-upload` to wait for someone to
     actually upload that CSV.
   - **List on eBay (API)** - only shown if the eBay developer API is
     configured and enabled (see "Running without the eBay API" above).
   - **Mark Listed (Other)** - for FB Marketplace, website, or anywhere else
     listed manually.
8. Items added to the eBay batch: a Pallet Admin runs `/ebay export-batch`
   whenever ready, uploads the attached CSV in eBay Seller Hub, then once
   eBay actually shows those listings live, runs `/ebay confirm-listed` to
   move them from `#pending-ebay-upload` into `#listed`.
9. However it got there, `#listed` items get a **Mark as Sold** button - one
   click, no price prompt.
10. **Finance Management** runs `/finance record-sale` (in that pallet's
    category, any time - immediately or days later) to log the actual price.
    The pinned card updates: revenue, profit/loss, and cost-recovery bar.
11. Once shipped, click **Mark as Shipped** in `#sold`.
12. If anything sits in `#listed` for 10+ days, `#10-day-alerts` pings,
    naming the pallet.
13. Anytime, check the pinned message at the top of a pallet's
    `#pallet-discussion` for full current status, or run `/finance summary`
    for a fresh non-pinned copy.

---

## 4. Admin commands (Pallet Admin role)

- `/item delete <item_number>` - run inside a pallet's category. Soft-deletes
  the item (best-effort deletes its current card from whichever channel it's
  in) but keeps the full row and event history in the database for audit.
- `/pallet archive` - run inside a pallet's category. Asks for confirmation,
  then deletes that pallet's own channels (`#pallet-discussion`,
  `#data-entry`). All item data stays in the database permanently - this
  only removes the Discord side. If items are still mid-pipeline, they stay
  visible in the shared channels (still labeled by pallet) even after the
  pallet's own category is gone.
- `/pallet list [include_archived]` - shows every pallet with item counts by
  stage and active/archived status.
- `/db-wipe` - **irreversible.** Requires both the Pallet Admin role AND real
  Discord Administrator permission on the server (two independent locks).
  Opens a form requiring you to type `DELETE EVERYTHING` exactly. On
  confirmation: deletes every pallet's category/channels, purges messages
  from the shared pipeline channels (best-effort - Discord won't bulk-delete
  messages older than 14 days), and drops/recreates every database table.
  The shared pipeline channels themselves are NOT deleted, since they're
  reusable infrastructure - only their message history is cleared.
- `/ebay export-batch` - downloads the accumulated eBay CSV batch as a
  Discord attachment, then archives and clears it so the next **Add to eBay
  Batch** click starts a fresh file.
- `/ebay confirm-listed [item_number]` - run inside a pallet's category.
  Confirms that item (or, with no `item_number`, every item in that pallet
  still waiting) actually went live on eBay after a CSV batch upload, moving
  it from `#pending-ebay-upload` to `#listed`. There's no live API to detect
  this automatically, so it's a manual confirmation after checking Seller Hub.

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
  pipeline channels, set once by `/setup-shared-channels`
- **items** - one row per physical item: status, AI-generated text, sale
  price/platform, local + R2 public photo URLs, and every relevant timestamp
- **ebay_listing_data** - one row per item, captured during Queue Review
  approval: eBay title, category ID, condition, price, and item specifics
  (stored as JSON, since which attributes apply varies by category)
- **ebay_category_usage** - one row per eBay category ID ever picked, with a
  running count - lets the category select menu sort `config.EBAY_CATEGORIES`
  by actual usage instead of a fixed order
- **item_events** - an append-only audit log of every status change and
  price recording, including deletions

The eBay CSV batch itself is **not** in the database - it's a plain CSV file
at `data/ebay_batch.csv` (see `ebay_csv.py`), archived to
`data/ebay_batch_archive/` on each `/ebay export-batch`.

### Backing this up
Since this is the only copy of your item/financial history, set up a
periodic backup of the `data/` folder (database, all saved item photos, and
the eBay batch CSV + its archive) - a daily `cron` job copying it elsewhere,
or syncing to cloud storage, is enough at this scale.

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
- **`/setup-shared-channels` is a one-time setup step.** If you ever need to
  move or recreate the shared channels, you'd need to update the
  `shared_channels` table manually (there's no command for this yet, since
  it should rarely be needed). If you're adding this after already running
  the bot, run it again to create just the new `#pending-ebay-upload`
  channel - it skips any channel that already exists.
- **The eBay CSV batch's photo URLs depend on R2 being configured.** Without
  `R2_ENABLED` (see "Running without R2" above), `PicURL` is left blank in
  the exported CSV - attach photos in Seller Hub yourself, or fill `PicURL`
  in before uploading. With R2 configured, it's filled in automatically.
- **`/ebay confirm-listed` is a manual step.** There's no live eBay API to
  automatically detect that an uploaded CSV batch was actually processed, so
  someone has to check Seller Hub and confirm it by hand.
