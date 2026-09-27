"""Synthetic local SQLite tests; no Discord, private profiles or network."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shop_approval import Actor, ShopApprovalError, ShopReviewStore, missing_fields

GUILD = "100000000000000001"
CHANNEL = "100000000000000002"
OPERATORS = ("100000000000000003", "100000000000000004")
ACTOR = Actor(GUILD, CHANNEL, OPERATORS[0])
DIGEST = "a" * 64
LINK = "https://www.ebay.com/itm/123456789012"


class ShopApprovalTests(unittest.TestCase):
    def test_system_source_staging_audits_system_and_cannot_approve_or_set_price(self):
        self.assertIsNone(self.store.system_get_review("7"))
        review = self.store.system_create_draft(item_id="7", sku="SKU7", product_ref="intake-7",
            content_digest=DIGEST, quantity=2, request_id="source-create")
        self.assertEqual((review.state, review.price_cents, review.ebay_url), ("draft", None, None))
        self.assertCode("shop_approval_fields_missing", lambda: self.approve())
        review = self.store.system_refresh("7", expected_revision=1, request_id="source-refresh",
                                          content_digest="b" * 64, quantity=3)
        self.assertEqual((review.revision, review.quantity), (2, 3))
        self.assertEqual({row[0] for row in self.store.connection.execute("SELECT actor_id FROM shop_audit")}, {"system:intake"})
        with self.assertRaises(TypeError):
            self.store.system_create_draft(item_id="8", sku="SKU8", product_ref="intake-8", request_id="bad", price_cents=100)

    def test_system_lifecycle_is_idempotent_cancels_and_never_reactivates(self):
        self.draft()
        job = self.approve()
        held = self.store.invalidate_from_source("7", "held")
        self.assertEqual(self.store.delivery_job(job.job_id).state, "cancelled")
        self.assertEqual(self.store.invalidate_from_source("7", "held").revision, held.revision)
        draft = self.store.system_release_hold("7", expected_revision=held.revision, request_id="system-release")
        self.assertIsNone(draft.content_digest)
        sold = self.store.invalidate_from_source("7", "sold")
        self.assertEqual(self.store.invalidate_from_source("7", "held"), sold)
        self.assertEqual(self.store.delivery_reviews(), [sold])
        self.assertCode("invalid_source_state", lambda: self.store.invalidate_from_source("7", "listed"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.path = self.directory / "shop.sqlite"
        self.clock = [1000.0]
        self.store = self.open_store()
        self.socket = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        self.socket.start()

    def tearDown(self):
        self.socket.stop()
        self.store.close()
        self.temp.cleanup()

    def open_store(self, **changes):
        options = {"guild_id": GUILD, "shop_channel_id": CHANNEL, "operator_ids": OPERATORS, "now": lambda: self.clock[0]}
        options.update(changes)
        return ShopReviewStore(self.path, **options)

    def draft(self, **changes):
        values = {"item_id": "7", "sku": "MAIN-PALLET-1-ITEM-7", "product_ref": "intake-7", "request_id": "create-7",
                  "content_digest": DIGEST, "price_cents": 2599, "ebay_url": LINK, "quantity": 2}
        values.update(changes)
        return self.store.create_draft(ACTOR, **values)

    def approve(self, revision=1, request_id="approve-7"):
        return self.store.approve(ACTOR, "7", expected_revision=revision, request_id=request_id, validated_content_digest=DIGEST)

    def acknowledge(self, job, receipt="website-7-v1"):
        return self.store.acknowledge(job.job_id, revision=job.revision, content_digest=job.content_digest,
                                      claim_token=job.claim_token, delivery_receipt=receipt)

    def count(self, table):
        return self.store.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def assertCode(self, code, callable):
        with self.assertRaises(ShopApprovalError) as caught:
            callable()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_missing_price_link_quantity_and_manifest_are_drafts_not_jobs(self):
        review = self.draft(price_cents=None, ebay_url=None, quantity=None, content_digest=None)
        self.assertEqual(review.state, "draft")
        self.assertEqual(missing_fields(review), ("price_cents", "ebay_url", "quantity", "content_digest"))
        error = self.assertCode("shop_approval_fields_missing", lambda: self.approve())
        self.assertEqual(error.missing_fields, missing_fields(review))
        self.assertEqual(self.count("shop_jobs"), 0)
        self.assertEqual(self.count("shop_audit"), 1)
        self.assertIsNone(self.store.claim_next("sender-a"))

    def test_canonical_integer_values_queue_only_after_explicit_approval(self):
        original = self.draft(price_cents=None, ebay_url=None)
        changed = self.store.edit(ACTOR, "7", expected_revision=original.revision, request_id="edit-7",
                                  price_cents=1234, ebay_url="https://ebay.com/itm/Title/123456789012?tracking=fixture")
        self.assertEqual((changed.revision, changed.state, changed.price_cents, changed.ebay_url, changed.quantity), (2, "draft", 1234, LINK, 2))
        self.assertEqual(self.count("shop_jobs"), 0)
        job = self.approve(2)
        self.assertEqual((job.state, job.price_cents, job.ebay_item_id, job.quantity, job.content_digest), ("queued", 1234, "123456789012", 2, DIGEST))
        self.assertEqual(self.store.get_review(ACTOR, "7").state, "approved")
        self.assertIsNone(self.store.get_review(ACTOR, "7").published_revision)

    def test_scope_and_operator_guard_every_human_read_or_mutation(self):
        self.draft()
        calls = [
            lambda actor: self.store.get_review(actor, "7"),
            lambda actor: self.store.create_draft(actor, item_id="8", sku="SKU8", product_ref="intake-8", request_id="create-8"),
            lambda actor: self.store.edit(actor, "7", expected_revision=1, request_id="edit-7", price_cents=500),
            lambda actor: self.store.approve(actor, "7", expected_revision=1, request_id="approve-7", validated_content_digest=DIGEST),
            lambda actor: self.store.hold(actor, "7", expected_revision=1, request_id="hold-7"),
            lambda actor: self.store.release_hold(actor, "7", expected_revision=1, request_id="release-7"),
            lambda actor: self.store.mark_sold(actor, "7", expected_revision=1, request_id="sold-7"),
            lambda actor: self.store.withdraw(actor, "7", expected_revision=1, request_id="withdraw-7"),
        ]
        for actor in (replace(ACTOR, guild_id="100000000000000099"), replace(ACTOR, channel_id="100000000000000099"),
                      replace(ACTOR, user_id="100000000000000099"), None):
            for call in calls:
                with self.subTest(actor=actor, call=call), self.assertRaises(ShopApprovalError):
                    call(actor)
        self.assertEqual(self.count("shop_audit"), 1)
        self.assertEqual(self.count("shop_items"), 1)
        self.assertEqual(self.count("shop_jobs"), 0)

    def test_scope_binds_existing_store_and_other_operator_can_review(self):
        self.draft()
        self.assertEqual(self.store.get_review(replace(ACTOR, user_id=OPERATORS[1]), "7").sku, "MAIN-PALLET-1-ITEM-7")
        for changes in ({"guild_id": "100000000000000099"}, {"shop_channel_id": "100000000000000099"},
                        {"operator_ids": [OPERATORS[0]]}):
            with self.subTest(changes=changes):
                self.assertCode("shop_store_scope_mismatch", lambda: self.open_store(**changes))
        other = self.open_store()
        try:
            self.assertEqual(other.get_review(ACTOR, "7").revision, 1)
        finally:
            other.close()

    def test_foreign_and_empty_database_files_are_never_adopted_or_changed(self):
        for name in ("foreign.sqlite", "empty.sqlite", "not-sqlite.db"):
            path = self.directory / name
            if name == "foreign.sqlite":
                foreign = sqlite3.connect(path)
                foreign.execute("CREATE TABLE old_products(id TEXT PRIMARY KEY, quantity INTEGER)")
                foreign.execute("INSERT INTO old_products VALUES ('preserve-56',98)")
                foreign.commit()
                foreign.close()
            else:
                path.write_bytes(b"" if name == "empty.sqlite" else b"unrelated data")
            before = path.read_bytes()
            with self.assertRaises(ShopApprovalError):
                ShopReviewStore(path, guild_id=GUILD, shop_channel_id=CHANNEL, operator_ids=OPERATORS)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(str(path) + "-wal").exists())

    def test_audit_revisions_requests_and_identity_cannot_be_rewritten(self):
        self.draft()
        for table in ("shop_audit", "shop_revisions", "shop_requests", "shop_meta"):
            for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET " + {"shop_audit": "action='changed'", "shop_revisions": "price_cents=1", "shop_requests": "action='changed'", "shop_meta": "value='changed'"}[table]):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(sql)
        for sql in ("UPDATE shop_items SET sku='adopted'", "UPDATE shop_items SET product_ref='another'", "DELETE FROM shop_items"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.connection.execute(sql)
        self.assertEqual(self.count("shop_audit"), 1)

    def test_duplicate_item_sku_or_product_reference_is_not_adopted(self):
        self.draft()
        for changes in ({"request_id": "other-request"},
                        {"item_id": "8", "product_ref": "intake-8", "request_id": "create-8"},
                        {"item_id": "8", "sku": "OTHER-SKU", "request_id": "create-8"}):
            with self.subTest(changes=changes):
                self.assertCode("shop_item_identity_conflict", lambda: self.draft(**changes))
        self.assertEqual(self.count("shop_items"), 1)

    def test_repricing_keeps_last_published_revision_until_new_approval_and_ack(self):
        self.draft()
        original_job = self.approve()
        claimed = self.store.claim_next("sender-a")
        published = self.acknowledge(claimed)
        self.assertEqual((published.state, published.published_revision), ("published", 1))
        draft = self.store.edit(ACTOR, "7", expected_revision=1, request_id="reprice", price_cents=1999)
        self.assertEqual((draft.state, draft.revision, draft.published_revision), ("draft", 2, 1))
        self.assertIsNone(self.store.claim_next("sender-a"))
        self.assertEqual(self.store._job(original_job.job_id).price_cents, 2599)
        self.approve(2, "approve-reprice")
        replacement = self.store.claim_next("sender-a")
        self.assertEqual((replacement.revision, replacement.price_cents), (2, 1999))
        published = self.acknowledge(replacement, "website-7-v2")
        self.assertEqual((published.state, published.published_revision), ("published", 2))
        self.assertEqual(self.count("shop_revisions"), 2)
        self.assertEqual(self.count("shop_jobs"), 2)

    def test_request_replay_does_not_change_revision_or_repeat_audit(self):
        self.draft()
        self.draft()
        self.assertEqual(self.count("shop_audit"), 1)
        first = self.store.edit(ACTOR, "7", expected_revision=1, request_id="edit-id", price_cents=1999)
        second = self.store.edit(ACTOR, "7", expected_revision=1, request_id="edit-id", price_cents=1999)
        self.assertEqual(first, second)
        self.assertEqual(self.count("shop_audit"), 2)
        self.assertCode("shop_request_id_conflict", lambda: self.store.edit(ACTOR, "7", expected_revision=1, request_id="edit-id", price_cents=1998))

    def test_approve_repeated_requests_create_one_job_and_ack_is_idempotent(self):
        self.draft()
        first = self.approve()
        repeated = self.approve()
        reaffirmed = self.approve(request_id="another-approval")
        self.assertEqual((first.job_id, first.job_id), (repeated.job_id, reaffirmed.job_id))
        self.assertEqual(self.count("shop_jobs"), 1)
        claimed = self.store.claim_next("sender-a")
        first_review = self.acknowledge(claimed)
        audit_count = self.count("shop_audit")
        self.assertEqual(self.acknowledge(claimed), first_review)
        self.assertEqual(self.count("shop_audit"), audit_count)
        self.assertCode("shop_acknowledgement_conflict", lambda: self.acknowledge(claimed, "different-receipt"))

    def test_stale_revision_and_wrong_manifest_cannot_approve(self):
        self.draft()
        self.assertCode("shop_content_digest_mismatch", lambda: self.store.approve(ACTOR, "7", expected_revision=1, request_id="wrong-digest", validated_content_digest="b" * 64))
        self.store.edit(ACTOR, "7", expected_revision=1, request_id="edit-7", price_cents=1000)
        self.assertCode("shop_revision_conflict", lambda: self.approve())
        self.assertCode("shop_revision_conflict", lambda: self.store.edit(ACTOR, "7", expected_revision=1, request_id="old-edit", price_cents=500))
        self.assertEqual(self.count("shop_jobs"), 0)

    def test_edit_cancels_claimed_job_and_old_ack_cannot_publish(self):
        self.draft()
        self.approve()
        job = self.store.claim_next("sender-a")
        self.store.edit(ACTOR, "7", expected_revision=1, request_id="edit-7", price_cents=1800)
        self.assertFalse(self.store.publication_guard(job.job_id, job.revision, job.content_digest, job.claim_token))
        self.assertCode("shop_publication_is_stale", lambda: self.acknowledge(job))
        self.assertEqual(self.store._job(job.job_id).state, "cancelled")
        self.assertIsNone(self.store.get_review(ACTOR, "7").published_revision)

    def test_sold_supersedes_claimed_approval_and_is_terminal(self):
        self.draft()
        self.approve()
        job = self.store.claim_next("sender-a")
        sold = self.store.mark_sold(ACTOR, "7", expected_revision=1, request_id="sold-7")
        self.assertEqual((sold.state, sold.revision), ("sold", 2))
        self.assertFalse(self.store.publication_guard(job.job_id, job.revision, job.content_digest, job.claim_token))
        self.assertCode("shop_publication_is_stale", lambda: self.acknowledge(job))
        self.assertIsNone(self.store.claim_next("sender-a"))
        self.assertCode("shop_item_not_approvable", lambda: self.approve(2, "after-sale"))
        self.assertCode("shop_item_not_editable", lambda: self.store.edit(ACTOR, "7", expected_revision=2, request_id="after-sale-edit", price_cents=999))
        self.assertCode("shop_terminal_item_requires_new_flow", lambda: self.store.release_hold(ACTOR, "7", expected_revision=2, request_id="release-sold"))
        self.assertEqual(self.approve().state, "cancelled")  # Retry never reactivates.

    def test_hold_cancels_queue_and_release_requires_restaged_content_and_new_approval(self):
        self.draft()
        approved = self.approve()
        held = self.store.hold(ACTOR, "7", expected_revision=1, request_id="hold-7")
        self.assertEqual(held.state, "held")
        self.assertEqual(self.store._job(approved.job_id).state, "cancelled")
        self.assertIsNone(self.store.claim_next("sender-a"))
        released = self.store.release_hold(ACTOR, "7", expected_revision=2, request_id="release-7")
        self.assertEqual((released.state, released.revision, released.content_digest), ("draft", 3, None))
        self.assertCode("shop_approval_fields_missing", lambda: self.approve(3, "release-approval"))
        restaged = self.store.edit(ACTOR, "7", expected_revision=3, request_id="restage-7", content_digest=DIGEST)
        self.assertEqual(restaged.revision, 4)
        self.assertEqual(self.approve(4, "review-release").state, "queued")

    def test_withdrawal_and_old_ack_replay_do_not_reactivate_previous_publication(self):
        self.draft()
        self.approve()
        job = self.store.claim_next("sender-a")
        self.acknowledge(job)
        withdrawn = self.store.withdraw(ACTOR, "7", expected_revision=1, request_id="withdraw-7")
        self.assertEqual((withdrawn.state, withdrawn.published_revision), ("withdrawn", 1))
        self.assertEqual(self.acknowledge(job).state, "withdrawn")
        self.assertCode("shop_item_not_approvable", lambda: self.approve(2, "reapprove-withdrawn"))
        self.assertIsNone(self.store.claim_next("sender-a"))

    def test_claim_lease_expiry_fences_old_worker_and_reclaims_same_snapshot(self):
        self.draft()
        self.approve()
        old = self.store.claim_next("sender-a", lease_seconds=10)
        self.assertIsNone(self.store.claim_next("sender-b"))
        self.clock[0] += 11
        self.assertFalse(self.store.publication_guard(old.job_id, old.revision, old.content_digest, old.claim_token))
        new = self.store.claim_next("sender-b")
        self.assertEqual((new.job_id, new.revision, new.price_cents), (old.job_id, old.revision, old.price_cents))
        self.assertNotEqual(new.claim_token, old.claim_token)
        self.assertCode("shop_publication_is_stale", lambda: self.acknowledge(old))
        self.assertEqual(self.acknowledge(new).state, "published")

    def test_two_connections_cannot_claim_or_ack_the_same_current_lease(self):
        self.draft()
        self.approve()
        other = self.open_store()
        try:
            job = self.store.claim_next("sender-a")
            self.assertIsNone(other.claim_next("sender-b"))
            other.hold(ACTOR, "7", expected_revision=1, request_id="other-hold")
            self.assertCode("shop_publication_is_stale", lambda: self.acknowledge(job))
            self.assertEqual(self.store.get_review(ACTOR, "7").state, "held")
        finally:
            other.close()

    def test_invalid_opaque_metadata_item_and_digest_never_create_rows(self):
        for changes in ({"product_ref": "../private/file"}, {"product_ref": "C:\\private\\file"},
                        {"product_ref": "https://example.com/item"}, {"product_ref": "x" * 129},
                        {"item_id": "0"}, {"item_id": 7}, {"item_id": "2147483648"},
                        {"content_digest": "A" * 64}, {"content_digest": "a" * 63}, {"sku": "../sku"}):
            with self.subTest(changes=changes), self.assertRaises(ShopApprovalError):
                self.draft(**changes)
        self.assertEqual(self.count("shop_items"), 0)

    def test_invalid_price_link_quantity_rejected_without_mutation(self):
        self.draft()
        for key, values in {"price_cents": [True, 0, -1, 1.5, "12", float("nan"), 2147483648],
                            "quantity": [True, 0, -1, 1.5, "2", 2147483648],
                            "ebay_url": ["https://attacker.example/123456789012", LINK + "?var=123"]}.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ShopApprovalError):
                    self.store.edit(ACTOR, "7", expected_revision=1, request_id="bad-edit", **{key: value})
        self.assertEqual(self.store.get_review(ACTOR, "7").revision, 1)
        self.assertEqual(self.count("shop_audit"), 1)
        self.assertEqual(self.count("shop_jobs"), 0)

    def test_explicit_clear_fields_and_replay_after_lifecycle_are_not_publication(self):
        self.draft()
        self.store.edit(ACTOR, "7", expected_revision=1, request_id="clear-link", ebay_url=None)
        self.assertIn("ebay_url", missing_fields(self.store.get_review(ACTOR, "7")))
        self.store.hold(ACTOR, "7", expected_revision=2, request_id="held")
        self.assertEqual(self.draft().state, "held")
        self.assertEqual(self.store.edit(ACTOR, "7", expected_revision=1, request_id="clear-link", ebay_url=None).state, "held")
        self.assertEqual(self.count("shop_jobs"), 0)

    def test_clock_failure_rolls_back_partial_create(self):
        self.clock[0] = float("nan")
        self.assertCode("invalid_store_clock", lambda: self.draft())
        self.assertEqual(self.count("shop_items"), 0)
        self.assertEqual(self.count("shop_audit"), 0)

    def test_complete_database_reopens_with_wal_and_immutable_audit(self):
        self.draft()
        self.approve()
        self.store.close()
        self.store = self.open_store()
        self.assertEqual(self.store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.count("shop_audit"), 2)
        self.assertEqual(self.store.claim_next("sender-a").revision, 1)


if __name__ == "__main__":
    unittest.main()
