"""Approval-only website delivery. Importing this module performs no I/O.

Runtime MUST hold the same asyncio.Lock for the entire tick, source reconciliation
and shop interactions; use one process lock around both SQLite stores. The
resolver reads only owned intake content. The lifecycle resolver independently
reads current scoped source state, without loading photos. No Discord history is
observed or adopted here. The configured website source uses an isolated journal,
reserved new item IDs and FGNEW- SKUs; no existing website items are adopted.

Journal owns immutable request bytes, versions, backoff and website ACKs. Shop
leases own human approval. Their bridge is durable: an HTTP replay after a crash
uses identical bytes/version; a journal ACK preceding a crash is reconciled back
to the exact still-current approval without another HTTP call. Cancelled or
superseded uncertain requests are compensated by a higher-version withdrawal.
"""
import asyncio
import json

from publisher.transport import Delivery
from shop_content import ShopContentError, verify_job_content
from shop_values import ShopValidationError


class DeliveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class DeliveryService:
    def __init__(self, store, journal, transport, resolver, lifecycle_resolver, enabled=False, *, heartbeat=True, max_deliveries=25):
        if type(enabled) is not bool or type(heartbeat) is not bool:
            raise DeliveryError("invalid_delivery_enabled")
        if enabled and transport is None:
            raise DeliveryError("enabled_delivery_requires_transport")
        if type(max_deliveries) is not int or not 1 <= max_deliveries <= 25:
            raise DeliveryError("invalid_delivery_limit")
        if store.guild_id != journal.pins.guild_id or (transport is not None and transport.website != journal.website):
            raise DeliveryError("delivery_scope_mismatch")
        journal.bind_shop_reviews(store.shop_channel_id)
        self.store, self.journal, self.transport = store, journal, transport
        self.resolver, self.lifecycle_resolver = resolver, lifecycle_resolver
        self.enabled = enabled
        self.heartbeat_enabled = heartbeat
        self.max_deliveries = max_deliveries
        self._tick_lock = asyncio.Lock()

    async def _source_state(self, item_id):
        try:
            state = await self.lifecycle_resolver(item_id)
        except Exception:
            raise DeliveryError("source_lifecycle_unavailable") from None
        if not isinstance(state, str) or state not in {"listed", "sold", "shipped", "held", "withdrawn"}:
            raise DeliveryError("source_lifecycle_invalid")
        return state

    def _withdraw(self, item_id, state):
        review = self.store.invalidate_from_source(item_id, "sold" if state == "shipped" else state)
        if review.state == "sold" and state != "shipped":
            state = "sold"
        elif review.state == "withdrawn":
            state = "withdrawn"
        return self.journal.queue_review_withdrawal(item_id, state)

    async def _available(self, item_id):
        state = await self._source_state(item_id)
        review = self.store.system_get_review(item_id)
        if review is None:
            raise DeliveryError("review_identity_missing")
        if state != "listed":
            self._withdraw(item_id, state)
            return False
        if review.state in {"held", "sold", "withdrawn"}:
            self.journal.queue_review_withdrawal(item_id, review.state)
            return False
        return True

    async def _content(self, job):
        try:
            content = await self.resolver(job.product_ref)
            verify_job_content(job, content)
            return content
        except (ShopContentError, ShopValidationError, ValueError, OSError):
            # New approval and verified content are required after a mismatch.
            self._withdraw(job.item_id, "held")
            return None

    def _ack_shop(self, job):
        binding = self.journal.review_delivery(job.job_id)
        current = self.journal.get_item(job.item_id)
        if not binding or not current:
            return False
        if (current["revision"] != binding["journal_revision"]
                or current["ack_revision"] != binding["journal_revision"]
                or current["ack_hash"] != binding["body_hash"]):
            return False
        if not self.store.publication_guard(job.job_id, job.revision, job.content_digest, job.claim_token):
            return False
        self.store.acknowledge(job.job_id, revision=job.revision, content_digest=job.content_digest,
                               claim_token=job.claim_token,
                               delivery_receipt=f"website:{self.journal.source_id}:{job.item_id}:{binding['journal_revision']}")
        return True

    async def _post(self, body, *, suffix="sync"):
        # Do not release the runtime lock/process lifetime while a cancelled
        # to_thread HTTP request is still in flight. Its result stays uncertain
        # in the durable outbox and is reconciled/replayed on the next run.
        request = asyncio.create_task(asyncio.to_thread(self.transport.post, suffix, body))
        try:
            return await asyncio.shield(request)
        except asyncio.CancelledError:
            try:
                await request
            except Exception:
                pass
            raise
        except Exception:
            return Delivery(False, True, "website_unreachable_or_invalid_response")

    async def tick(self):
        """One bounded pass. Counts only; does not create a background scheduler."""
        if not self.enabled:
            return {"enabled": False, "sent": 0, "withdrawn": 0, "acknowledged": 0, **self.journal.counts()}
        if self.transport is None:
            raise DeliveryError("enabled_delivery_requires_transport")
        async with self._tick_lock:
            result = {"enabled": True, "sent": 0, "withdrawn": 0, "acknowledged": 0}
            reviews = self.store.delivery_reviews()
            # Obtain complete lifecycle evidence before considering any delivery.
            states = {review.item_id: await self._source_state(review.item_id) for review in reviews}
            for review in reviews:
                state = states[review.item_id]
                if state != "listed":
                    self._withdraw(review.item_id, state)
                elif review.state in {"sold", "held", "withdrawn"}:
                    self.journal.queue_review_withdrawal(review.item_id, review.state)

            # Restart recovery: every queued public body must still have its
            # original approval. A cancelled uncertain delivery must be hidden.
            for tracked in self.journal.tracked_messages():
                current = self.journal.get_item(tracked["item_id"])
                if not json.loads(current["body"])["listing"]:
                    continue
                binding = self.journal.review_delivery_for_version(current["item_id"], current["revision"])
                if not binding:
                    raise DeliveryError("review_delivery_binding_missing")
                job = self.store.delivery_job(binding["job_id"])
                if job.state == "cancelled":
                    # Do not cancel a NEWER approval while fencing an older one.
                    self.journal.queue_review_withdrawal(job.item_id, "withdrawn")

            for _ in range(25):
                job = self.store.claim_next("combined-website", lease_seconds=300)
                if job is None:
                    break
                if not await self._available(job.item_id):
                    continue
                content = await self._content(job)
                if content is None or not self.store.publication_guard(job.job_id, job.revision, job.content_digest, job.claim_token):
                    continue
                self.journal.queue_approved_review(job, content)
                result["acknowledged"] += int(self._ack_shop(job))

            # Fetch due rows only after reconciliation/claims may supersede them.
            for pending in self.journal.due(self.max_deliveries):
                if not self.journal.is_current(pending):
                    continue
                body = json.loads(pending["body"])
                job = None
                if body["listing"] is not None:
                    if not await self._available(pending["item_id"]):
                        continue
                    binding = self.journal.review_delivery_for_version(pending["item_id"], pending["revision"])
                    if not binding or binding["body_hash"] != pending["body_hash"]:
                        raise DeliveryError("review_delivery_binding_missing")
                    job = self.store.delivery_job(binding["job_id"])
                    if job.state != "claimed" or not self.store.publication_guard(job.job_id, job.revision, job.content_digest, job.claim_token):
                        continue
                    if await self._content(job) is None:
                        continue
                    # Fresh lifecycle after potentially expensive photo validation.
                    if not await self._available(job.item_id):
                        continue
                    if not self.store.publication_guard(job.job_id, job.revision, job.content_digest, job.claim_token):
                        continue
                else:
                    # Withdrawal never depends on photo availability, but source
                    # permissions/unavailability still must not be mistaken for sold.
                    await self._source_state(pending["item_id"])
                if not self.journal.is_current(pending):
                    continue
                delivery = await self._post(pending["body"])
                if not delivery.ok:
                    self.journal.fail(pending, delivery.code, delivery.retryable)
                    continue
                value = delivery.value or {}
                if value.get("outcome") not in {"applied", "replayed"} or type(value.get("version")) is not int or value["version"] != pending["revision"]:
                    self.journal.fail(pending, "receiver_revision_conflict", False)
                    continue
                if not self.journal.is_current(pending):
                    raise DeliveryError("delivery_changed_during_http")
                self.journal.acknowledge(pending, receiver_version=value["version"])
                result["sent"] += 1
                if job is None:
                    result["withdrawn"] += 1
                elif await self._available(job.item_id):
                    if self._ack_shop(job):
                        result["acknowledged"] += 1
                    else:
                        # Local mutation during HTTP cannot acknowledge a stale
                        # approval, even if the website accepted the old request.
                        self.journal.queue_review_withdrawal(job.item_id, "withdrawn")
            result.update(self.journal.counts())
            result["review_blocked"] = sum(review.state == "held" for review in self.store.delivery_reviews())
            if self.heartbeat_enabled:
                heartbeat = await self._post(self.journal.heartbeat(), suffix="heartbeat")
                result["heartbeat_ok"] = heartbeat.ok
                if not heartbeat.ok:
                    # Retried on the next enabled tick. No false healthy result;
                    # no arbitrary server text is retained in logs/status.
                    result["failed"] += 1
                    result["heartbeat_error"] = "heartbeat_unavailable"
            return result
