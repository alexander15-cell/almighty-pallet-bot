import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shop_approval import Actor, ShopReviewStore
from shop_content import verify_content
from shop_discord import ShopDetailsModal, ShopReviewView, post_review, review_embed
from test_shop_content import fixture_content

GUILD, CHANNEL, APP, USER = '100000000000000001', '100000000000000002', '100000000000000003', '100000000000000004'
URL = 'https://www.ebay.com/itm/123456789012'

class Response:
    def __init__(self):
        self.done = False
        self.messages = []
        self.modal = None
    def is_done(self):
        return self.done
    async def defer(self, **kwargs):
        self.done = True
    async def send_message(self, message, **kwargs):
        self.messages.append(message)
        self.done = True
    async def send_modal(self, modal):
        self.modal = modal
        self.done = True

def interaction(number=1):
    author = SimpleNamespace(id=int(APP), bot=True)
    channel = SimpleNamespace(id=int(CHANNEL), guild=SimpleNamespace(id=int(GUILD), me=author), send=AsyncMock())
    return SimpleNamespace(id=number, application_id=int(APP), guild_id=int(GUILD), channel_id=int(CHANNEL),
        user=SimpleNamespace(id=int(USER), bot=False), message=SimpleNamespace(author=author),
        channel=channel, response=Response(), followup=SimpleNamespace(send=AsyncMock()))

@pytest.fixture
def setup(tmp_path):
    store = ShopReviewStore(tmp_path / 'shop.sqlite', guild_id=GUILD, shop_channel_id=CHANNEL, operator_ids={USER})
    payload = fixture_content()
    actor = Actor(GUILD, CHANNEL, USER)
    content = verify_content(payload)
    review = store.create_draft(actor, item_id='1', sku='NEW-ITEM-1', product_ref=content.product_ref,
        content_digest=content.digest, quantity=2, request_id='initial')
    yield store, actor, review, payload
    store.close()

def view_for(store, review, payload, resolver=None):
    return ShopReviewView(store=store, review=review, title=payload['title'],
                          resolver=resolver or AsyncMock(return_value=payload), application_id=APP)

def complete(store, actor, review):
    return store.edit(actor, review.item_id, expected_revision=review.revision, request_id='details', price_cents=2599, ebay_url=URL)

def test_form_requires_both_fields_and_approval_stays_disabled_until_complete(setup):
    async def run():
        store, actor, review, payload = setup
        view = view_for(store, review, payload)
        assert view.approve.disabled and not view.details.disabled
        modal = ShopDetailsModal(view, USER)
        assert modal.price.required and modal.ebay.required
        ready = complete(store, actor, review)
        assert not view_for(store, ready, payload).approve.disabled
        assert '4G1P-WEBSITE/1' not in str(review_embed(ready, payload['title']).to_dict())
    asyncio.run(run())

def test_approval_is_queued_not_published(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        event = interaction()
        await view_for(store, ready, payload).approve_item(event)
        current = store.get_review(actor, '1')
        assert current.state == 'approved' and current.published_revision is None
        assert 'not live until' in event.followup.send.call_args.args[0]
    asyncio.run(run())

@pytest.mark.parametrize('field,value', [('guild_id', 999), ('channel_id', 999), ('application_id', 999)])
def test_foreign_interaction_never_resolves_or_approves(setup, field, value):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        resolver = AsyncMock(return_value=payload)
        event = interaction()
        setattr(event, field, value)
        await view_for(store, ready, payload, resolver).approve_item(event)
        resolver.assert_not_called()
        assert store.get_review(actor, '1').state == 'draft'
    asyncio.run(run())

def test_unapproved_user_and_bot_cannot_approve(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        for user in [SimpleNamespace(id=999, bot=False), SimpleNamespace(id=int(USER), bot=True)]:
            event = interaction()
            event.user = user
            await view_for(store, ready, payload).approve_item(event)
        assert store.get_review(actor, '1').state == 'draft'
    asyncio.run(run())

def test_changed_photos_or_text_require_new_review(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        changed = deepcopy(payload)
        changed['description'] = 'Changed contents, not the reviewed item description.'
        await view_for(store, ready, changed).approve_item(interaction())
        assert store.get_review(actor, '1').state == 'draft'
    asyncio.run(run())

def test_notice_failure_does_not_claim_committed_approval_was_undone(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        event = interaction()
        event.followup.send.side_effect = RuntimeError('synthetic transport unavailable')
        await view_for(store, ready, payload).approve_item(event)
        assert store.get_review(actor, '1').state == 'approved'
        assert event.followup.send.call_count == 1
        assert 'queued' in event.followup.send.call_args.args[0]
    asyncio.run(run())

def test_sold_during_content_read_blocks_approval(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        async def resolve(_):
            store.mark_sold(actor, '1', expected_revision=ready.revision, request_id='sale')
            return payload
        await view_for(store, ready, payload, resolve).approve_item(interaction())
        assert store.get_review(actor, '1').state == 'sold'
    asyncio.run(run())

def test_failed_new_card_can_be_reopened_from_stale_card(setup):
    async def run():
        store, actor, review, payload = setup
        view = view_for(store, review, payload)
        modal = ShopDetailsModal(view, USER)
        modal.price._value, modal.ebay._value = '25.99', URL
        event = interaction()
        event.channel.send.side_effect = RuntimeError('synthetic send failure')
        await modal.on_submit(event)
        saved = store.get_review(actor, '1')
        assert saved.price_cents == 2599 and saved.state == 'draft'
        assert saved.revision > review.revision
        retry = interaction(2)
        await view.refresh_review(retry)
        retry.channel.send.assert_awaited_once()
        sent = retry.channel.send.call_args.kwargs
        assert sent['view'].review.revision == saved.revision
        assert sent['embeds'][0].description.endswith(payload['description'])
        assert len(sent['files']) == 1 and not sent['view'].approve.disabled
        assert store.get_review(actor, '1').state == 'draft'
    asyncio.run(run())

def test_stale_or_other_user_modal_cannot_save(setup):
    async def run():
        store, actor, review, payload = setup
        view = view_for(store, review, payload)
        modal = ShopDetailsModal(view, USER)
        modal.price._value, modal.ebay._value = '25.99', URL
        newer = complete(store, actor, review)
        await modal.on_submit(interaction())
        assert store.get_review(actor, '1').revision == newer.revision
        other = interaction(2)
        other.user = SimpleNamespace(id=999, bot=False)
        await modal.on_submit(other)
        assert store.get_review(actor, '1').revision == newer.revision
    asyncio.run(run())

def test_existing_approved_item_can_open_details_for_fresh_review(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        store.approve(actor, '1', expected_revision=ready.revision, request_id='approve', validated_content_digest=ready.content_digest)
        approved = store.get_review(actor, '1')
        view = view_for(store, approved, payload)
        assert not view.details.disabled and view.approve.disabled
        event = interaction()
        await view.open_details(event)
        assert event.response.modal is not None
    asyncio.run(run())

def test_changed_content_is_shown_before_fresh_approval(setup):
    async def run():
        store, actor, review, payload = setup
        new = deepcopy(payload)
        new['description'] = 'New description explicitly shown for review.'
        view = view_for(store, review, new)
        modal = ShopDetailsModal(view, USER)
        modal.price._value, modal.ebay._value = '25.99', URL
        event = interaction()
        await modal.on_submit(event)
        current = store.get_review(actor, '1')
        assert current.state == 'draft' and current.content_digest == verify_content(new).digest
        assert new['description'] in event.channel.send.call_args.kwargs['embeds'][0].description
    asyncio.run(run())

def test_review_posts_are_append_only_and_scoped_to_owned_bot(setup):
    async def run():
        store, actor, review, payload = setup
        event = interaction()
        await post_review(event.channel, store=store, review=review, title=payload['title'], resolver=AsyncMock(return_value=payload), application_id=APP)
        kwargs = event.channel.send.call_args.kwargs
        assert kwargs['allowed_mentions'].everyone is False
        event.channel.guild.me.id = 999
        with pytest.raises(ValueError):
            await post_review(event.channel, store=store, review=review, title=payload['title'], resolver=AsyncMock(return_value=payload), application_id=APP)
        assert event.channel.send.call_count == 1
    asyncio.run(run())

def test_confirming_approval_deletes_the_card(setup):
    """
    Regression guard: the card served its purpose once actually accepted -
    leaving it in #website_shop forever just accumulates clutter. Deleting
    it must never happen before the approval is actually committed (see the
    other approve_item tests above for that), only after.
    """
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        event = interaction()
        event.message.delete = AsyncMock()
        await view_for(store, ready, payload).approve_item(event)
        event.message.delete.assert_awaited_once()
    asyncio.run(run())


def test_a_rejected_approval_does_not_delete_the_card(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        changed = deepcopy(payload)
        changed['description'] = 'Changed contents, not the reviewed item description.'
        event = interaction()
        event.message.delete = AsyncMock()
        await view_for(store, ready, changed).approve_item(event)
        event.message.delete.assert_not_awaited()
    asyncio.run(run())


def test_entering_details_deletes_the_prior_card_once_the_new_one_is_posted(setup):
    """
    Regression guard: entering price/eBay link used to always post a brand
    new card and leave the old one behind forever (deliberately, per the
    removed "append-only" comment) - accumulating one stale, dead-buttoned
    card per edit. The new card is posted FIRST (so a failed post never
    loses the fallback - see the next test), and only once that succeeds is
    the old one cleaned up.
    """
    async def run():
        store, actor, review, payload = setup
        view = view_for(store, review, payload)
        source_message = SimpleNamespace(delete=AsyncMock())
        modal = ShopDetailsModal(view, USER, source_message)
        modal.price._value, modal.ebay._value = '25.99', URL
        event = interaction()
        await modal.on_submit(event)
        source_message.delete.assert_awaited_once()
    asyncio.run(run())


def test_a_failed_new_card_post_does_not_delete_the_old_one(setup):
    async def run():
        store, actor, review, payload = setup
        view = view_for(store, review, payload)
        source_message = SimpleNamespace(delete=AsyncMock())
        modal = ShopDetailsModal(view, USER, source_message)
        modal.price._value, modal.ebay._value = '25.99', URL
        event = interaction()
        event.channel.send.side_effect = RuntimeError('synthetic send failure')
        await modal.on_submit(event)
        source_message.delete.assert_not_awaited()
    asyncio.run(run())


def test_condition_classification_is_visible_not_just_notes(setup):
    store, actor, review, payload = setup
    first = review_embed(review, payload['title'], payload).to_dict()['fields'][0]['value']
    payload['condition'] = 'open_box'
    second = review_embed(review, payload['title'], payload).to_dict()['fields'][0]['value']
    assert first.startswith('Used\n') and second.startswith('Open box\n')
    assert first != second

def test_editing_published_item_keeps_old_live_revision_and_requires_approval(setup):
    async def run():
        store, actor, review, payload = setup
        ready = complete(store, actor, review)
        store.approve(actor, '1', expected_revision=ready.revision, request_id='approve', validated_content_digest=ready.content_digest)
        job = store.claim_next('synthetic-worker')
        published = store.acknowledge(job.job_id, revision=job.revision, content_digest=job.content_digest,
            claim_token=job.claim_token, delivery_receipt='synthetic-receiver:1')
        assert published.state == 'published'
        view = view_for(store, published, payload)
        assert not view.details.disabled and view.approve.disabled
        event = interaction()
        await view.open_details(event)
        modal = event.response.modal
        modal.price._value, modal.ebay._value = '29.99', URL
        await modal.on_submit(interaction(2))
        changed = store.get_review(actor, '1')
        assert changed.state == 'draft' and changed.revision > published.revision
        assert changed.published_revision == published.revision and changed.price_cents == 2999
        assert store.claim_next('synthetic-worker') is None
    asyncio.run(run())
