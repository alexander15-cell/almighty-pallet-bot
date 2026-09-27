"""Synthetic stores and fake transport only; no profiles, network or live data."""
import asyncio
import copy
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from combined_delivery import DeliveryError, DeliveryService
from publisher.contract import Card, ContractError, normalize_photo
from publisher.journal import Journal
from publisher.transport import Delivery
from shop_approval import Actor, ShopApprovalError, ShopReviewStore
from shop_content import verify_content

GUILD = "100000000000000001"
SHOP = "100000000000000002"
BOT = "100000000000000003"
LISTED = "100000000000000004"
SOLD = "100000000000000005"
OPERATOR = "100000000000000006"
ITEM = "1000000001"
ACTOR = Actor(GUILD, SHOP, OPERATOR)
WEBSITE = "https://fixture.example"
SOURCE = "a" * 32


class FakeTransport:
    website = WEBSITE

    def __init__(self):
        self.calls = []
        self.responses = []

    def post(self, suffix, body):
        self.calls.append((suffix, body))
        if self.responses:
            return self.responses.pop(0)
        if suffix == "heartbeat":
            return Delivery(True, value={"ok": True})
        return Delivery(True, value={"outcome": "applied", "version": json.loads(body)["version"]})


class CombinedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.clock = [1000.0]
        self.socket = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.socket.start()
        self.store = self.open_store()
        self.journal = self.open_journal()
        self.transport = FakeTransport()
        self.source = "listed"
        self.resolve_calls = 0
        self.resolve_error = None
        self.lifecycle_error = False
        output = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
        self.content = {"product_ref": "intake-" + ITEM, "title": "Fixture item", "description": "Fixture description",
                        "condition": "used", "condition_notes": "Returned untested", "quantity": 2,
                        "photos": [normalize_photo(output.getvalue())], "source_state": "listed"}
        self.service = self.make_service()

    def tearDown(self):
        self.store.close()
        self.journal.close()
        self.socket.stop()
        self.temp.cleanup()

    def open_store(self):
        return ShopReviewStore(self.directory / "shop.sqlite", guild_id=GUILD, shop_channel_id=SHOP,
                               operator_ids=[OPERATOR], now=lambda: self.clock[0])

    def open_journal(self):
        return Journal(self.directory / "publisher.sqlite", SOURCE, GUILD, WEBSITE, BOT, LISTED, SOLD, now=lambda: self.clock[0])

    async def resolver(self, product_ref):
        self.resolve_calls += 1
        self.assertEqual(product_ref, self.content["product_ref"])
        if self.resolve_error:
            raise self.resolve_error
        return copy.deepcopy(self.content)

    async def lifecycle(self, item_id):
        self.assertEqual(item_id, ITEM)
        if self.lifecycle_error:
            raise RuntimeError("Private upstream details never leak")
        return self.source

    def make_service(self, **overrides):
        options = dict(store=self.store, journal=self.journal, transport=self.transport, resolver=self.resolver,
                       lifecycle_resolver=self.lifecycle, enabled=True, heartbeat=False)
        options.update(overrides)
        return DeliveryService(**options)

    def draft(self):
        return self.store.create_draft(ACTOR, item_id=ITEM, sku="FGNEW-PALLET-1-ITEM-1", product_ref=self.content["product_ref"],
            content_digest=verify_content(self.content).digest, price_cents=2999, ebay_url="https://www.ebay.com/itm/123456789012",
            quantity=2, request_id="create-1")

    def approve(self, revision=1):
        return self.store.approve(ACTOR, ITEM, expected_revision=revision, request_id="approve-" + str(revision),
                                  validated_content_digest=verify_content(self.content).digest)

    def body(self, index=-1):
        return json.loads(self.transport.calls[index][1])

    async def test_no_draft_or_disabled_publication(self):
        self.draft()
        await self.service.tick()
        self.approve()
        self.service.enabled = False
        outcome = await self.service.tick()
        self.assertFalse(outcome["enabled"])
        self.assertEqual(self.transport.calls, [])
        self.assertIsNone(self.journal.get_item(ITEM))

    async def test_disabled_preview_needs_no_transport_or_secret(self):
        service = self.make_service(transport=None, enabled=False)
        self.assertFalse((await service.tick())["enabled"])
        with self.assertRaisesRegex(DeliveryError, "enabled_delivery_requires_transport"):
            self.make_service(transport=None, enabled=True)
        service.enabled = True
        with self.assertRaisesRegex(DeliveryError, "enabled_delivery_requires_transport"):
            await service.tick()
        self.assertEqual(self.transport.calls, [])

    async def test_delivery_batch_limit_is_validated_and_applied_to_outbox(self):
        for value in (True, 0, 26, 1.5, "1", None):
            with self.subTest(value=value), self.assertRaisesRegex(DeliveryError, "invalid_delivery_limit"):
                self.make_service(max_deliveries=value)
        self.draft()
        self.approve()
        service = self.make_service(max_deliveries=1)
        with patch.object(self.journal, "due", wraps=self.journal.due) as due:
            await service.tick()
        due.assert_called_once_with(1)
        self.assertEqual(len(self.transport.calls), 1)

    async def test_heartbeat_once_after_tick_failure_reported_and_retried_next_tick(self):
        self.service.heartbeat_enabled = True
        self.transport.responses = [Delivery(False, True, "http_error")]
        failed = await self.service.tick()
        self.assertEqual((failed["heartbeat_ok"], failed["failed"], failed["heartbeat_error"]),
                         (False, 1, "heartbeat_unavailable"))
        success = await self.service.tick()
        self.assertEqual((success["heartbeat_ok"], success["failed"]), (True, 0))
        self.assertEqual([suffix for suffix, _ in self.transport.calls], ["heartbeat", "heartbeat"])
        self.assertEqual(set(self.body()), {"schemaVersion", "sourceId", "guildId", "pending", "failed"})

    async def test_explicit_approval_sends_quantity_price_link_and_ack_exact_version(self):
        self.draft()
        job = self.approve()
        result = await self.service.tick()
        self.assertEqual((result["sent"], result["acknowledged"], result["pending"]), (1, 1, 0))
        body = self.body()
        self.assertEqual((body["version"], body["sku"], body["itemId"]), (1, "FGNEW-PALLET-1-ITEM-1", ITEM))
        self.assertEqual((body["listing"]["priceCents"], body["listing"]["quantity"], body["listing"]["ebayItemId"]),
                         (2999, 2, "123456789012"))
        self.assertEqual(self.store.delivery_job(job.job_id).state, "published")
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 1)

    async def test_retry_is_durable_same_bytes_same_version_after_restart(self):
        self.draft()
        self.approve()
        self.transport.responses = [Delivery(False, True, "website_unreachable_or_invalid_response")]
        first = await self.service.tick()
        self.assertEqual((first["pending"], first["failed"]), (1, 1))
        original = self.transport.calls[0][1]
        self.store.close()
        self.journal.close()
        self.store, self.journal = self.open_store(), self.open_journal()
        self.service = self.make_service()
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 1)  # durable backoff
        self.clock[0] += 6
        await self.service.tick()
        self.assertEqual(self.transport.calls[1][1], original)
        self.assertEqual(self.store.system_get_review(ITEM).state, "published")

    async def test_network_timeout_then_sold_replaces_pending_listing_before_retry(self):
        self.draft()
        self.approve()
        self.transport.responses = [Delivery(False, True, "http_error")]
        await self.service.tick()
        self.source = "sold"
        result = await self.service.tick()
        self.assertEqual((self.body()["version"], self.body()["status"], self.body()["listing"]), (2, "sold", None))
        self.assertEqual((result["withdrawn"], result["pending"]), (1, 0))
        self.assertEqual(self.store.system_get_review(ITEM).state, "sold")

    async def test_sold_before_claim_never_sends_listing_or_creates_unapproved_product(self):
        self.draft()
        job = self.approve()
        self.source = "sold"
        await self.service.tick()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.delivery_job(job.job_id).state, "cancelled")

    async def test_shipped_with_bad_photos_still_withdraws_published_listing(self):
        self.draft()
        self.approve()
        await self.service.tick()
        calls = self.resolve_calls
        self.resolve_error = ValueError("no photo")
        self.source = "shipped"
        await self.service.tick()
        self.assertEqual(self.resolve_calls, calls)
        self.assertEqual((self.body()["status"], self.body()["listing"]), ("shipped", None))

    async def test_sold_can_advance_to_shipped_but_never_back_to_held(self):
        self.draft()
        self.approve()
        await self.service.tick()
        self.source = "sold"
        await self.service.tick()
        self.source = "shipped"
        await self.service.tick()
        self.assertEqual((self.body()["status"], self.body()["version"]), ("shipped", 3))
        self.source = "held"
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 3)

    async def test_hold_release_requires_new_review_and_never_auto_republishes(self):
        self.draft()
        self.approve()
        await self.service.tick()
        self.source = "held"
        await self.service.tick()
        self.assertEqual((self.body()["status"], self.body()["onHold"], self.body()["listing"]), ("listed", True, None))
        held = self.store.system_get_review(ITEM)
        self.source = "listed"
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 2)
        released = self.store.system_release_hold(ITEM, expected_revision=held.revision, request_id="release")
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 2)
        self.assertIsNone(released.content_digest)

    async def test_changed_content_invalidates_job_without_publishing(self):
        self.draft()
        job = self.approve()
        self.content["title"] = "Changed after approval"
        result = await self.service.tick()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.store.delivery_job(job.job_id).state, "cancelled")
        self.assertEqual(result["review_blocked"], 1)

    async def test_changed_content_on_reprice_withdraws_old_live_listing(self):
        self.draft()
        self.approve()
        await self.service.tick()
        self.store.edit(ACTOR, ITEM, expected_revision=1, request_id="reprice", price_cents=1999)
        self.approve(2)
        self.content["description"] = "Changed content"
        await self.service.tick()
        self.assertIsNone(self.body()["listing"])
        self.assertTrue(self.body()["onHold"])

    async def test_draft_repricing_preserves_old_published_revision_until_approval(self):
        self.draft()
        self.approve()
        await self.service.tick()
        self.store.edit(ACTOR, ITEM, expected_revision=1, request_id="reprice", price_cents=1999)
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.system_get_review(ITEM).published_revision, 1)
        self.approve(2)
        await self.service.tick()
        self.assertEqual((self.body()["version"], self.body()["listing"]["priceCents"]), (2, 1999))

    async def test_cancelled_uncertain_delivery_is_fenced_before_new_approval(self):
        self.draft()
        self.approve()
        self.transport.responses = [Delivery(False, True, "http_error")]
        await self.service.tick()
        self.store.edit(ACTOR, ITEM, expected_revision=1, request_id="edit", price_cents=1599)
        await self.service.tick()
        self.assertEqual((self.body()["version"], self.body()["status"]), (2, "deleted"))
        self.assertEqual(self.store.system_get_review(ITEM).state, "draft")
        self.approve(2)
        await self.service.tick()
        self.assertEqual((self.body()["version"], self.body()["listing"]["priceCents"]), (3, 1599))

    async def test_receiver_wrong_version_blocks_without_ack_or_auto_retry(self):
        self.draft()
        job = self.approve()
        self.transport.responses = [Delivery(True, value={"outcome": "ignored", "version": 22})]
        result = await self.service.tick()
        self.assertEqual((result["pending"], result["failed"]), (1, 1))
        self.assertEqual(self.store.delivery_job(job.job_id).state, "claimed")
        self.clock[0] += 1000
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 1)

    async def test_crash_between_journal_ack_and_shop_ack_recovers_without_http_repost(self):
        self.draft()
        job = self.approve()
        original = self.store.acknowledge
        with patch.object(self.store, "acknowledge", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                await self.service.tick()
        self.assertEqual(self.journal.counts()["pending"], 0)
        self.clock[0] += 301
        await self.service.tick()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.store.delivery_job(job.job_id).state, "published")

    async def test_lifecycle_failure_sends_nothing_and_leaks_no_private_exception(self):
        self.draft()
        self.approve()
        self.lifecycle_error = True
        with self.assertRaisesRegex(DeliveryError, "^source_lifecycle_unavailable$"):
            await self.service.tick()
        self.assertEqual(self.transport.calls, [])

    async def test_source_changes_during_content_validation_prevent_send(self):
        self.draft()
        self.approve()
        async def changed(ref):
            value = await self.resolver(ref)
            self.source = "sold"
            return value
        self.service.resolver = changed
        await self.service.tick()
        self.assertEqual(self.transport.calls, [])
        await self.service.tick()
        self.assertEqual(self.body()["status"], "sold")
        self.assertTrue(all(json.loads(body)["listing"] is None for _, body in self.transport.calls))

    async def test_journal_cannot_adopt_existing_observer_namespace(self):
        observer = Journal(self.directory / "observer.sqlite", SOURCE, GUILD, WEBSITE, BOT, LISTED, SOLD)
        try:
            observer.observe(Card(ITEM, "LEGACY-SKU", "listed", True, None, "100000000000000099", LISTED, ""))
            with self.assertRaisesRegex(ContractError, "journal_review_requires_fresh_namespace"):
                self.make_service(journal=observer)
        finally:
            observer.close()

    async def test_shop_journal_rejects_observer_and_wrong_binding(self):
        with self.assertRaisesRegex(ContractError, "review_journal_disallows_observer"):
            self.journal.observe(Card(ITEM, "FGNEW-SKU", "listed", True, None, "100000000000000099", LISTED, ""))
        with self.assertRaisesRegex(ContractError, "journal_review_binding_mismatch"):
            self.journal.bind_shop_reviews("100000000000000009")

    async def test_destination_mismatch_is_rejected(self):
        wrong = FakeTransport()
        wrong.website = "https://wrong.example"
        with self.assertRaisesRegex(DeliveryError, "delivery_scope_mismatch"):
            self.make_service(transport=wrong)

    async def test_concurrent_ticks_do_not_duplicate_http_delivery(self):
        self.draft()
        self.approve()
        await asyncio.gather(self.service.tick(), self.service.tick())
        self.assertEqual(len(self.transport.calls), 1)

    async def test_sale_during_http_is_not_acknowledged_as_published(self):
        self.draft()
        job = self.approve()
        original = self.transport.post
        def sell(suffix, body):
            result = original(suffix, body)
            self.source = "sold"
            return result
        self.transport.post = sell
        result = await self.service.tick()
        self.assertEqual((result["acknowledged"], result["pending"]), (0, 1))
        self.assertEqual(self.store.delivery_job(job.job_id).state, "cancelled")
        await self.service.tick()
        self.assertEqual((self.body()["status"], self.body()["version"]), ("sold", 2))

    async def test_cancelled_http_finishes_before_tick_releases_and_retains_retry(self):
        self.draft()
        self.approve()
        started, finish = threading.Event(), threading.Event()
        original = self.transport.post
        def blocked(suffix, body):
            started.set()
            if not finish.wait(5):
                raise AssertionError("test did not release fake HTTP")
            return original(suffix, body)
        self.transport.post = blocked
        task = asyncio.create_task(self.service.tick())
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
        finally:
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.journal.counts()["pending"], 1)
        self.transport.post = original
        await self.service.tick()
        self.assertEqual(self.transport.calls[0][1], self.transport.calls[1][1])


if __name__ == "__main__":
    unittest.main()
