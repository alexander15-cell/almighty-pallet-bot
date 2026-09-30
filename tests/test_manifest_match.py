"""
Queue Review's manifest-matching step (cogs/item_flow.py) - once a pallet
has an attached manifest (see manifest_import.py, database.upsert_manifest_lot),
Approve first offers an AI-suggested best-guess match against the manifest's
still-unmatched lines (suggest_manifest_matches/_manifest_match_prompt),
with a human confirm/choose-different/no-match step
(ManifestMatchView/ManifestMatchChooseDifferentSelect) before continuing on
to the existing eBay condition select - same "AI suggests, human confirms"
shape as EbayCategoryPickView.
"""
import asyncio
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import cogs.item_flow as item_flow_module
from cogs.item_flow import (
    EbayConditionSelectView, ItemFlow, ManifestMatchChooseDifferentSelect, ManifestMatchView,
    _manifest_match_prompt, suggest_manifest_matches, send_item_card,
)


class _FakeRole:
    pass


class _FakeUser:
    def __init__(self, role, user_id=42):
        self.id = user_id
        self.roles = [role]


class _FakeMessage:
    def __init__(self, msg_id=999):
        self.id = msg_id


class _FakeResponse:
    def __init__(self):
        self.content = None
        self.view = None
        self.edited = []

    async def send_message(self, content=None, **kwargs):
        self.content = content
        self.view = kwargs.get("view")

    async def edit_message(self, content=None, **kwargs):
        self.edited.append((content, kwargs.get("view")))
        self.content = content
        self.view = kwargs.get("view")

    async def defer(self, ephemeral=True, thinking=True):
        pass


class _FakeInteraction:
    def __init__(self, role=None, message=None):
        self.response = _FakeResponse()
        self.message = message or _FakeMessage()
        self.user = _FakeUser(role)
        self.guild = object()
        self.client = None


@pytest.fixture(autouse=True)
def bypass_role_check(monkeypatch):
    role = _FakeRole()
    monkeypatch.setattr(item_flow_module.runtime_settings, "resolve_role", lambda guild, name: role)
    return role


@pytest.fixture
def pallet_with_manifest(fresh_db):
    pallet_id = fresh_db.create_pallet("Manifest Match Pallet", category_id=1, created_by=1)
    charge_id = fresh_db.create_manual_invoice_charge(100.0, submitted_by=1)
    lot_id = fresh_db.upsert_manifest_lot(charge_id, 100.0, 200.0, "m.csv", 1, [
        {"sku": "A1", "product": "DeWalt Cordless Drill", "retail_price": 150.0},
        {"sku": "B2", "product": "Extension Cord 25ft", "retail_price": 50.0},
    ])
    fresh_db.set_manifest_lot_pallet(lot_id, pallet_id)
    return pallet_id


@pytest.fixture
def queue_review_item(fresh_db, pallet_with_manifest):
    item_id = fresh_db.create_item(pallet_with_manifest, "cordless drill", [], 1)
    fresh_db.save_ai_review(item_id, title="Cordless Drill", description="A drill.", flags="")
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=999)
    return fresh_db.get_item(item_id)


def test_suggest_manifest_matches_ranks_by_keyword_overlap(fresh_db, pallet_with_manifest):
    item = {"ai_title": "Cordless Drill", "ai_description": "", "raw_description": ""}
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    ranked = suggest_manifest_matches(item, lines)
    assert ranked[0]["product"] == "DeWalt Cordless Drill"


def test_suggest_manifest_matches_sku_mention_outweighs_keywords(fresh_db, pallet_with_manifest):
    item = {"ai_title": "Mystery box", "ai_description": "", "raw_description": "found SKU B2 on the box"}
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    ranked = suggest_manifest_matches(item, lines)
    assert ranked[0]["sku"] == "B2"


def test_manifest_match_prompt_none_when_pallet_has_no_manifest(fresh_db):
    pallet_id = fresh_db.create_pallet("No Manifest Pallet", category_id=2, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    item = fresh_db.get_item(item_id)
    assert _manifest_match_prompt(item) is None


def test_manifest_match_prompt_none_when_already_matched(fresh_db, pallet_with_manifest, queue_review_item):
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    fresh_db.match_manifest_line(lines[0]["id"], queue_review_item["id"])
    item = fresh_db.get_item(queue_review_item["id"])
    assert _manifest_match_prompt(item) is None


def test_manifest_match_prompt_none_when_flagged_unmatched(fresh_db, queue_review_item):
    fresh_db.mark_item_manifest_unmatched(queue_review_item["id"])
    item = fresh_db.get_item(queue_review_item["id"])
    assert _manifest_match_prompt(item) is None


def test_manifest_match_prompt_none_when_every_line_already_matched(fresh_db, pallet_with_manifest, queue_review_item):
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    other_item = fresh_db.create_item(pallet_with_manifest, "other", [], 1)
    for line in lines:
        fresh_db.match_manifest_line(line["id"], other_item)
    item = fresh_db.get_item(queue_review_item["id"])
    assert _manifest_match_prompt(item) is None


def test_manifest_match_prompt_labels_process_of_elimination(fresh_db, pallet_with_manifest, queue_review_item):
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    other_item = fresh_db.create_item(pallet_with_manifest, "other", [], 1)
    fresh_db.match_manifest_line(lines[1]["id"], other_item)  # leave exactly one line
    item = fresh_db.get_item(queue_review_item["id"])
    content, view = _manifest_match_prompt(item)
    assert "process of elimination" in content
    assert isinstance(view, ManifestMatchView)


def test_prompt_ebay_condition_shows_manifest_match_first(fresh_db, queue_review_item, bypass_role_check):
    cog = ItemFlow.__new__(ItemFlow)
    interaction = _FakeInteraction(role=bypass_role_check)

    asyncio.run(cog.prompt_ebay_condition(interaction, queue_review_item["id"]))

    assert isinstance(interaction.response.view, ManifestMatchView)
    assert "manifest match" in interaction.response.content.lower()


def test_prompt_ebay_condition_skips_straight_to_condition_when_no_manifest(fresh_db, bypass_role_check):
    pallet_id = fresh_db.create_pallet("No Manifest Pallet 2", category_id=3, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "widget", [], 1)
    fresh_db.update_status(item_id, fresh_db.STATUS_QUEUE_REVIEW, actor_id=1, new_message_id=999)
    cog = ItemFlow.__new__(ItemFlow)
    interaction = _FakeInteraction(role=bypass_role_check)

    asyncio.run(cog.prompt_ebay_condition(interaction, item_id))

    assert isinstance(interaction.response.view, EbayConditionSelectView)


def test_confirm_button_matches_line_and_continues_to_condition_select(fresh_db, pallet_with_manifest, queue_review_item):
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    item = fresh_db.get_item(queue_review_item["id"])
    view = ManifestMatchView(item["id"], lines[0], lines)
    interaction = _FakeInteraction()

    asyncio.run(view.confirm.callback(interaction))

    updated = fresh_db.get_item(item["id"])
    assert updated["manifest_line_id"] == lines[0]["id"]
    assert isinstance(interaction.response.view, EbayConditionSelectView)


def test_no_match_button_flags_item_and_continues(fresh_db, pallet_with_manifest, queue_review_item):
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    item = fresh_db.get_item(queue_review_item["id"])
    view = ManifestMatchView(item["id"], lines[0], lines)
    interaction = _FakeInteraction()

    asyncio.run(view.no_match.callback(interaction))

    updated = fresh_db.get_item(item["id"])
    assert updated["manifest_unmatched"] == 1
    assert updated["manifest_line_id"] is None
    assert isinstance(interaction.response.view, EbayConditionSelectView)


def test_choose_different_select_matches_the_picked_line(fresh_db, pallet_with_manifest, queue_review_item):
    lines = fresh_db.get_unmatched_manifest_lines_for_pallet(pallet_with_manifest)
    item = fresh_db.get_item(queue_review_item["id"])
    select = ManifestMatchChooseDifferentSelect(item["id"], lines)
    select._values = [str(lines[1]["id"])]
    interaction = _FakeInteraction()

    asyncio.run(select.callback(interaction))

    updated = fresh_db.get_item(item["id"])
    assert updated["manifest_line_id"] == lines[1]["id"]


def test_item_card_shows_persistent_no_match_flag(fresh_db, queue_review_item):
    fresh_db.mark_item_manifest_unmatched(queue_review_item["id"])
    item = fresh_db.get_item(queue_review_item["id"])

    class _FakeChannel:
        async def send(self, *args, **kwargs):
            self.embeds = kwargs.get("embeds")
            return self

    channel = _FakeChannel()
    message = asyncio.run(send_item_card(channel, item))
    field_names = [f.name for f in message.embeds[0].fields]
    assert "⚠️ No manifest match" in field_names
