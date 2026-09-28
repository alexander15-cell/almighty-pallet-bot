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
        "Awaiting Listing card buttons (#awaiting-listing)",
        "Add to eBay Batch, Add to FB Marketplace Batch, List on eBay (API), or Mark Listed (Other).",
        ("list item", "list on ebay", "list on facebook", "listing", "awaiting listing"),
    ),
    HelpEntry(
        "/ebay export-batch",
        "Download the accumulated eBay CSV batch and start a fresh one.",
        ("ebay batch", "ebay csv", "export ebay"),
    ),
    HelpEntry(
        "/ebay fill-recommendations",
        "Upload eBay's returned recommendations file - fills in price/quantity/condition/format from Queue Review data.",
        ("ebay recommendations", "fill recommendations"),
    ),
    HelpEntry(
        "/ebay category-search <query>",
        "Look up a real eBay category ID by keyword.",
        ("ebay category", "category id"),
    ),
    HelpEntry(
        "/ebay retry-item <item_number> ...",
        "Fix and re-queue an item whose batch upload needs a correction.",
        ("retry item", "fix ebay upload", "requeue item"),
    ),
    HelpEntry(
        "/ebay requeue-pending",
        "Bulk retry-item: re-queue every item pending an eBay upload into a fresh batch, no corrections needed.",
        ("requeue all", "bulk retry", "pending ebay upload"),
    ),
    HelpEntry(
        "/ebay batches / batch <id>",
        "List recent eBay batches, or show one batch's still-pending items.",
        ("ebay batches", "batch status"),
    ),
    HelpEntry(
        "/ebay import-results <batch_id> <csv>",
        "Reconcile a batch against a results CSV downloaded from Seller Hub.",
        ("ebay results", "reconcile", "seller hub"),
    ),
    HelpEntry(
        "/ebay confirm-listed [item_number]",
        "Manually confirm an item is live on eBay (or tap Confirm Listed on its card in #pending-ebay-upload).",
        ("confirm listed ebay", "ebay live"),
    ),
    HelpEntry(
        "/fb-marketplace export-batch",
        "Download the accumulated FB Marketplace CSV batch and start a fresh one.",
        ("facebook batch", "fb marketplace csv", "facebook csv"),
    ),
    HelpEntry(
        "/fb-marketplace confirm-listed [item_number]",
        "Manually confirm an item is live on FB Marketplace.",
        ("confirm listed facebook", "facebook live"),
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
        "Record (or correct) an item's actual sale price + platform.",
        ("record sale", "sale price", "how much did it sell"),
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
        "/finance reverse-sale",
        "Undo an item's recorded sale (duplicate entry, fell through).",
        ("undo sale", "reverse sale", "fix sale"),
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
        "/finance refresh-card",
        "Force-refresh the pinned finance card in place right now, without waiting for an item to move.",
        ("refresh card", "update card"),
    ),
    HelpEntry(
        "/finance pallet-summary [pallet_name]",
        "Full cost breakdown by type (purchase/credit card/shipping/etc), revenue, and margin for a pallet.",
        ("cost breakdown", "margin", "profit"),
    ),
    HelpEntry(
        "/finance overview",
        "Business-wide snapshot: QuickBooks balance, month-to-date spend/revenue, pallet counts.",
        ("business overview", "quickbooks balance", "month to date"),
    ),
    HelpEntry(
        "/finance connect-quickbooks",
        "Admin: link QuickBooks Online for automatic credit-card charge tracking.",
        ("connect quickbooks", "link quickbooks", "quickbooks setup"),
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
        "Unclaimed invoices/credit-card charges for a pallet that hasn't been created yet wait here - Start New Pallet offers to attach any to the new pallet.",
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
