"""
Data + search for the /help command (cogs/help.py) - lets someone unsure
which command they need type a few plain words ("submit invoice", "delete
a pallet", "list on ebay") and get back the matching command(s) or button/
channel, instead of already having to know the right slash command name or
dig through #information.

Kept as its own module, static data rather than logic, so it's easy to keep
in sync with real commands as features change - same approach as
information_content.py, which remains the full written guide this
complements rather than replaces.
"""
import re
from dataclasses import dataclass
from typing import Tuple

_TOKEN_RE = re.compile(r"[\w'-]+")
_STOPWORDS = {"a", "an", "the", "to", "for", "of", "on", "in", "is", "it", "my", "do", "does", "i", "how"}


@dataclass(frozen=True)
class HelpEntry:
    usage: str
    description: str
    keywords: Tuple[str, ...]


ENTRIES = (
    HelpEntry(
        "Start New Pallet button (#new-pallet-tracking)",
        "Begin tracking a new pallet - creates its category, channels, and cost card.",
        ("new pallet", "start pallet", "create pallet", "add pallet", "begin pallet", "track pallet"),
    ),
    HelpEntry(
        "Post in #data-entry",
        'Post a photo + short note in that pallet\'s #data-entry channel, one message per item. Start the note with "3x " to log several identical items at once.',
        ("add item", "new item", "post item", "log item", "intake", "data entry"),
    ),
    HelpEntry(
        "/item duplicate <item_number> <count>",
        "Split an item still in Queue Review into N identical, independently-tracked items.",
        ("duplicate item", "split item", "multiple of the same item", "clone item"),
    ),
    HelpEntry(
        "/item hold <item_number> reason:<choice> [note]",
        "Move an item into #hold with a reason. Tap Resolved on its card to send it back where it came from.",
        ("hold item", "pause item", "item on hold"),
    ),
    HelpEntry(
        "/item delete <item_number>",
        "Delete a single item by its number (admin only).",
        ("delete item", "remove item", "erase item"),
    ),
    HelpEntry(
        "Queue Review card buttons (#queue-review)",
        "Approve, Edit, Re-review (AI), or Reject an item waiting for review.",
        ("approve item", "reject item", "review item", "edit item", "re-review"),
    ),
    HelpEntry(
        "Automatic AI retry",
        "An item whose AI review failed (backend unreachable/timed out) gets retried automatically every AI_RETRY_POLL_MINUTES, up to AI_RETRY_BATCH_SIZE at a time, once the backend's reachable again - no need to click Re-review (AI) by hand.",
        ("ai retry", "ai backlog", "ollama offline", "ai review failed", "backend down"),
    ),
    HelpEntry(
        "Awaiting Listing card buttons (#awaiting-listing)",
        "List on eBay (live through the API, straight to #listed), Edit Details (fix title/price/item specifics first), or Mark Listed (Other).",
        ("list item", "list on ebay", "listing", "awaiting listing", "edit details", "fix listing", "change price"),
    ),
    HelpEntry(
        "/ebay category-search <query>",
        "Look up a real eBay category ID by keyword.",
        ("ebay category", "category id"),
    ),
    HelpEntry(
        "/ebay category-aspects <category_id>",
        "Show the item specifics eBay requires or recommends for a category.",
        ("item specifics", "aspects", "required specifics", "category aspects", "brand type color"),
    ),
    HelpEntry(
        "/ebay sync-finances [days_back]",
        "Pull eBay's real fees, shipping, refunds and payouts into the bot (read-only - changes no recorded numbers).",
        ("ebay fees", "sync fees", "ebay payout", "final value fee", "sync finances"),
    ),
    HelpEntry(
        "/ebay net-report [pallet_name]",
        "Compare recorded eBay revenue with eBay's real net payout after fees, labels and refunds.",
        ("net profit", "ebay net", "profit after fees", "real profit", "net report", "fees report"),
    ),
    HelpEntry(
        "💸 eBay fees cards / /ebay fee-cards since:<date>",
        "Book eBay's fees, labels and ad fees to QuickBooks and the pallet's cost - Confirm & log fees in #ebay-orders.",
        ("log ebay fees", "ebay fee expense", "final value fee", "shipping label cost", "fee cards", "undeposited funds"),
    ),
    HelpEntry(
        "Mark as Sold / Mark as Shipped buttons",
        "Mark as Sold in #listed is a single click, no price prompt - Finance Management records the actual price separately. Mark as Shipped in #sold checks it off.",
        ("mark sold", "mark shipped", "sold item", "shipped item"),
    ),
    HelpEntry(
        "/pirate-ship export-batch",
        "Export every sold non-eBay item that hasn't shipped yet, as a CSV for Pirate Ship's batch import.",
        ("shipping csv", "pirate ship", "export shipping"),
    ),
    HelpEntry(
        "/pirate-ship purge-buyer-data [days] [confirm]",
        "Preview or clear old buyer name/address data past the retention window.",
        ("delete buyer data", "purge buyer", "buyer privacy"),
    ),
    HelpEntry(
        "/finance setprice <cost>",
        "Set a pallet's total cost. Run inside that pallet's category.",
        ("pallet cost", "set price", "purchase price", "total cost"),
    ),
    HelpEntry(
        "/finance record-sale",
        "Manually correct an item's sale price/platform - /finance log-sale sets these normally.",
        ("record sale", "sale price", "how much did it sell", "correct sale"),
    ),
    HelpEntry(
        "/finance log-sale <items> <total_price>",
        "Log a sale (single item or cross-pallet bundle) and book a Sales Receipt to QuickBooks - cash basis, so there's no separate COGS journal entry (the pallet's cost was already expensed at purchase).",
        ("log sale", "book sale", "cogs", "cost of goods sold", "sales receipt", "sales tax"),
    ),
    HelpEntry(
        "/finance retry-sale <sale_id>",
        "Retry pushing a saved sale to QuickBooks after an earlier API failure.",
        ("retry sale", "quickbooks failed", "sale failed"),
    ),
    HelpEntry(
        "Mark as Sold platform picker",
        "Choosing where an item sold (eBay/In Person/Other) right when it's marked sold - needed for sales tax, and posts it to #accounting for Finance Management to log with /finance log-sale.",
        ("mark sold platform", "where did it sell", "sales tax platform"),
    ),
    HelpEntry(
        "#accounting",
        "Sold items land here as a to-do list for Finance Management - eBay sales via Confirm & log sale in #ebay-orders, others via /finance log-sale. Each turns ✅ once logged.",
        ("accounting channel", "log sale"),
    ),
    HelpEntry(
        "#finance-audit-log",
        "Read-only, visible to everyone: a permanent record of every completed QuickBooks transaction this bot has posted.",
        ("audit log", "transaction history", "what was logged", "finance history channel"),
    ),
    HelpEntry(
        "#finance-dashboard",
        "Read-only, visible to everyone: a pinned message with the bank and Undeposited Funds balances, this year's sales/costs/net income from QuickBooks, and pallet progress - refreshes after every sale/expense and every 20 minutes (admins: /finance refresh-dashboard).",
        ("dashboard", "business position", "account balances", "our position", "financial summary", "bank balance", "net income"),
    ),
    HelpEntry(
        "Categorize credit-card expense",
        "Only if a credit card account is set up (QUICKBOOKS_CREDIT_CARD_ACCOUNT_ID - there isn't one now): after allocating a card charge to a pallet, an optional second dropdown picks its QuickBooks expense/COGS account.",
        ("categorize expense", "expense account", "assign account", "credit card category"),
    ),
    HelpEntry(
        "/finance refund",
        "Log a refund against an item's sale.",
        ("refund",),
    ),
    HelpEntry(
        "/finance expense",
        "Log a cost against a pallet (packaging, fees, etc).",
        ("expense", "packaging cost", "fees"),
    ),
    HelpEntry(
        "/finance sales [pallet_name] [platform]",
        "List logged sales - number, date, platform, amount, items, and whether each reached QuickBooks or was reversed.",
        ("list sales", "logged sales", "check sale", "was it logged", "yard sale", "sales history", "find sale"),
    ),
    HelpEntry(
        "/ebay payouts / payout-card <payout_id>",
        "eBay payouts and whether each is recorded in QuickBooks. A 🏦 payout card (posted automatically once a payout reaches the bank) records the Bank Deposit into the bank account when everything in it is logged - then Match it in QuickBooks Banking.",
        ("payout", "deposit", "bank deposit", "undeposited funds", "match bank feed", "ebay paid us"),
    ),
    HelpEntry(
        "/ebay refund-cards / account-charges [month]",
        "↩️ eBay refund cards book refunds/returns (lower the sale, credit Undeposited Funds); 🧾 account-charges cards book eBay charges not tied to an order (store subscription, listing fees) once a month. Both post automatically; these commands post them now.",
        ("refund", "return", "store subscription", "listing fee", "insertion fee", "account charges"),
    ),
    HelpEntry(
        "/ebay order-card <order_id>",
        "Post the Confirm & log sale card for one eBay order whose card never appeared (refuses if already logged).",
        ("missing card", "order card", "no confirm button", "log ebay sale", "card didn't post"),
    ),
    HelpEntry(
        "/finance reverse-sale",
        "Undo an item's recorded sale (duplicate entry, fell through). For a sale already in QuickBooks: void its Sales Receipt there, then run with quickbooks_voided:True.",
        ("undo sale", "reverse sale", "fix sale", "void sale", "cancelled sale"),
    ),
    HelpEntry(
        "/partners summary / put-in / paid-expense / reimburse / payout",
        "Partner ledger: money each partner put in, expenses they paid personally, reimbursements, profit payouts, and everyone's equal share. A record only - never moves money.",
        ("partner", "partners", "split profit", "payout", "reimburse", "reimbursement", "who owes", "contribution", "put money in", "equity"),
    ),
    HelpEntry(
        "/ebay status",
        "Check every eBay permission and QuickBooks setting the bot needs - shows what works and what to fix. Read-only.",
        ("status", "health check", "is it working", "ebay permissions", "scopes", "check setup"),
    ),
    HelpEntry(
        "/finance set-shipping-info",
        "Capture a non-eBay buyer's recipient/address for Pirate Ship.",
        ("buyer address", "shipping info", "recipient"),
    ),
    HelpEntry(
        "/finance history",
        "List a pallet's recent refunds/expenses/reversals.",
        ("finance history", "transaction history"),
    ),
    HelpEntry(
        "/finance override-count / clear-count-override",
        'Manually set the pallet\'s "items received" count, or revert to the automatic (live) count.',
        ("items received", "override count"),
    ),
    HelpEntry(
        "/finance summary",
        "Post a fresh copy of a pallet's financial/status card.",
        ("finance summary", "pallet financials", "cost card"),
    ),
    HelpEntry(
        "/finance refresh-dashboard",
        "Admin: update the pinned #finance-dashboard right now (re-posts and pins it if it was deleted).",
        ("refresh dashboard", "update dashboard", "dashboard out of date", "repin dashboard"),
    ),
    HelpEntry(
        "/finance refresh-card",
        "Force-refresh the pinned finance card in place right now, without waiting for an item to move.",
        ("refresh card", "update card"),
    ),
    HelpEntry(
        "/finance pallet-profit [pallet_name] / /finance pallets",
        "True profit for a pallet - purchase cost, what eBay kept, labels, refunds, return on cost, sell-through, days to sell, and what's left to sell. /finance pallets ranks every pallet side by side.",
        ("true profit", "roi", "return on cost", "sell through", "which pallets make money", "best pallet"),
    ),
    HelpEntry(
        "/finance refund <item> <amount> <reason> [refunded_from]",
        "Refund a non-eBay sale - also posts a QuickBooks Journal Entry (from the bank, or undeposited cash). eBay refunds use the ↩️ eBay refund card instead.",
        ("refund", "give money back", "yard sale refund"),
    ),
    HelpEntry(
        "/finance pallet-summary [pallet_name]",
        "Full cost breakdown by type (purchase/invoice/shipping/eBay fees/etc), revenue, and margin for a pallet.",
        ("cost breakdown", "margin", "profit"),
    ),
    HelpEntry(
        "/finance overview",
        "Business-wide snapshot: bank and Undeposited Funds balances (plus a card if set), month-to-date spend/revenue, pallet counts.",
        ("business overview", "quickbooks balance", "bank balance", "month to date"),
    ),
    HelpEntry(
        "/finance connect-quickbooks",
        "Admin: link QuickBooks Online - sales receipts, eBay fee entries, pallet purchase expenses and balances.",
        ("connect quickbooks", "link quickbooks", "quickbooks setup"),
    ),
    HelpEntry(
        "/finance list-accounts",
        "Admin: list QuickBooks accounts and their IDs, for the QUICKBOOKS_* settings in .env (e.g. QUICKBOOKS_BANK_ACCOUNT_ID). Use account_type:All account types for income/expense accounts.",
        ("quickbooks account id", "find account id", "list quickbooks accounts", "bank account id", "expense account id", "credit card account id"),
    ),
    HelpEntry(
        "/finance import-pirateship <csv>",
        "Admin: import a Pirate Ship shipping export and allocate its costs to pallets/items.",
        ("import pirate ship", "allocate shipping cost"),
    ),
    HelpEntry(
        "Post in #submit-invoices",
        'One message per invoice: attach the invoice (photo or PDF) and type just the dollar amount (e.g. "125.50") in the same message, then send.',
        ("submit invoice", "log invoice", "purchase invoice", "add invoice"),
    ),
    HelpEntry(
        "Confirm logged in QuickBooks button (#awaiting-pallet-charges)",
        "On a submitted invoice's card - click once you've entered it into QuickBooks by hand to remove the card. It can still be attached to a pallet later either way.",
        ("confirm invoice", "dismiss invoice card", "quickbooks logged"),
    ),
    HelpEntry(
        "#awaiting-pallet-charges",
        "Unclaimed invoices for a pallet that hasn't been created yet wait here - Start New Pallet offers to attach any to the new pallet.",
        ("unclaimed charge", "attach charge", "awaiting charge"),
    ),
    HelpEntry(
        "/pallet list [include_archived]",
        "List all pallets with item counts and status.",
        ("list pallets", "all pallets", "pallet status"),
    ),
    HelpEntry(
        "/pallet archive",
        "Close out a finished pallet: deletes its Discord channels, keeps all data.",
        ("archive pallet", "close pallet", "finish pallet"),
    ),
    HelpEntry(
        "/pallet delete <pallet_name> [confirm]",
        "Irreversible - erases a pallet and everything tied to it (items, history, cost/sale records), freeing its name for reuse.",
        ("delete pallet", "erase pallet", "reuse pallet name", "remove pallet"),
    ),
    HelpEntry(
        "/website status",
        "Check website publishing mode and delivery counts.",
        ("website status", "storefront status"),
    ),
    HelpEntry(
        "/website review",
        "Force-check for new items to review right now.",
        ("website review", "recheck website"),
    ),
    HelpEntry(
        "/website hold <item_id>",
        "Put a website-listed item on hold and queue its removal from the site.",
        ("website hold", "remove from website"),
    ),
    HelpEntry(
        "#website_shop",
        "Approval queue for items eligible to publish to the separate storefront website - enter price + eBay link, then Confirm, or send it back for changes.",
        ("website shop", "approve website item", "storefront"),
    ),
    HelpEntry(
        "/admin backup-now / backups",
        "Create or list local backups of the database, photos, and CSV archives.",
        ("backup", "restore"),
    ),
    HelpEntry(
        "/admin bind-role / unbind-role / role-bindings",
        "Bind a bot role to a specific Discord role ID, so a future rename doesn't break it.",
        ("bind role", "role binding", "rename role"),
    ),
    HelpEntry(
        "/admin purge-old-photos [days] [confirm]",
        "Preview/delete R2 photo copies for items sold past the retention window (local copies untouched).",
        ("purge photos", "delete photos", "photo retention"),
    ),
    HelpEntry(
        "/admin db-wipe",
        "Irreversible - permanently erases all pallet/item data, and R2 photos too if configured.",
        ("wipe database", "reset everything", "delete all data"),
    ),
    HelpEntry(
        "/setup shared-channels / hub / info-channel",
        "One-time server setup commands.",
        ("setup", "initial setup", "server setup"),
    ),
    HelpEntry(
        "/setup rebind-channel <stage> <channel>",
        "Point a shared channel at a different Discord channel - for after accidentally deleting and recreating one.",
        ("rebind channel", "deleted channel", "recreate channel", "fix channel"),
    ),
    HelpEntry(
        "/setup refresh-channel-info",
        "Admin: update every shared channel's topic and pinned ℹ️ guide to the bot's current text (they're otherwise only written when a channel is first created).",
        ("channel info", "pinned message", "channel topic", "update pinned", "outdated pin"),
    ),
)


def search(query: str, limit: int = 8):
    """Ranks ENTRIES by which query words are *present* in their usage/
    description/keywords (each field counted once per word, not per
    occurrence, so a word repeated across an entry's own keyword phrases
    - e.g. "pallet" - doesn't drown out a more specific match), weighted
    keywords > usage > description, plus a bonus when a keyword contains
    the whole query verbatim. Returns [] for a blank query, best matches
    first."""
    q = " ".join(query.strip().lower().split())
    if not q:
        return []
    terms = [t for t in q.split() if t not in _STOPWORDS] or q.split()
    scored = []
    for entry in ENTRIES:
        keyword_words = set(" ".join(entry.keywords).split())
        usage_words = set(_TOKEN_RE.findall(entry.usage.lower()))
        description_words = set(_TOKEN_RE.findall(entry.description.lower()))
        score = 0
        for term in terms:
            if term in keyword_words:
                score += 4
            if term in usage_words:
                score += 3
            if term in description_words:
                score += 1
        if any(q == keyword or q in keyword for keyword in entry.keywords):
            score += 6
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _, entry in scored[:limit]]
