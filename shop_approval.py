"""Offline durable #website_shop review core. No Discord, HTTP or live-profile access.

Public content is intentionally NOT stored or inferred here. ``product_ref`` is
an opaque stable identifier, never a path; ``content_digest`` is SHA256 of a
separately validated approved-text/condition/photo manifest. Before approve, the
adapter must resolve that manifest, validate all public fields/photo provenance,
and pass its verified digest as ``validated_content_digest``. Before EVERY send,
resolve it again, compare the frozen job digest, then call publication_guard.

Approval means queued, never published. Claim leases/tokens prevent stale workers
from acknowledging a replaced/held/sold job. The sender must use stable item and
revision idempotency, and serialize lifecycle handling with dispatch / enforce a
receiver-side version fence: a local guard cannot make a later HTTP call atomic.
Sold/withdrawn/held only invalidate publication here; the adapter must separately
deliver the matching website withdrawal before calling that workflow complete.

No existing products are adopted. Human calls always require exact guild/channel
and allowlisted actor IDs. Worker methods are internal capabilities, not Discord
commands; the adapter also owns real user/bot/role/application checks. Reusing a
request ID with identical input has no new effect and returns the current review
or existing job (which can since have been cancelled). Reusing it differently
fails. Audit, revision and request records are immutable.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from shop_values import MAX_INTEGER, ShopValidationError, canonical_ebay_url, ebay_item_id, validate_quantity

SCHEMA = "four-guys-shop-review-v1"
APPLICATION_ID = 0x34475348
UNSET = object()
STATES = frozenset({"draft", "approved", "published", "held", "sold", "withdrawn"})
TABLES = frozenset({"shop_meta", "shop_items", "shop_revisions", "shop_jobs", "shop_requests", "shop_audit"})


class ShopApprovalError(ValueError):
    def __init__(self, code, *, missing_fields=()):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,100}", code) else "shop_approval_error"
        self.missing_fields = tuple(missing_fields)
        super().__init__(self.code)


def fail(code, **kwargs):
    raise ShopApprovalError(code, **kwargs)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _safe_path(value):
    path = Path(os.path.abspath(str(value)))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            fail("unsafe_store_path")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            fail("unsafe_store_path")
    return path


def _snowflake(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{16,19}", value) or int(value) >= 2**64:
        fail("invalid_scope_identity")
    return value


def _opaque(value, maximum=128, *, punctuation="_-", code="invalid_opaque_identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9" + re.escape(punctuation) + r"]{0," + str(maximum - 1) + r"}", value):
        fail(code)
    return value


def _item_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > MAX_INTEGER:
        fail("invalid_item_id")
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        fail("invalid_revision")
    return value


def _digest(value):
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
        fail("invalid_content_digest")
    return value


def _price(value):
    if value is not None and (type(value) is not int or not 1 <= value <= MAX_INTEGER):
        fail("invalid_price")
    return value


def _values(price, url, quantity, digest):
    try:
        canonical_url = canonical_ebay_url(url) if url is not None else None
        return {"price_cents": _price(price), "ebay_url": canonical_url,
                "ebay_item_id": ebay_item_id(canonical_url) if canonical_url is not None else None,
                "quantity": validate_quantity(quantity) if quantity is not None else None,
                "content_digest": _digest(digest)}
    except ShopValidationError as error:
        fail(error.code)


@dataclass(frozen=True)
class Actor:
    guild_id: str
    channel_id: str
    user_id: str


@dataclass(frozen=True)
class _SourceActor:
    guild_id: str
    channel_id: str
    user_id: str = "system:intake"


@dataclass(frozen=True)
class Review:
    item_id: str
    sku: str
    product_ref: str
    content_digest: str | None
    revision: int
    state: str
    price_cents: int | None
    ebay_url: str | None
    ebay_item_id: str | None
    quantity: int | None
    published_revision: int | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class ApprovalJob:
    job_id: str
    item_id: str
    revision: int
    sku: str
    product_ref: str
    content_digest: str
    price_cents: int
    ebay_url: str
    ebay_item_id: str
    quantity: int
    state: str
    claim_token: str | None
    worker_id: str | None
    lease_until: float | None
    delivery_receipt: str | None


def missing_fields(review: Review):
    return tuple(key for key in ("price_cents", "ebay_url", "quantity", "content_digest") if getattr(review, key) is None)


DDL = """
CREATE TABLE shop_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE shop_items (
  item_id TEXT PRIMARY KEY, sku TEXT NOT NULL UNIQUE, product_ref TEXT NOT NULL UNIQUE,
  revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision BETWEEN 1 AND 2147483647),
  state TEXT NOT NULL CHECK(state IN ('draft','approved','published','held','sold','withdrawn')),
  published_revision INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  FOREIGN KEY(item_id,revision) REFERENCES shop_revisions(item_id,revision) DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(item_id,published_revision) REFERENCES shop_revisions(item_id,revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE shop_revisions (
  item_id TEXT NOT NULL REFERENCES shop_items(item_id), revision INTEGER NOT NULL,
  content_digest TEXT, price_cents INTEGER, ebay_url TEXT, ebay_item_id TEXT, quantity INTEGER,
  created_at REAL NOT NULL, PRIMARY KEY(item_id,revision),
  CHECK(price_cents IS NULL OR (typeof(price_cents)='integer' AND price_cents BETWEEN 1 AND 2147483647)),
  CHECK(quantity IS NULL OR (typeof(quantity)='integer' AND quantity BETWEEN 1 AND 2147483647)),
  CHECK((ebay_url IS NULL)=(ebay_item_id IS NULL))
);
CREATE TABLE shop_jobs (
  job_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, revision INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','claimed','published','cancelled')),
  claim_token TEXT, worker_id TEXT, lease_until REAL, delivery_receipt TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(item_id,revision),
  FOREIGN KEY(item_id,revision) REFERENCES shop_revisions(item_id,revision)
);
CREATE TABLE shop_requests (
  request_id TEXT PRIMARY KEY, action TEXT NOT NULL, input_hash TEXT NOT NULL,
  result_kind TEXT NOT NULL CHECK(result_kind IN ('review','job')), result_id TEXT NOT NULL
);
CREATE TABLE shop_audit (
  audit_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, actor_id TEXT NOT NULL,
  action TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES shop_items(item_id), revision INTEGER NOT NULL,
  created_at REAL NOT NULL, details_json TEXT NOT NULL
);
CREATE TRIGGER shop_items_identity BEFORE UPDATE ON shop_items
WHEN NEW.item_id != OLD.item_id OR NEW.sku != OLD.sku OR NEW.product_ref != OLD.product_ref
  OR NEW.revision < OLD.revision
  OR (OLD.state IN ('sold','withdrawn') AND (NEW.state != OLD.state OR NEW.revision != OLD.revision))
BEGIN SELECT RAISE(ABORT,'shop identity or terminal state invariant'); END;
CREATE TRIGGER shop_jobs_identity BEFORE UPDATE ON shop_jobs
WHEN NEW.job_id != OLD.job_id OR NEW.item_id != OLD.item_id OR NEW.revision != OLD.revision
  OR OLD.state='published'
BEGIN SELECT RAISE(ABORT,'shop job identity or published snapshot invariant'); END;
"""
for _table in ("shop_meta", "shop_revisions", "shop_requests", "shop_audit"):
    for _action in ("UPDATE", "DELETE"):
        DDL += f"CREATE TRIGGER {_table}_{_action.lower()}_immutable BEFORE {_action} ON {_table} BEGIN SELECT RAISE(ABORT,'immutable shop history'); END;\n"
DDL += "CREATE TRIGGER shop_jobs_delete_immutable BEFORE DELETE ON shop_jobs BEGIN SELECT RAISE(ABORT,'immutable shop job history'); END;\n"
DDL += "CREATE TRIGGER shop_items_delete_immutable BEFORE DELETE ON shop_items BEGIN SELECT RAISE(ABORT,'immutable shop identity'); END;\n"
TRIGGERS = frozenset(re.findall(r"CREATE TRIGGER ([a-z_]+)", DDL))


class ShopReviewStore:
    def __init__(self, path, *, guild_id, shop_channel_id, operator_ids, now=time.time):
        self.guild_id, self.shop_channel_id = _snowflake(guild_id), _snowflake(shop_channel_id)
        if self.guild_id == self.shop_channel_id or not isinstance(operator_ids, (tuple, list, set, frozenset)) or not 1 <= len(operator_ids) <= 100:
            fail("invalid_operator_scope")
        self.operator_ids = frozenset(_snowflake(value) for value in operator_ids)
        if len(self.operator_ids) != len(operator_ids):
            fail("invalid_operator_scope")
        self.now = now
        self.path = _safe_path(path)
        for suffix in ("-wal", "-shm", "-journal"):
            _safe_path(str(self.path) + suffix)
        self.scope = _canonical({"guild_id": self.guild_id, "shop_channel_id": self.shop_channel_id,
                                 "operator_ids": sorted(self.operator_ids)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            created = True
        except FileExistsError:
            self._inspect_existing()
        self.connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA journal_mode=WAL")
            if created:
                self.connection.executescript("BEGIN IMMEDIATE;" + DDL + f"PRAGMA application_id={APPLICATION_ID}; PRAGMA user_version=1;" +
                    "INSERT INTO shop_meta VALUES ('schema','" + SCHEMA + "');" +
                    "INSERT INTO shop_meta VALUES ('scope','" + self.scope.replace("'", "''") + "'); COMMIT;")
                self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except BaseException:
            self.connection.close()
            raise

    def _inspect_existing(self):
        if not self.path.is_file() or self.path.stat().st_size == 0:
            fail("not_a_shop_review_store")
        check = None
        try:
            # Metadata/schema are checkpointed at creation and never changed.
            # Immutable read-only inspection never adopts or modifies a foreign DB.
            check = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
            check.execute("PRAGMA query_only=ON")
            check.execute("PRAGMA trusted_schema=OFF")
            if check.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or check.execute("PRAGMA user_version").fetchone()[0] != 1:
                fail("not_a_shop_review_store")
            tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            triggers = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            if tables != TABLES or triggers != TRIGGERS:
                fail("shop_store_schema_mismatch")
            meta = dict(check.execute("SELECT key,value FROM shop_meta"))
            if meta != {"schema": SCHEMA, "scope": self.scope}:
                fail("shop_store_scope_mismatch")
        except sqlite3.Error:
            fail("not_a_shop_review_store")
        finally:
            if check is not None:
                check.close()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException as error:
            self.connection.execute("ROLLBACK")
            if isinstance(error, sqlite3.IntegrityError):
                fail("shop_store_integrity_conflict")
            raise

    def _time(self):
        value = self.now()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            fail("invalid_store_clock")
        return float(value)

    def _actor(self, actor):
        if not isinstance(actor, Actor) or actor.guild_id != self.guild_id or actor.channel_id != self.shop_channel_id:
            fail("shop_scope_mismatch")
        if not isinstance(actor.user_id, str) or actor.user_id not in self.operator_ids:
            fail("shop_operator_not_authorized")

    def _review(self, item_id):
        row = self.connection.execute("""SELECT i.item_id,i.sku,i.product_ref,r.content_digest,i.revision,i.state,
            r.price_cents,r.ebay_url,r.ebay_item_id,r.quantity,i.published_revision,i.created_at,i.updated_at
            FROM shop_items i JOIN shop_revisions r ON r.item_id=i.item_id AND r.revision=i.revision WHERE i.item_id=?""", (item_id,)).fetchone()
        if row is None:
            fail("shop_item_not_found")
        return Review(**dict(row))

    def get_review(self, actor, item_id):
        self._actor(actor)
        return self._review(_item_id(item_id))

    def system_get_review(self, item_id):
        """Internal pinned-intake capability; never exposed as a user command."""
        item_id = _item_id(item_id)
        if not self.connection.execute("SELECT 1 FROM shop_items WHERE item_id=?", (item_id,)).fetchone():
            return None
        return self._review(item_id)

    def delivery_reviews(self):
        return [self._review(row[0]) for row in self.connection.execute("SELECT item_id FROM shop_items ORDER BY item_id")]

    def delivery_job(self, job_id):
        return self._job(_opaque(job_id))

    def _source_actor(self):
        return _SourceActor(self.guild_id, self.shop_channel_id)

    def _job(self, job_id):
        row = self.connection.execute("""SELECT j.job_id,j.item_id,j.revision,i.sku,i.product_ref,r.content_digest,
            r.price_cents,r.ebay_url,r.ebay_item_id,r.quantity,j.state,j.claim_token,j.worker_id,j.lease_until,j.delivery_receipt
            FROM shop_jobs j JOIN shop_items i ON i.item_id=j.item_id
            JOIN shop_revisions r ON r.item_id=j.item_id AND r.revision=j.revision WHERE j.job_id=?""", (job_id,)).fetchone()
        if row is None:
            fail("shop_job_not_found")
        return ApprovalJob(**dict(row))

    def _request(self, actor, request_id, action, data):
        _opaque(request_id, punctuation="_.:-", code="invalid_request_id")
        payload = {"actor": asdict(actor), "action": action, **data}
        digest = hashlib.sha256(_canonical(payload).encode()).hexdigest()
        row = self.connection.execute("SELECT * FROM shop_requests WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["input_hash"] != digest:
                fail("shop_request_id_conflict")
            return digest, self._review(row["result_id"]) if row["result_kind"] == "review" else self._job(row["result_id"])
        return digest, None

    def _audit(self, operation_key, actor_id, action, item_id, revision, details=None):
        self.connection.execute("INSERT INTO shop_audit VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, operation_key, actor_id, action, item_id, revision, self._time(), _canonical(details or {})))

    def _record_request(self, actor, request_id, digest, action, item_id, revision, *, job_id=None):
        self.connection.execute("INSERT INTO shop_requests VALUES (?,?,?,?,?)",
            (request_id, action, digest, "job" if job_id else "review", job_id or item_id))
        self._audit("request:" + request_id, actor.user_id, action, item_id, revision)

    def _append_revision(self, item_id, revision, values, now):
        self.connection.execute("INSERT INTO shop_revisions VALUES (?,?,?,?,?,?,?,?)",
            (item_id, revision, values["content_digest"], values["price_cents"], values["ebay_url"], values["ebay_item_id"], values["quantity"], now))

    def _expected(self, item_id, expected_revision):
        review = self._review(item_id)
        if review.revision != _revision(expected_revision):
            fail("shop_revision_conflict")
        return review

    def _cancel_pending(self, item_id, now):
        self.connection.execute("UPDATE shop_jobs SET state='cancelled',updated_at=? WHERE item_id=? AND state IN ('queued','claimed')", (now, item_id))

    def create_draft(self, actor, *, item_id, sku, product_ref, content_digest=None, price_cents=None, ebay_url=None, quantity=None, request_id):
        self._actor(actor)
        return self._create_draft(actor, item_id=item_id, sku=sku, product_ref=product_ref, content_digest=content_digest,
                                  price_cents=price_cents, ebay_url=ebay_url, quantity=quantity, request_id=request_id)

    def system_create_draft(self, *, item_id, sku, product_ref, content_digest=None, quantity=None, request_id):
        """Stage source content only. A human must supply price/link and approve."""
        return self._create_draft(self._source_actor(), item_id=item_id, sku=sku, product_ref=product_ref,
                                  content_digest=content_digest, quantity=quantity, request_id=request_id)

    def _create_draft(self, actor, *, item_id, sku, product_ref, content_digest=None, price_cents=None, ebay_url=None, quantity=None, request_id):
        item_id = _item_id(item_id)
        sku = _opaque(sku, 100, punctuation="_.-", code="invalid_sku")
        product_ref = _opaque(product_ref, code="invalid_product_ref")
        values = _values(price_cents, ebay_url, quantity, content_digest)
        data = {"item_id": item_id, "sku": sku, "product_ref": product_ref, **values}
        with self._transaction():
            digest, replay = self._request(actor, request_id, "create", data)
            if replay is not None:
                return replay
            if self.connection.execute("SELECT 1 FROM shop_items WHERE item_id=? OR sku=? OR product_ref=?", (item_id, sku, product_ref)).fetchone():
                fail("shop_item_identity_conflict")
            now = self._time()
            self.connection.execute("INSERT INTO shop_items VALUES (?,?,?,1,'draft',NULL,?,?)", (item_id, sku, product_ref, now, now))
            self._append_revision(item_id, 1, values, now)
            self._record_request(actor, request_id, digest, "create", item_id, 1)
            return self._review(item_id)

    def edit(self, actor, item_id, *, expected_revision, request_id, price_cents=UNSET, ebay_url=UNSET, quantity=UNSET, content_digest=UNSET):
        self._actor(actor)
        return self._edit(actor, item_id, expected_revision=expected_revision, request_id=request_id,
                          price_cents=price_cents, ebay_url=ebay_url, quantity=quantity, content_digest=content_digest)

    def system_refresh(self, item_id, *, expected_revision, request_id, content_digest, quantity):
        """Refresh source fields, never approve or alter human price/link."""
        return self._edit(self._source_actor(), item_id, expected_revision=expected_revision, request_id=request_id,
                          content_digest=content_digest, quantity=quantity)

    def _edit(self, actor, item_id, *, expected_revision, request_id, price_cents=UNSET, ebay_url=UNSET, quantity=UNSET, content_digest=UNSET):
        item_id, expected_revision = _item_id(item_id), _revision(expected_revision)
        supplied = {key: value for key, value in {"price_cents": price_cents, "ebay_url": ebay_url, "quantity": quantity, "content_digest": content_digest}.items() if value is not UNSET}
        if not supplied:
            fail("shop_edit_requires_changes")
        validated = _values(supplied.get("price_cents"), supplied.get("ebay_url"), supplied.get("quantity"), supplied.get("content_digest"))
        supplied = {key: validated[key] for key in supplied}
        with self._transaction():
            digest, replay = self._request(actor, request_id, "edit", {"item_id": item_id, "expected_revision": expected_revision, "changes": supplied})
            if replay is not None:
                return replay
            old = self._expected(item_id, expected_revision)
            if old.state in {"held", "sold", "withdrawn"}:
                fail("shop_item_not_editable")
            values = _values(supplied.get("price_cents", old.price_cents), supplied.get("ebay_url", old.ebay_url),
                             supplied.get("quantity", old.quantity), supplied.get("content_digest", old.content_digest))
            revision, now = _revision(old.revision + 1), self._time()
            self._append_revision(item_id, revision, values, now)
            self._cancel_pending(item_id, now)
            self.connection.execute("UPDATE shop_items SET revision=?,state='draft',updated_at=? WHERE item_id=?", (revision, now, item_id))
            self._record_request(actor, request_id, digest, "edit", item_id, revision)
            return self._review(item_id)

    def approve(self, actor, item_id, *, expected_revision, request_id, validated_content_digest):
        self._actor(actor)
        item_id, expected_revision = _item_id(item_id), _revision(expected_revision)
        _digest(validated_content_digest)
        with self._transaction():
            digest, replay = self._request(actor, request_id, "approve", {"item_id": item_id, "expected_revision": expected_revision, "validated_content_digest": validated_content_digest})
            if replay is not None:
                return replay
            review = self._expected(item_id, expected_revision)
            if review.state in {"held", "sold", "withdrawn"}:
                fail("shop_item_not_approvable")
            missing = missing_fields(review)
            if missing:
                fail("shop_approval_fields_missing", missing_fields=missing)
            if _digest(validated_content_digest) != review.content_digest:
                fail("shop_content_digest_mismatch")
            existing = self.connection.execute("SELECT job_id FROM shop_jobs WHERE item_id=? AND revision=?", (item_id, review.revision)).fetchone()
            if existing:
                job = self._job(existing[0])
                if job.state == "cancelled":
                    fail("shop_approval_was_cancelled")
            else:
                now, job_id = self._time(), uuid.uuid4().hex
                self.connection.execute("INSERT INTO shop_jobs VALUES (?,?,?,'queued',NULL,NULL,NULL,NULL,?,?)", (job_id, item_id, review.revision, now, now))
                self.connection.execute("UPDATE shop_items SET state='approved',updated_at=? WHERE item_id=?", (now, item_id))
                job = self._job(job_id)
            self._record_request(actor, request_id, digest, "approve", item_id, review.revision, job_id=job.job_id)
            return job

    def _lifecycle(self, actor, item_id, *, expected_revision, request_id, target):
        self._actor(actor)
        return self._lifecycle_apply(actor, item_id, expected_revision=expected_revision, request_id=request_id, target=target)

    def _lifecycle_apply(self, actor, item_id, *, expected_revision, request_id, target):
        item_id, expected_revision = _item_id(item_id), _revision(expected_revision)
        with self._transaction():
            digest, replay = self._request(actor, request_id, target, {"item_id": item_id, "expected_revision": expected_revision})
            if replay is not None:
                return replay
            old = self._expected(item_id, expected_revision)
            if old.state in {"sold", "withdrawn"}:
                fail("shop_terminal_item_requires_new_flow")
            if target == "release_hold" and old.state != "held":
                fail("shop_item_is_not_held")
            revision, now = _revision(old.revision + 1), self._time()
            values = _values(old.price_cents, old.ebay_url, old.quantity, None if target == "release_hold" else old.content_digest)
            self._append_revision(item_id, revision, values, now)
            self._cancel_pending(item_id, now)
            state = "draft" if target == "release_hold" else target
            self.connection.execute("UPDATE shop_items SET revision=?,state=?,updated_at=? WHERE item_id=?", (revision, state, now, item_id))
            self._record_request(actor, request_id, digest, target, item_id, revision)
            return self._review(item_id)

    def mark_sold(self, actor, item_id, *, expected_revision, request_id):
        return self._lifecycle(actor, item_id, expected_revision=expected_revision, request_id=request_id, target="sold")

    def withdraw(self, actor, item_id, *, expected_revision, request_id):
        return self._lifecycle(actor, item_id, expected_revision=expected_revision, request_id=request_id, target="withdrawn")

    def hold(self, actor, item_id, *, expected_revision, request_id):
        return self._lifecycle(actor, item_id, expected_revision=expected_revision, request_id=request_id, target="held")

    def release_hold(self, actor, item_id, *, expected_revision, request_id):
        return self._lifecycle(actor, item_id, expected_revision=expected_revision, request_id=request_id, target="release_hold")

    def system_release_hold(self, item_id, *, expected_revision, request_id):
        return self._lifecycle_apply(self._source_actor(), item_id, expected_revision=expected_revision,
                                     request_id=request_id, target="release_hold")

    def invalidate_from_source(self, item_id, state):
        """Trusted scoped lifecycle input. Never reactivates a terminal item."""
        if state not in {"held", "sold", "withdrawn"}:
            fail("invalid_source_state")
        item_id = _item_id(item_id)
        old = self._review(item_id)
        if old.state == state or old.state in {"sold", "withdrawn"}:
            return old
        request_id = f"system-source:{item_id}:{old.revision}:{state}"
        return self._lifecycle_apply(self._source_actor(), item_id, expected_revision=old.revision,
                                     request_id=request_id, target=state)

    def claim_next(self, worker_id, *, lease_seconds=30):
        """Internal sender API; claims only the current approved revision."""
        worker_id = _opaque(worker_id, code="invalid_worker_id")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 300:
            fail("invalid_claim_lease")
        with self._transaction():
            now = self._time()
            row = self.connection.execute("""SELECT j.job_id FROM shop_jobs j JOIN shop_items i ON i.item_id=j.item_id
                WHERE j.revision=i.revision AND i.state='approved'
                AND (j.state='queued' OR (j.state='claimed' AND j.lease_until<=?)) ORDER BY j.created_at,j.job_id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.connection.execute("UPDATE shop_jobs SET state='claimed',claim_token=?,worker_id=?,lease_until=?,updated_at=? WHERE job_id=?", (token, worker_id, now + lease_seconds, now, row[0]))
            job = self._job(row[0])
            self._audit("claim:" + token, worker_id, "claim", job.item_id, job.revision)
            return job

    def publication_guard(self, job_id, revision, content_digest, claim_token):
        """Internal pre-send check, not an atomic promise about later HTTP I/O."""
        _opaque(job_id)
        _revision(revision)
        _digest(content_digest)
        _opaque(claim_token)
        job = self._job(job_id)
        review = self._review(job.item_id)
        return (job.state == "claimed" and job.revision == revision == review.revision
                and review.state == "approved" and job.content_digest == content_digest
                and job.claim_token == claim_token and job.lease_until is not None and job.lease_until > self._time())

    def acknowledge(self, job_id, *, revision, content_digest, claim_token, delivery_receipt):
        """Acknowledge only the still-current claimed revision after real delivery."""
        _opaque(job_id)
        _revision(revision)
        _digest(content_digest)
        _opaque(claim_token)
        receipt = _opaque(delivery_receipt, 200, punctuation="_.:-", code="invalid_delivery_receipt")
        with self._transaction():
            job = self._job(job_id)
            if job.state == "published":
                if (job.revision, job.content_digest, job.claim_token, job.delivery_receipt) != (revision, content_digest, claim_token, receipt):
                    fail("shop_acknowledgement_conflict")
                return self._review(job.item_id)
            if not self.publication_guard(job_id, revision, content_digest, claim_token):
                fail("shop_publication_is_stale")
            now = self._time()
            self.connection.execute("UPDATE shop_jobs SET state='published',delivery_receipt=?,updated_at=? WHERE job_id=?", (receipt, now, job_id))
            self.connection.execute("UPDATE shop_items SET state='published',published_revision=?,updated_at=? WHERE item_id=?", (revision, now, job.item_id))
            self._audit("ack:" + job_id, job.worker_id, "published", job.item_id, revision, {"receipt": receipt})
            return self._review(job.item_id)
