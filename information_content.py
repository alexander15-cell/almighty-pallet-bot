"""
Content for the #information channel (posted by /setup info-channel in
cogs/pallet_setup.py) - a read-only guide explaining what Almighty Pallet
Bot does, how the pipeline works, who does what, and every command grouped
by category, so anyone new can read this instead of asking around.

Kept as its own module (rather than inline in the cog) since it's mostly
static content, not logic - build_embeds() is a pure function so it's easy
to keep in sync with real behavior as features change, and easy to test.
"""
import discord

COLOR = discord.Color.blurple()


def build_embeds() -> list:
    """Returns the ordered list of embeds to post, one per Discord message
    (a single embed can't fit this much content under Discord's 6000-char/
    25-field limits)."""
    return [
        _overview_embed(),
        _pipeline_embed(),
        _roles_embed(),
        _ebay_commands_embed(),
        _fb_marketplace_commands_embed(),
        _finance_commands_embed(),
        _shipping_commands_embed(),
        _pallet_item_commands_embed(),
        _admin_setup_commands_embed(),
    ]


def _overview_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📦 Welcome to Almighty Pallet Bot",
        description=(
            "This bot tracks every item from a liquidation pallet through the whole "
            "resale pipeline - intake, AI-assisted review, listing on eBay (or "
            "wherever else), and finally sold + shipped - with a live cost/profit "
            "card per pallet the whole time.\n\n"
            "Only **#pallet-discussion** and **#data-entry** are created per pallet. "
            "Every stage after that (Automated Review, Queue Review, Awaiting "
            "Listing, Listed, Sold, 10-Day Alerts) is **one shared channel used by "
            "every pallet at once** - keeps the server from drowning in channels as "
            "more pallets come in. Every item's card always shows which pallet it "
            "belongs to, so nothing gets lost in the shared channels.\n\n"
            "To start tracking a new pallet, click **Start New Pallet** in "
            "#new-pallet-tracking."
        ),
        color=COLOR,
    )
    embed.set_footer(text="See the next messages below for the pipeline, roles, and full command list.")
    return embed


def _pipeline_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔀 The Pipeline, Stage by Stage",
        description="An item moves through these automatically as each step is completed - nobody has to manually move a card.",
        color=COLOR,
    )
    embed.add_field(
        name="1️⃣ #data-entry (per pallet)",
        value=(
            "Data Entry posts a photo + short note, one message per item. The bot replies right "
            "there with the item number(s) assigned - write that on the box/sticker - then logs "
            "it, removes the message, and sends it into Automated Review. Got several of the "
            "exact same thing? Start the note with \"3x \" (or say \"3 of the same\"/\"3 of these\" "
            "anywhere in it) and the bot logs 3 separate items, each independently tracked (and "
            "numbered) from there. Replying to a rejected item's card (not posting a new message) "
            "resubmits the same item."
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ #automated-review (shared, bot-only)",
        value=(
            "A vision model drafts a title/description, flags anything the photo and note "
            "disagree on, and suggests a category, starting price, and rough shipping "
            "weight/dimensions. All of it is a rough guess a human confirms next - never final."
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ #queue-review (shared)",
        value=(
            "Queue Review approves, edits, re-reviews (AI), or rejects back to Data Entry. "
            "Approving walks through condition, category (AI match or manual search), "
            "fixed-price/auction format, then a final form for title/price/weight/dimensions/"
            "specifics. Re-review (AI) re-runs the AI pass (picking up any Edit correction) - "
            "for after fixing the description, or when the first pass errored/timed out."
        ),
        inline=False,
    )
    embed.add_field(
        name="4️⃣ #awaiting-listing (shared)",
        value=(
            "Listing Management actually lists it: **Add to eBay Batch** or **Add to FB "
            "Marketplace Batch** (CSV, no API - see the commands below), **List on eBay "
            "(API)** (once configured), or **Mark Listed (Other)** for anywhere else entirely."
        ),
        inline=False,
    )
    embed.add_field(
        name="5️⃣ #pending-ebay-upload / #pending-fb-marketplace-upload → #listed (shared)",
        value=(
            "Batch items wait here until confirmed live. Tap **Confirm Listed** right on the "
            "card once you see it live on eBay/Facebook, then it moves to #listed alongside "
            "everything else - no need to go find that pallet's own channel."
        ),
        inline=False,
    )
    embed.add_field(
        name="6️⃣ #listed → #sold → shipped",
        value=(
            "**Mark as Sold** is a single click, no price prompt - Finance Management records "
            "the actual price separately, at their own pace. **Mark as Shipped** checks it off. "
            "#10-day-alerts pings if anything sits in Listed for 10+ days."
        ),
        inline=False,
    )
    return embed


def _roles_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🧑‍🤝‍🧑 Roles - who does what",
        description="Ask a Pallet Admin to grant these Discord roles. Pallet Admin can always see/do everything.",
        color=COLOR,
    )
    embed.add_field(name="Data Entry", value="Posts new items in #data-entry.", inline=True)
    embed.add_field(name="Queue Review", value="Approves/edits/rejects items in #queue-review.", inline=True)
    embed.add_field(name="Listing Management", value="Lists approved items in #awaiting-listing; runs `/ebay`, `/fb-marketplace`, and `/pirate-ship` commands.", inline=True)
    embed.add_field(name="Purchase Management", value="Sets a pallet's total cost with `/finance setprice`.", inline=True)
    embed.add_field(name="Finance Management", value="Records sales, refunds, expenses, shipping info - everything under `/finance`.", inline=True)
    embed.add_field(name="Pallet Admin", value="Full access everywhere, plus `/admin`, `/item`, `/pallet`, and `/setup` commands.", inline=True)
    return embed


def _ebay_commands_embed() -> discord.Embed:
    embed = discord.Embed(title="💬 Commands - eBay (`/ebay ...`)", color=COLOR)
    embed.add_field(name="export-batch", value="Download the accumulated eBay CSV batch and start a fresh one.", inline=False)
    embed.add_field(name="fill-recommendations", value="Upload eBay's returned recommendations file - fills in price/quantity/condition/format from Queue Review data.", inline=False)
    embed.add_field(name="category-search <query>", value="Look up a real eBay category ID by keyword.", inline=False)
    embed.add_field(name="retry-item <item_number> ...", value="Fix and re-queue an item whose batch upload needs a correction.", inline=False)
    embed.add_field(name="requeue-pending", value="Bulk retry-item: re-queue EVERY item pending an eBay upload into a fresh batch, no corrections needed.", inline=False)
    embed.add_field(name="batches / batch <id>", value="List recent batches, or show one batch's still-pending items.", inline=False)
    embed.add_field(name="import-results <batch_id> <csv>", value="Reconcile a batch against a results CSV from Seller Hub.", inline=False)
    embed.add_field(name="confirm-listed [item_number]", value="Manually confirm an item is live on eBay (or tap **Confirm Listed** right on its card in #pending-ebay-upload).", inline=False)
    return embed


def _fb_marketplace_commands_embed() -> discord.Embed:
    embed = discord.Embed(title="💬 Commands - FB Marketplace (`/fb-marketplace ...`)", color=COLOR)
    embed.add_field(name="export-batch", value="Download the accumulated FB Marketplace CSV batch and start a fresh one.", inline=False)
    embed.add_field(name="confirm-listed [item_number]", value="Manually confirm an item is live on FB Marketplace (or tap **Confirm Listed** right on its card in #pending-fb-marketplace-upload).", inline=False)
    return embed


def _finance_commands_embed() -> discord.Embed:
    embed = discord.Embed(title="💬 Commands - Finance (`/finance ...`)", color=COLOR)
    embed.add_field(name="setprice <cost>", value="Set a pallet's total cost. Purchase Management or Finance Management.", inline=False)
    embed.add_field(name="record-sale", value="Record (or correct) an item's actual sale price + platform.", inline=False)
    embed.add_field(name="refund / expense", value="Log a refund against a sale, or an expense against a pallet.", inline=False)
    embed.add_field(name="reverse-sale", value="Undo an item's recorded sale (duplicate entry, fell through).", inline=False)
    embed.add_field(name="set-shipping-info", value="Capture a non-eBay buyer's recipient/address for Pirate Ship.", inline=False)
    embed.add_field(name="history", value="List a pallet's recent refunds/expenses/reversals.", inline=False)
    embed.add_field(name="override-count / clear-count-override", value="Manually correct the \"items received\" figure, or revert to automatic.", inline=False)
    embed.add_field(name="summary", value="Post a fresh copy of a pallet's financial/status card.", inline=False)
    embed.add_field(name="refresh-card", value="Force-refresh the pinned card in place right now, without waiting for an item to move.", inline=False)
    embed.add_field(name="pallet-summary [pallet_name]", value="Full cost breakdown by type (purchase/credit card/shipping/etc), revenue, and margin for a pallet.", inline=False)
    embed.add_field(name="overview", value="Business-wide snapshot: QuickBooks balance, month-to-date spend/revenue, pallets in progress vs sold out.", inline=False)
    embed.add_field(name="connect-quickbooks", value="Admin: link QuickBooks Online for automatic credit-card charge tracking. See #credit-card-charges.", inline=False)
    embed.add_field(name="import-pirateship <csv>", value="Admin: import a Pirate Ship shipping export and allocate its costs to pallets/items.", inline=False)
    return embed


def _shipping_commands_embed() -> discord.Embed:
    embed = discord.Embed(title="💬 Commands - Shipping (`/pirate-ship ...`)", color=COLOR)
    embed.add_field(
        name="export-batch",
        value="Export every sold non-eBay item that hasn't shipped yet, as a CSV for Pirate Ship's batch import.",
        inline=False,
    )
    embed.add_field(
        name="purge-buyer-data [days] [confirm]",
        value="Preview or clear old buyer name/address data past the retention window.",
        inline=False,
    )
    embed.set_footer(text="eBay sales never need this - Pirate Ship pulls those directly via its own eBay integration.")
    return embed


def _pallet_item_commands_embed() -> discord.Embed:
    embed = discord.Embed(title="💬 Commands - Pallets & Items", color=COLOR)
    embed.add_field(name="/pallet list [include_archived]", value="List all pallets with item counts and status.", inline=False)
    embed.add_field(name="/pallet archive", value="Close out a finished pallet: deletes its Discord channels, keeps all data.", inline=False)
    embed.add_field(name="/item delete <item_number>", value="Delete a single item by its number (admin only).", inline=False)
    embed.add_field(
        name="/item duplicate <item_number> <count>",
        value="Split an item still in Queue Review into N identical, independently-tracked items - for when Data Entry's \"3x ...\" shorthand wasn't used, or the count changes later.",
        inline=False,
    )
    embed.add_field(
        name="/item hold <item_number> reason:<choice> [note]",
        value="Move an item into #hold with a fixed reason (Queue Review or Listing Management role - not admin-only). Tap Resolved on its card to send it back where it came from.",
        inline=False,
    )
    return embed


def _admin_setup_commands_embed() -> discord.Embed:
    embed = discord.Embed(title="💬 Commands - Admin & Setup", color=COLOR)
    embed.add_field(name="/admin backup-now / backups", value="Create or list local backups of the database/photos/CSVs.", inline=False)
    embed.add_field(name="/admin bind-role / unbind-role / role-bindings", value="Bind a bot role to a specific Discord role ID, so a rename doesn't break permission checks.", inline=False)
    embed.add_field(name="/admin purge-old-photos [days] [confirm]", value="Preview/delete R2 photo copies for items sold past the retention window (local copies untouched).", inline=False)
    embed.add_field(name="/admin db-wipe", value="⚠️ Irreversible - permanently erases all pallet/item data, and R2 photos too if configured.", inline=False)
    embed.add_field(name="/setup shared-channels / hub / info-channel", value="One-time server setup commands (this channel included).", inline=False)
    embed.set_footer(text="All /admin and /pallet, /setup commands (and /item delete, /item duplicate) require the Pallet Admin role - /item hold is the one exception, see Pallets & Items.")
    return embed
