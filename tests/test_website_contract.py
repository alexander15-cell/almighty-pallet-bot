import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from website_contract import build_website_contract, FOOTER_PREFIX


def test_local_intake_does_not_subscribe_to_direct_messages():
    from combined_runtime import GuardedBot
    settings = SimpleNamespace(application_id="100000000000000001", guild_id="100000000000000002",
        operator_ids=("100000000000000003",), channels={"data-entry": "100000000000000004"},
        item_id_floor=1000000000)
    async def check():
        bot = GuardedBot(settings)
        try:
            assert bot.intents.guild_messages
            assert bot.intents.message_content
            assert not bot.intents.dm_messages
            assert not bot.intents.dm_reactions
            assert not bot.intents.dm_typing
            assert not bot.intents.members
            assert not bot.intents.presences
            assert not bot.combined_guard.ready
        finally:
            await bot.close()
    asyncio.run(check())


def example():
    return ({"id": 12, "pallet_id": 3, "item_number": 2, "status": "awaiting_listing",
             "ai_title": "Unapproved AI title", "ai_description": "Approved description.",
             "raw_description": "Earlier raw description", "photo_urls": "[]",
             "ai_flags": "INTERNAL FLAG", "buyer_address": "PRIVATE ADDRESS"},
            {"ebay_title": "Approved title", "price": 19.99, "condition_id": "3000",
             "listing_format": "FixedPrice", "set_by": "PRIVATE STAFF"})


def fields(embed):
    data = embed if isinstance(embed, dict) else embed.to_dict()
    return {field["name"]: field["value"] for field in data["fields"]}


def test_contract_approved_public_fields_explicit_destination_and_no_mutation():
    item, listing = example()
    originals = deepcopy((item, listing))
    contract = build_website_contract(item, listing, status="listed", photo_count=2)
    assert contract["title"] == "Approved title"
    assert contract["description"] == "Approved description."
    assert contract["footer"]["text"] == FOOTER_PREFIX
    assert fields(contract) == {
        "Website item ID": "12", "Website SKU": "PALLET-3-ITEM-2", "Website status": "listed",
        "Website hold": "false", "Website issue": "none", "Website price cents": "1999",
        "Website condition": "used", "Website condition notes": "Used",
    }
    assert "PRIVATE" not in json.dumps(contract)
    assert "INTERNAL" not in json.dumps(contract)
    assert "Unapproved" not in json.dumps(contract)
    assert (item, listing) == originals


@pytest.mark.parametrize("amount,cents", [(19.99, "1999"), ("0.29", "29"), (Decimal("1.01"), "101"),
                                         ("21474836.47", "2147483647"), (1, "100")])
def test_exact_price_cents(amount, cents):
    item, listing = example()
    listing["price"] = amount
    assert fields(build_website_contract(item, listing, status="listed", photo_count=1))["Website price cents"] == cents


@pytest.mark.parametrize("amount", [None, True, False, "NaN", "Infinity", "-Infinity", "", "free", 0, -1,
                                   "1.001", "21474836.48", 0.1 + 0.2,
                                   "1.000000000000000000000000000001", "1e-999999999"])
def test_invalid_price_never_rounded_or_guessed(amount):
    item, listing = example()
    listing["price"] = amount
    exported = fields(build_website_contract(item, listing, status="listed", photo_count=1))
    assert exported["Website issue"] == "missing_price"
    assert "Website price cents" not in exported


@pytest.mark.parametrize("change,issue", [({"ebay_title": None}, "missing_approved_text"),
    ({"ebay_title": "x" * 81}, "missing_approved_text"), ({"condition_id": "???"}, "unsupported_condition"),
    ({"listing_format": "Auction"}, "auction_listing"), ({"listing_format": None}, "missing_price")])
def test_missing_or_invalid_listing_blocks(change, issue):
    item, listing = example()
    listing.update(change)
    exported = fields(build_website_contract(item, listing, status="listed", photo_count=1))
    assert exported["Website issue"] == issue
    assert "Website price cents" not in exported


@pytest.mark.parametrize("description", [None, "", " " * 4, "x" * 4001])
def test_missing_or_overlong_description_does_not_publish(description):
    item, listing = example()
    item["ai_description"] = item["raw_description"] = description
    assert fields(build_website_contract(item, listing, status="listed", photo_count=1))["Website issue"] == "missing_approved_text"


@pytest.mark.parametrize("count,error,issue", [(0, False, "missing_photos"), (1, True, "photo_error"),
                                              (11, False, "photo_error"), ("1", False, "photo_error")])
def test_photo_issues_block(count, error, issue):
    item, listing = example()
    assert fields(build_website_contract(item, listing, status="listed", photo_count=count, photo_error=error))["Website issue"] == issue


@pytest.mark.parametrize("status", ["sold", "shipped"])
def test_withdrawal_does_not_require_listing_or_photos(status):
    item, _ = example()
    item["status"] = "listed"
    contract = fields(build_website_contract(item, None, status=status, photo_count=0))
    assert contract["Website status"] == status
    assert contract["Website issue"] == "none"
    assert "Website price cents" not in contract


def test_hold_is_explicit_and_never_has_publish_fields():
    item, listing = example()
    item["on_hold"] = 1
    contract = fields(build_website_contract(item, listing, status="listed", photo_count=1))
    assert contract["Website hold"] == "true"
    assert "Website price cents" not in contract


@pytest.mark.parametrize("key,value", [("id", 0), ("id", True), ("pallet_id", "1x"),
                                       ("item_number", -1), ("on_hold", "false")])
def test_invalid_identity_or_hold_rejected(key, value):
    item, listing = example()
    item[key] = value
    with pytest.raises(ValueError):
        build_website_contract(item, listing, status="listed", photo_count=1)


@pytest.mark.parametrize("condition,expected", [("1000", "new"), ("1500", "open_box"),
                                              ("3000", "used")])
def test_condition_is_conservative(condition, expected):
    item, listing = example()
    listing["condition_id"] = condition
    assert fields(build_website_contract(item, listing, status="listed", photo_count=1))["Website condition"] == expected


def test_send_card_unique_attachments_and_max_embed_size(tmp_path, monkeypatch):
    import cogs.item_flow as flow
    item, listing = example()
    paths = []
    for index in range(10):
        folder = tmp_path / str(index)
        folder.mkdir()
        photo = folder / "duplicate.jpg"
        photo.write_bytes(b"offline-fixture")
        paths.append(str(photo))
    item.update(photo_urls=json.dumps(paths), ai_description="D" * 4000, ai_flags="F" * 4000)
    listing["ebay_title"] = "T" * 80
    monkeypatch.setattr(flow.db, "get_pallet", lambda _: {"name": "P" * 1000})
    monkeypatch.setattr(flow.db, "get_ebay_listing_data", lambda _: listing)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
    asyncio.run(flow.send_item_card(channel, item, destination_status="listed", extra_text="E" * 1000))
    sent = channel.send.call_args.kwargs
    assert len(sent["files"]) == len(sent["embeds"]) == 10
    assert len({file.filename for file in sent["files"]}) == 10
    assert sum(len(embed) for embed in sent["embeds"]) <= 6000
    assert fields(sent["embeds"][0])["Website status"] == "listed"
    assert fields(sent["embeds"][0])["Website issue"] == "none"


def test_send_card_missing_file_blocks_without_interrupting_intake(monkeypatch):
    import cogs.item_flow as flow
    item, listing = example()
    item["photo_urls"] = '["no-such-file.jpg"]'
    monkeypatch.setattr(flow.db, "get_pallet", lambda _: {"name": "Pallet"})
    monkeypatch.setattr(flow.db, "get_ebay_listing_data", lambda _: listing)
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
    asyncio.run(flow.send_item_card(channel, item, destination_status="listed"))
    assert fields(channel.send.call_args.kwargs["embeds"][0])["Website issue"] == "photo_error"


def test_mark_shipped_updates_contract_and_removes_buttons(monkeypatch):
    import discord
    import cogs.item_flow as flow
    item, listing = example()
    item["status"] = "sold"
    embed = discord.Embed.from_dict(build_website_contract(item, listing, status="sold", photo_count=0))
    embed.set_image(url="attachment://photo-01.jpg")
    message = SimpleNamespace(embeds=[embed], edit=AsyncMock())
    interaction = SimpleNamespace(message=message, user=SimpleNamespace(id=123),
                                  response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setattr(flow.db, "get_item", lambda _: item)
    monkeypatch.setattr(flow.db, "get_ebay_listing_data", lambda _: listing)
    monkeypatch.setattr(flow.db, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(flow.finance_utils, "refresh_finance_message", AsyncMock())
    cog = flow.ItemFlow(SimpleNamespace())
    monkeypatch.setattr(cog, "_require_role", AsyncMock(return_value=True))
    asyncio.run(cog.mark_shipped(interaction, 12))
    updated = message.edit.call_args.kwargs
    assert updated["view"] is None
    assert fields(updated["embeds"][0])["Website status"] == "shipped"
    assert updated["embeds"][0].image.url == "attachment://photo-01.jpg"
    assert updated["embeds"][0].footer.text.startswith(FOOTER_PREFIX)
    assert item["status"] == "sold"  # source dictionary was not mutated


@pytest.mark.parametrize("method,start_status,destination", [("move_to_listed", "awaiting_listing", "listed"),
                                                           ("move_to_sold", "listed", "sold")])
def test_transition_passes_destination_before_source_update(monkeypatch, method, start_status, destination):
    import cogs.item_flow as flow
    item, _ = example()
    item["status"] = start_status
    channel = SimpleNamespace(id=321)
    interaction = SimpleNamespace(message=SimpleNamespace(delete=AsyncMock()), user=SimpleNamespace(id=123),
                                  response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    monkeypatch.setattr(flow.db, "get_item", lambda _: item)
    monkeypatch.setattr(flow.db, "resolve_channel_id", lambda *args: 321)
    monkeypatch.setattr(flow.db, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(flow.finance_utils, "refresh_finance_message", AsyncMock())
    send_card = AsyncMock(return_value=SimpleNamespace(id=456))
    monkeypatch.setattr(flow, "send_item_card", send_card)
    cog = flow.ItemFlow(SimpleNamespace(get_channel=lambda _: channel))
    monkeypatch.setattr(cog, "_require_role", AsyncMock(return_value=True))
    asyncio.run(getattr(cog, method)(interaction, 12))
    assert send_card.call_args.kwargs["destination_status"] == destination
    assert send_card.call_args.args[1]["status"] == start_status
