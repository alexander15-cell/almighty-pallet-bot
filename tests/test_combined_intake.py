"""Synthetic new-intake/shop integration; never uses Discord or a live DB."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

import config
from combined_intake import IntakeAdapter, IntakeAdapterError, MIN_NEW_ITEM_ID
from shop_approval import Actor, ShopReviewStore
from shop_content import ShopContentError
from shop_discord import post_review

GUILD = "100000000000000001"
SHOP = "100000000000000002"
APP = "100000000000000003"
USER = "100000000000000004"
CHANNELS = {"website_shop": SHOP, "data-entry": "100000000000000005",
            "listed": "100000000000000006", "sold": "100000000000000007"}
URL = "https://www.ebay.com/itm/123456789012"


class Channel:
    def __init__(self):
        self.id = int(SHOP)
        self.guild = SimpleNamespace(id=int(GUILD), me=SimpleNamespace(id=int(APP), bot=True))
        self.sent = []
        self.fail = False

    async def send(self, **kwargs):
        if self.fail:
            raise RuntimeError("synthetic send failure")
        result = SimpleNamespace(id=600000000000000000 + len(self.sent))
        self.sent.append((result, kwargs))
        return result


@pytest.fixture
def case(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COMBINED_MODE", True, raising=False)
    monkeypatch.setattr(config, "PRESERVE_DISCORD_HISTORY", True)
    photo_root = tmp_path / "photos"
    monkeypatch.setattr(config, "PHOTO_DIR", str(photo_root))
    pallet = fresh_db.create_pallet("Synthetic new inventory", category_id=111, created_by=int(USER))
    fresh_db.map_channel(pallet, "data-entry", int(CHANNELS["data-entry"]))
    for name in ("listed", "sold"):
        fresh_db.set_shared_channel(name, int(CHANNELS[name]))
    legacy = fresh_db.create_item(pallet, "Old record must not be adopted", [], int(USER))
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='items'", (MIN_NEW_ITEM_ID - 1,))
    item_id = fresh_db.create_item(pallet, "Approved description of actual item", [], int(USER))
    assert item_id == MIN_NEW_ITEM_ID
    photo = photo_root / str(item_id) / "photo.png"
    photo.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "navy").save(photo)
    with fresh_db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls=?,ai_flags=?,recipient_name=? WHERE id=?",
                     (json.dumps([str(photo)]), "PRIVATE STAFF FLAGS", "PRIVATE BUYER", item_id))
    fresh_db.save_ebay_listing_data(item_id, "Human approved title", "123", "3000", 25.99, {})
    fresh_db.update_status(item_id, "listed", new_message_id=500000000000000001)
    store = ShopReviewStore(tmp_path / "shop.sqlite", guild_id=GUILD, shop_channel_id=SHOP, operator_ids=[USER])
    channel = Channel()
    bot = SimpleNamespace(get_channel=lambda channel_id: channel if channel_id == int(SHOP) else None, add_view=Mock())
    settings = SimpleNamespace(application_id=APP, guild_id=GUILD, channels=CHANNELS,
        item_id_floor=MIN_NEW_ITEM_ID, sku_prefix="FGNEW-", photo_directory=photo_root,
        state_directory=tmp_path, operator_ids=[USER])
    adapter = IntakeAdapter(bot, settings, store)
    value = SimpleNamespace(adapter=adapter, settings=settings, db=fresh_db, item_id=item_id,
        reference=f"intake-{item_id}", pallet=pallet, legacy=legacy, store=store, bot=bot,
        channel=channel, photo=photo, actor=Actor(GUILD, SHOP, USER))
    yield value
    value.adapter.close()
    store.close()


def run(coro):
    return asyncio.run(coro)


def approve(case):
    run(case.adapter.reconcile())
    review = case.store.system_get_review(str(case.item_id))
    ready = case.store.edit(case.actor, review.item_id, expected_revision=review.revision,
        request_id=f"manual-details-{review.revision}", price_cents=2599, ebay_url=URL)
    return case.store.approve(case.actor, ready.item_id, expected_revision=ready.revision,
        request_id=f"manual-approve-{ready.revision}", validated_content_digest=ready.content_digest)


def test_new_listed_item_is_draft_not_published_and_never_changes_stage_card(case):
    before = case.db.get_item(case.item_id)["current_message_id"]
    assert run(case.adapter.reconcile()) == {"created": 1, "refreshed": 0, "invalidated": 0, "posted": 1, "blocked": 0}
    review = case.store.system_get_review(str(case.item_id))
    assert review.state == "draft" and review.published_revision is None
    assert review.quantity == 1 and review.price_cents is None and review.ebay_url is None
    assert review.sku == f"FGNEW-PALLET-{case.pallet}-ITEM-2"
    assert case.db.get_item(case.item_id)["current_message_id"] == before
    assert case.store.claim_next("fixture-worker") is None
    assert case.store.system_get_review(str(case.legacy)) is None


def test_content_is_allowlisted_normalized_and_one_physical_sale_unit(case):
    content = run(case.adapter.resolve_content(case.reference))
    assert content["quantity"] == 1
    assert content["condition"] == "used"
    assert content["photos"][0]["mimeType"] == "image/jpeg"
    assert "PRIVATE" not in json.dumps(content)
    assert "functional testing has not" in content["condition_notes"]
    assert set(content) == {"product_ref", "title", "description", "condition", "condition_notes", "quantity", "photos", "source_state"}


def test_legacy_or_unbound_reference_never_adopted(case):
    for reference in ("intake-1", "legacy-1", str(case.photo), "https://example.test/photo"):
        with pytest.raises(IntakeAdapterError):
            run(case.adapter.resolve_content(reference))
    with pytest.raises(IntakeAdapterError):
        run(case.adapter.lifecycle(1))


def test_restart_restores_all_owned_views_without_reposting_or_touching_intake(case):
    run(case.adapter.reconcile())
    case.adapter.close()
    case.adapter = IntakeAdapter(case.bot, case.settings, case.store)
    run(case.adapter.restore_views())
    assert run(case.adapter.reconcile())["posted"] == 0
    assert len(case.channel.sent) == 1
    case.bot.add_view.assert_called_once()
    assert case.bot.add_view.call_args.kwargs["message_id"] == case.channel.sent[0][0].id
    assert case.db.get_item(case.item_id)["current_message_id"] == 500000000000000001


def test_failed_append_keeps_draft_and_retries_without_recreating_identity(case):
    case.channel.fail = True
    assert run(case.adapter.reconcile())["blocked"] == 1
    first = case.store.system_get_review(str(case.item_id))
    assert first.state == "draft"
    case.channel.fail = False
    result = run(case.adapter.reconcile())
    assert result["created"] == 0 and result["posted"] == 1
    assert case.store.system_get_review(str(case.item_id)).revision == first.revision


def test_changed_content_invalidates_approval_and_requires_new_review(case):
    job = approve(case)
    with case.db.get_conn() as conn:
        conn.execute("UPDATE items SET raw_description='Newly corrected description' WHERE id=?", (case.item_id,))
    result = run(case.adapter.reconcile())
    review = case.store.system_get_review(str(case.item_id))
    assert result["refreshed"] == 1
    assert review.state == "draft" and review.revision > job.revision
    assert case.store.delivery_job(job.job_id).state == "cancelled"
    assert case.store.claim_next("fixture-worker") is None


@pytest.mark.parametrize("status,target", [("sold", "sold"), ("shipped", "sold"), ("on_hold", "held"), ("deleted", "withdrawn")])
def test_source_lifecycle_invalidates_pending_and_does_not_require_photos(case, status, target):
    job = approve(case)
    case.db.update_status(case.item_id, status)
    with case.db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls='[]' WHERE id=?", (case.item_id,))
    assert run(case.adapter.lifecycle(case.item_id)) == target
    assert run(case.adapter.reconcile())["invalidated"] == 1
    assert case.store.system_get_review(str(case.item_id)).state == target
    assert case.store.delivery_job(job.job_id).state == "cancelled"


def test_hold_release_needs_fresh_human_approval(case):
    approve(case)
    case.db.update_status(case.item_id, "on_hold")
    run(case.adapter.reconcile())
    held = case.store.system_get_review(str(case.item_id))
    case.db.update_status(case.item_id, "listed")
    run(case.adapter.reconcile())
    ready = case.store.system_get_review(str(case.item_id))
    assert ready.state == "draft" and ready.revision > held.revision
    assert case.store.claim_next("fixture-worker") is None


def test_terminal_sold_item_never_revived_by_stale_listed_source(case):
    approve(case)
    case.db.update_status(case.item_id, "sold")
    run(case.adapter.reconcile())
    sold = case.store.system_get_review(str(case.item_id))
    case.db.update_status(case.item_id, "listed")
    run(case.adapter.reconcile())
    assert case.store.system_get_review(str(case.item_id)) == sold


@pytest.mark.parametrize("condition", ["1750", "7000", "unknown"])
def test_known_bad_or_unknown_condition_is_blocked_not_reclassified(case, condition):
    with case.db.get_conn() as conn:
        conn.execute("UPDATE ebay_listing_data SET condition_id=? WHERE item_id=?", (condition, case.item_id))
    with pytest.raises(ShopContentError, match="unsupported_condition"):
        run(case.adapter.resolve_content(case.reference))
    assert run(case.adapter.reconcile())["blocked"] == 1
    assert case.store.system_get_review(str(case.item_id)) is None


def test_invalid_photos_hold_existing_approval_then_recover_as_draft(case):
    approve(case)
    with case.db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls='[]' WHERE id=?", (case.item_id,))
    assert run(case.adapter.reconcile())["invalidated"] == 1
    assert case.store.system_get_review(str(case.item_id)).state == "held"
    with case.db.get_conn() as conn:
        conn.execute("UPDATE items SET photo_urls=? WHERE id=?", (json.dumps([str(case.photo)]), case.item_id))
    run(case.adapter.reconcile())
    assert case.store.system_get_review(str(case.item_id)).state == "draft"


def test_photo_from_another_item_or_outside_photo_root_is_rejected(case, tmp_path):
    external = tmp_path / "outside.png"
    Image.new("RGB", (2, 2), "red").save(external)
    for path in (external, Path("relative.png")):
        with case.db.get_conn() as conn:
            conn.execute("UPDATE items SET photo_urls=? WHERE id=?", (json.dumps([str(path)]), case.item_id))
        with pytest.raises(IntakeAdapterError, match="invalid_intake_photos"):
            run(case.adapter.resolve_content(case.reference))


def test_mapping_change_blocks_and_cancels_existing_approval(case):
    approve(case)
    case.db.map_channel(case.pallet, "data-entry", 999)
    assert run(case.adapter.reconcile())["invalidated"] == 1
    assert case.store.system_get_review(str(case.item_id)).state == "held"


def test_owned_shop_posts_from_human_ui_do_not_get_duplicated(case):
    async def flow():
        await case.adapter.reconcile()
        review = case.store.system_get_review(str(case.item_id))
        newer = case.store.edit(case.actor, review.item_id, expected_revision=review.revision,
            request_id="manual-change", price_cents=1000, ebay_url=URL)
        result = await post_review(case.channel, store=case.store, review=newer, title="Human approved title",
            resolver=case.adapter.resolve_content, application_id=APP)
        # The optional UI hook performs this receipt; duplicate recording is safe.
        case.adapter.record_review_message(newer, "Human approved title", result.id)
        count = len(case.channel.sent)
        assert (await case.adapter.reconcile())["posted"] == 0
        assert len(case.channel.sent) == count
    run(flow())


def test_system_reconciliation_audit_does_not_impersonate_operator(case):
    run(case.adapter.reconcile())
    rows = case.store.connection.execute("SELECT actor_id FROM shop_audit").fetchall()
    assert rows and {row[0] for row in rows} == {"system:intake"}


def test_source_id_collision_never_changes_an_unowned_review(case):
    foreign = case.store.system_create_draft(item_id=str(case.item_id), sku="OLD-REVIEWED-SKU",
        product_ref="legacy-reviewed-card", content_digest="a" * 64, quantity=3, request_id="legacy-fixture")
    assert run(case.adapter.reconcile())["blocked"] == 1
    assert case.store.system_get_review(str(case.item_id)) == foreign
    assert case.channel.sent == []


def test_combined_ordinary_listed_and_hold_cards_cannot_bypass_shop(case):
    import cogs.item_flow as flow
    from website_contract import FOOTER_PREFIX
    run(flow.send_item_card(case.channel, case.db.get_item(case.item_id)))
    assert str(case.item_id) in case.channel.sent[-1][1]["embeds"][0].footer.text
    run(flow.send_website_hold_notice(case.channel, case.db.get_item(case.item_id)))
    for _, sent in case.channel.sent:
        embeds = sent.get("embeds") or [sent["embed"]]
        assert all(FOOTER_PREFIX not in json.dumps(embed.to_dict()) for embed in embeds)
