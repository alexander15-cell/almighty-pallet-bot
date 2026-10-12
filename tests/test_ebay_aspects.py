"""
eBay item specifics ("aspects") - ebay_aspects.py, ai_review.fill_item_aspects,
and their wiring into Automated Review (run_ai_review), Queue Review's
Approve form (EbayListingModal), the post-Approve missing-specifics warning,
and /ebay category-aspects.

Response shapes follow eBay's published Taxonomy API spec for
getItemAspectsForCategory. aiohttp and the Anthropic client are faked - no
real network calls.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest

import ai_review
import config
import ebay_api
import ebay_aspects
import cogs.item_flow as item_flow_module
from cogs.item_flow import EbayListingModal, ItemFlow


def _raw_aspect(name, required=False, usage="RECOMMENDED", mode="FREE_TEXT", values=(), max_length=None,
                multi=False):
    constraint = {"aspectRequired": required, "aspectUsage": usage, "aspectMode": mode,
                  "itemToAspectCardinality": "MULTI" if multi else "SINGLE"}
    if max_length:
        constraint["aspectMaxLength"] = max_length
    return {"localizedAspectName": name, "aspectConstraint": constraint,
            "aspectValues": [{"localizedValue": v} for v in values]}


EBAY_BODY = {"aspects": [
    _raw_aspect("Color", usage="RECOMMENDED", mode="SELECTION_ONLY", values=["Black", "White", "Brass"]),
    _raw_aspect("Brand", required=True),
    _raw_aspect("Number of Lights", usage="OPTIONAL"),
    _raw_aspect("Type", required=True, mode="SELECTION_ONLY", values=["Vanity Light", "Wall Sconce"]),
    _raw_aspect("Style", usage="RECOMMENDED", max_length=10),
    _raw_aspect("Room", usage="RECOMMENDED", multi=True),
]}
ASPECTS = ebay_aspects.parse_aspects(EBAY_BODY)


# ------------------------------------------------------------------ fakes

class _FakeResponse:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responder, calls):
        self._responder, self._calls = responder, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, auth=None, headers=None, json=None):
        return _FakeResponse(200, {"access_token": "app-tok", "expires_in": 7200})

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append(url)
        return _FakeResponse(*self._responder(url))


@pytest.fixture
def ebay_on(monkeypatch):
    ebay_api._cached_app_token = None
    monkeypatch.setattr(config, "EBAY_ENABLED", True)
    monkeypatch.setattr(config, "EBAY_APP_ID", "a")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "c")
    monkeypatch.setattr(config, "EBAY_ENVIRONMENT", "production")
    calls = []

    def install(responder):
        monkeypatch.setattr(ebay_api.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(responder, calls))
        return calls
    yield install
    ebay_api._cached_app_token = None


def _fake_anthropic(monkeypatch, reply_text, seen=None):
    def create(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply_text)])
    monkeypatch.setattr(ai_review, "_client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    monkeypatch.setattr(config, "AI_REVIEW_BACKEND", "anthropic")


# ---------------------------------------------------------------- parsing

def test_parse_aspects_orders_required_then_recommended_then_optional():
    assert [a["name"] for a in ASPECTS] == ["Brand", "Type", "Color", "Style", "Room", "Number of Lights"]
    type_aspect = next(a for a in ASPECTS if a["name"] == "Type")
    assert type_aspect == {"name": "Type", "required": True, "usage": "RECOMMENDED", "mode": "SELECTION_ONLY",
                           "multi": False, "max_length": None, "values": ["Vanity Light", "Wall Sconce"]}
    assert next(a for a in ASPECTS if a["name"] == "Room")["multi"] is True


def test_relevant_aspects_drops_optional_ones_and_caps():
    assert [a["name"] for a in ebay_aspects.relevant_aspects(ASPECTS)] == ["Brand", "Type", "Color", "Style", "Room"]
    assert len(ebay_aspects.relevant_aspects(ASPECTS, limit=2)) == 2


# ------------------------------------------------------- fetching + cache

def test_get_category_aspects_calls_ebay_once_then_uses_the_cache(fresh_db, ebay_on):
    calls = ebay_on(lambda url: (200, EBAY_BODY))

    first = asyncio.run(ebay_aspects.get_category_aspects("117503"))
    second = asyncio.run(ebay_aspects.get_category_aspects("117503"))

    assert first == second == ASPECTS
    assert calls == ["https://api.ebay.com/commerce/taxonomy/v1/category_tree/0/"
                     "get_item_aspects_for_category?category_id=117503"]


def test_stale_cache_is_refetched(fresh_db, ebay_on):
    calls = ebay_on(lambda url: (200, EBAY_BODY))
    asyncio.run(ebay_aspects.get_category_aspects("117503"))
    later = datetime.now(timezone.utc) + ebay_aspects.CACHE_MAX_AGE + timedelta(days=1)

    asyncio.run(ebay_aspects.get_category_aspects("117503", now=later))

    assert len(calls) == 2


def test_ebay_failure_falls_back_to_stale_cache_or_none(fresh_db, ebay_on):
    ebay_on(lambda url: (200, EBAY_BODY))
    asyncio.run(ebay_aspects.get_category_aspects("117503"))
    ebay_on(lambda url: (500, {"errors": [{"message": "down"}]}))
    later = datetime.now(timezone.utc) + ebay_aspects.CACHE_MAX_AGE + timedelta(days=1)

    assert asyncio.run(ebay_aspects.get_category_aspects("117503", now=later)) == ASPECTS
    assert asyncio.run(ebay_aspects.get_category_aspects("999")) is None


def test_get_category_aspects_is_off_without_the_ebay_api(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    assert asyncio.run(ebay_aspects.get_category_aspects("117503")) is None


# -------------------------------------------------------------- cleaning

def test_clean_suggestions_enforces_ebays_rules():
    raw = {
        "brand": "Kichler",                 # case-insensitive name -> eBay's spelling
        "Type": "vanity light",             # pick-list: matched to eBay's exact value
        "Color": "Chartreuse",              # pick-list: not an allowed value -> dropped
        "Style": "Transitional Modern",     # trimmed to max_length 10
        "Room": ["Bathroom", "Hallway"],    # multi-value list joined
        "MPN": "Unknown",                   # not in this category's list -> dropped
        "Number of Lights": "N/A",          # placeholder -> dropped
        "Made Up Aspect": "x",              # not an eBay aspect -> dropped
    }

    assert ebay_aspects.clean_suggestions(raw, ASPECTS) == {
        "Brand": "Kichler", "Type": "Vanity Light", "Style": "Transition", "Room": "Bathroom, Hallway",
    }


def test_clean_suggestions_handles_garbage():
    assert ebay_aspects.clean_suggestions(None, ASPECTS) == {}
    assert ebay_aspects.clean_suggestions({"Brand": {"nested": 1}, "Type": True, "Style": None}, ASPECTS) == {}


def test_missing_required_is_case_insensitive_and_ignores_blank_values():
    assert ebay_aspects.missing_required(ASPECTS, {"brand": "X"}) == ["Type"]
    assert ebay_aspects.missing_required(ASPECTS, {"Brand": "  ", "TYPE": "Wall Sconce"}) == ["Brand"]
    assert ebay_aspects.missing_required(None, {}) == []


def test_format_specifics_never_exceeds_discords_text_box_limit():
    many = {f"Aspect {i}": "x" * 40 for i in range(50)}
    text = ebay_aspects.format_specifics(many)
    assert len(text) <= ebay_aspects.SPECIFICS_TEXT_LIMIT
    assert all(": " in line for line in text.splitlines())  # whole lines only


# --------------------------------------------------------- AI fill call

def test_fill_item_aspects_sends_aspects_and_parses_json(monkeypatch, tmp_path):
    seen = []
    _fake_anthropic(monkeypatch, '```json\n{"Brand": "Kichler"}\n```', seen)
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"jpegbytes")

    result = asyncio.run(ai_review.fill_item_aspects([str(photo)] * 4, "Vanity light", "desc", "note",
                                                     ebay_aspects.relevant_aspects(ASPECTS)))

    assert result == {"Brand": "Kichler"}
    blocks = seen[0]["messages"][0]["content"]
    assert sum(1 for b in blocks if b["type"] == "image") == 2  # capped at two photos
    prompt = blocks[-1]["text"]
    assert "- Type (required) - must be one of: Vanity Light; Wall Sconce" in prompt
    assert "Number of Lights" not in prompt  # optional aspects aren't sent


@pytest.mark.parametrize("reply", ["not json", "[1, 2]"])
def test_fill_item_aspects_returns_empty_on_bad_output(monkeypatch, reply):
    _fake_anthropic(monkeypatch, reply)
    assert asyncio.run(ai_review.fill_item_aspects([], "t", "d", "n", ASPECTS)) == {}


def test_fill_item_aspects_is_skipped_on_ollama(monkeypatch):
    _fake_anthropic(monkeypatch, '{"Brand": "X"}')
    monkeypatch.setattr(config, "AI_REVIEW_BACKEND", "ollama")
    assert asyncio.run(ai_review.fill_item_aspects([], "t", "d", "n", ASPECTS)) == {}


# ------------------------------------------------------ prefill pipeline

def _prefill_env(monkeypatch, ebay_on, ai_reply='{"Brand": "Kichler", "Type": "Wall Sconce"}'):
    monkeypatch.setattr(config, "AI_ENABLED", True)
    ebay_on(lambda url: (200, EBAY_BODY))

    async def fake_search(query, limit=10):
        return [("117503", "Home & Garden > Lamps > Wall Fixtures")]
    monkeypatch.setattr(ebay_api, "search_categories", fake_search)
    _fake_anthropic(monkeypatch, ai_reply)


def test_prefill_saves_a_cleaned_suggestion_for_every_item(fresh_db, monkeypatch, ebay_on):
    _prefill_env(monkeypatch, ebay_on, ai_reply='{"Brand": "Kichler", "Type": "Wall Sconce", "Color": "Teal"}')
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    ids = [fresh_db.create_item(pallet_id, "2x sconce", [], 1) for _ in range(2)]

    result = asyncio.run(ebay_aspects.prefill_for_items(ids, "wall sconce", [], "Sconce", "desc", "note"))

    assert result == {"category_id": "117503", "specifics": {"Brand": "Kichler", "Type": "Wall Sconce"}}
    for item_id in ids:
        assert fresh_db.get_item_aspect_suggestion(item_id) == result


def test_prefill_does_nothing_without_ebay_ai_or_a_category(fresh_db, monkeypatch, ebay_on):
    _prefill_env(monkeypatch, ebay_on)
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)

    assert asyncio.run(ebay_aspects.prefill_for_items([item_id], None, [], "t", "d", "n")) is None
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    assert asyncio.run(ebay_aspects.prefill_for_items([item_id], "lamp", [], "t", "d", "n")) is None
    assert fresh_db.get_item_aspect_suggestion(item_id) is None


def test_prefill_never_raises(fresh_db, monkeypatch, ebay_on):
    _prefill_env(monkeypatch, ebay_on)

    async def boom(query, limit=10):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(ebay_api, "search_categories", boom)
    assert asyncio.run(ebay_aspects.prefill_for_items([1], "lamp", [], "t", "d", "n")) is None


def test_suggestion_never_attaches_to_a_different_item_reusing_the_id(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)
    fresh_db.save_item_aspect_suggestion(item_id, "117503", {"Brand": "Old"})
    with fresh_db.get_conn() as conn:  # simulate a db-wipe re-issuing this ID to a new item
        conn.execute("UPDATE items SET created_at = ? WHERE id = ?", ("2099-01-01T00:00:00+00:00", item_id))
    assert fresh_db.get_item_aspect_suggestion(item_id) is None


# --------------------------------------------- wiring into the pipeline

class _FakeMessage:
    def __init__(self, msg_id=1):
        self.id = msg_id

    async def delete(self):
        pass


class _FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return _FakeMessage(len(self.sent) + 1000)


class _FakeBot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel


def test_run_ai_review_prefills_after_posting_the_queue_review_card(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "sconce", [], 1)
    channel = _FakeChannel()
    order = []

    async def fake_review(photo_paths, note):
        return {"suggested_title": "Wall Sconce", "suggested_description": "d", "suggested_category": "wall sconce"}

    async def fake_prefill(item_ids, category, photos, title, description, note):
        order.append(("prefill", len(channel.sent), item_ids, category, title))
    monkeypatch.setattr(ai_review, "review_item", fake_review)
    monkeypatch.setattr(item_flow_module.ebay_aspects, "prefill_for_items", fake_prefill)

    asyncio.run(ItemFlow(_FakeBot(channel)).run_ai_review([item_id], _FakeMessage()))

    assert order == [("prefill", 1, [item_id], "wall sconce", "Wall Sconce")]  # card already posted
    assert fresh_db.get_item(item_id)["status"] == fresh_db.STATUS_QUEUE_REVIEW


def test_run_ai_review_skips_prefill_when_the_ai_review_itself_failed(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setattr(item_flow_module, "PHOTO_DIR", tmp_path)
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)
    called = []

    async def fake_review(photo_paths, note):
        return {"suggested_title": "", "ai_backend_failed": True}

    async def fake_prefill(*args):
        called.append(args)
    monkeypatch.setattr(ai_review, "review_item", fake_review)
    monkeypatch.setattr(item_flow_module.ebay_aspects, "prefill_for_items", fake_prefill)

    asyncio.run(ItemFlow(_FakeBot(_FakeChannel())).run_ai_review([item_id], _FakeMessage()))
    assert called == []


def _modal(item_id, category_id):
    async def build():
        return EbayListingModal(item_id, "1000", category_id, "FixedPrice", None)
    return asyncio.run(build())


def test_approve_form_prefills_suggested_specifics_for_the_same_category(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)
    fresh_db.save_item_aspect_suggestion(item_id, "117503", {"Brand": "Kichler", "Type": "Wall Sconce"})

    same = _modal(item_id, "117503")
    other = _modal(item_id, "99999")

    assert same.item_specifics.default == "Brand: Kichler\nType: Wall Sconce"
    assert "AI, confirm" in same.item_specifics.label and len(same.item_specifics.label) <= 45
    assert other.item_specifics.default == "" and "AI" not in other.item_specifics.label


def test_approve_form_prefers_already_saved_specifics(fresh_db):
    pallet_id = fresh_db.create_pallet("P", category_id=1, created_by=1)
    item_id = fresh_db.create_item(pallet_id, "x", [], 1)
    fresh_db.save_item_aspect_suggestion(item_id, "117503", {"Brand": "AI Guess"})
    fresh_db.save_ebay_listing_data(item_id, "T", "117503", "1000", 5.0, {"Brand": "Human"})

    assert _modal(item_id, "117503").item_specifics.default == "Brand: Human"


def test_missing_specifics_note_names_required_blanks_except_auto_filled_ones(fresh_db, monkeypatch, ebay_on):
    body = {"aspects": EBAY_BODY["aspects"] + [_raw_aspect("Wattage", required=True)]}
    ebay_on(lambda url: (200, body))
    cog = ItemFlow(_FakeBot(_FakeChannel()))

    note = asyncio.run(cog._missing_specifics_note("117503", {"Color": "Black"}, 7))
    assert "**Wattage**" in note and "Brand" not in note and "Type" not in note
    assert "/ebay retry-item 7" in note
    assert asyncio.run(cog._missing_specifics_note("117503", {"Wattage": "60"}, 7)) == ""


def test_missing_specifics_note_is_silent_when_ebay_is_unreachable(fresh_db, monkeypatch):
    monkeypatch.setattr(config, "EBAY_ENABLED", False)
    cog = ItemFlow(_FakeBot(_FakeChannel()))
    assert asyncio.run(cog._missing_specifics_note("117503", {}, 1)) == ""


def test_category_aspects_text_fits_a_discord_message():
    import cogs.ebay as ebay_cog
    text = ebay_cog.build_category_aspects_text("117503", ASPECTS)
    assert "**Required:**\n• **Brand**\n• **Type** - pick one: Vanity Light, Wall Sconce" in text
    assert "**Recommended:**" in text and "Number of Lights" not in text

    huge = [dict(ASPECTS[0], name=f"Aspect {i}") for i in range(300)]
    assert len(ebay_cog.build_category_aspects_text("1", huge)) <= 1900
