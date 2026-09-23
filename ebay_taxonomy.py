"""
eBay's own official category taxonomy (ebay_categories.json - generated
from "New Category Structure from June 1st, 2026 - US", eBay's published
category tree export), used as a keyword-searchable ground truth for
picking eBay categories - both Automated Review's AI suggestion and Queue
Review's manual "Search categories" step (see EbayCategorySearchModal in
item_flow.py) resolve against this file instead of a hand-typed list.

This replaces config.py's old EBAY_CATEGORIES dict, which had to be
maintained by hand (~30 entries) with no way to verify an ID was real or a
genuine leaf category without eBay API access - it had already caused
several real upload failures this way (non-leaf IDs, and once even two
categories accidentally sharing the same ID, which crashed the entire
approval flow outright since Discord rejects a select menu with a
duplicate option value). Every ID in ebay_categories.json comes directly
from eBay's own published taxonomy, so there's no more hand-entry error
possible, and every one of its ~18,000 entries is a real, listable LEAF
category - there's no separate "confirmed working" tracking needed for
correctness anymore (database.record_ebay_category_confirmed's usage
counts still drive the Queue Review quick-pick sort order, just no longer
for "is this ID even real").

18,000 categories is far past Discord's 25-option select cap and far too
much to hand a vision model as a fixed enum in every review call, so
neither the AI suggestion nor the manual picker choose from a fixed list
any more - search() below ranks the full taxonomy by keyword relevance
against a plain-text query (the AI's own short product-type guess, or
whatever a reviewer types into the search modal) and returns the
best-matching leaf categories.

To regenerate this data file from a fresh eBay category export (a CSV with
L1..L6 columns for each nesting level, and a Cat ID column - eBay publishes
these periodically as its own taxonomy changes), see the parsing logic this
data file was built with: read the CSV, track the current L1..L6 path as
you walk the rows top to bottom (each row fills in its own level, deeper
levels reset), and a row is a real listable LEAF only if no later row is
nested one level deeper under it - eBay's own top-level and intermediate
grouping nodes (like "Home & Garden" or "Tools & Workshop Equipment") are
real category IDs too, but aren't leaves and can't actually be listed
against.
"""
import json
import re
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent / "ebay_categories.json"


def _tokenize(text: str) -> set:
    """
    Lowercased word tokens, with a crude trailing "s"/"es" strip so simple
    plural/singular mismatches (a query for "wrench" against a category
    named "Wrenches") still match - not real stemming, just enough for
    short product-type phrases without adding an NLP dependency.
    """
    tokens = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) > 4 and word.endswith("es"):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        tokens.add(word)
    return tokens


def _load_index() -> list:
    with DATA_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    index = []
    for category_id, full_path in raw:
        leaf_name = full_path.rsplit(" > ", 1)[-1]
        leaf_tokens = _tokenize(leaf_name)
        ancestor_tokens = _tokenize(full_path) - leaf_tokens
        index.append((category_id, full_path, leaf_tokens, ancestor_tokens))
    return index


_INDEX = _load_index()
_BY_ID = {category_id: full_path for category_id, full_path, _, _ in _INDEX}


def get_path(category_id: str) -> str | None:
    """Full "L1 > L2 > ... > leaf" breadcrumb for a category ID, or None if
    it's not in the current taxonomy (a stale ID from before a taxonomy
    refresh, or a manually-entered one via /ebay retry-item)."""
    return _BY_ID.get(category_id)


def search(query: str, limit: int = 10) -> list:
    """
    Ranks every leaf category by relevance to `query` (a short plain-text
    product-type description, not required to match eBay's own naming) and
    returns up to `limit` (category_id, full_path) pairs, best match first.
    A token matching the category's own leaf name (not just an ancestor in
    its breadcrumb) counts for more, so a query like "wrench" ranks an
    actual "Wrenches" category ahead of "Tools & Workshop Equipment" (an
    ancestor-only match). Returns [] if `query` has no usable words or
    nothing scores above zero.
    """
    query_tokens = _tokenize(query or "")
    if not query_tokens:
        return []
    scored = []
    for category_id, full_path, leaf_tokens, ancestor_tokens in _INDEX:
        leaf_matches = query_tokens & leaf_tokens
        ancestor_matches = (query_tokens & ancestor_tokens) - leaf_matches
        # A small penalty per leaf-name word the query DIDN'T account for -
        # otherwise a loosely-related leaf with extra unrelated words (e.g.
        # "Drill & Tap Centers" matching a query for "drill") ties a tightly
        # matching one (e.g. "Cordless Drills") on raw match count alone,
        # and an arbitrary tie-break (like alphabetical) decides instead of
        # which one is actually the closer match.
        precision_penalty = 0.1 * len(leaf_tokens - leaf_matches)
        score = 3 * len(leaf_matches) + len(ancestor_matches) - precision_penalty
        if score > 0:
            scored.append((score, full_path, category_id))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(category_id, full_path) for _, full_path, category_id in scored[:limit]]
