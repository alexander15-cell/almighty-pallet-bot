"""Publisher-owned durable ordering, outbox and fail-closed withdrawals.

Callers serialize Gateway/reconciliation work, and hold one process lock for this
journal. A confirmed Discord 404 is distinct from permission/network failures.
"""
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from .contract import Card, ContractError, MAX_BODY, MAX_INTEGER, SOURCE_ID, TrustPins, canonical, make_snapshot, snowflake
from .transport import validate_website

SCHEMA = "4g1p-website-publisher-1"


def _card_observation_values(card):
    """Keep pre-extension hashes stable when optional listing fields are absent."""
    values = asdict(card)
    for key in ("quantity", "ebay_item_id"):
        if values.get(key) is None:
            values.pop(key, None)
    return values


def _safe_path(value):
    path = Path(os.path.abspath(str(value)))
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ContractError("unsafe_journal_path")
    return path


class Journal:
    def __init__(self, path, source_id, guild_id, website, intake_bot_id,
                 listed_channel_id, sold_channel_id, *, now=time.time, allow_loopback=False):
        if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
            raise ContractError("invalid_source_identity")
        self.pins = TrustPins(guild_id, intake_bot_id, listed_channel_id, sold_channel_id)
        self.source_id = source_id
        self.website = validate_website(website, allow_loopback)
        self.now = now
        path = _safe_path(path)
        for suffix in ("-wal", "-shm", "-journal"):
            _safe_path(str(path) + suffix)
        # Inspect before writing: never turn an intake/site/unrelated DB into ours.
        if path.exists() and path.stat().st_size:
            check = None
            try:
                check = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
                check.execute("PRAGMA query_only=ON")
                check.execute("PRAGMA trusted_schema=OFF")
                row = check.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
                if not row or row[0] != SCHEMA:
                    raise ContractError("not_a_publisher_journal")
            except sqlite3.Error:
                raise ContractError("not_a_publisher_journal") from None
            finally:
                if check is not None:
                    check.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=3)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items(
                    item_id TEXT PRIMARY KEY,sku TEXT NOT NULL UNIQUE,message_id TEXT NOT NULL UNIQUE,
                    channel_id TEXT NOT NULL,edited_at TEXT NOT NULL,revision INTEGER NOT NULL,
                    observation_hash TEXT NOT NULL,body_hash TEXT NOT NULL,body BLOB NOT NULL,
                    ack_revision INTEGER,ack_hash TEXT,last_seen REAL NOT NULL,
                    missing_since REAL,diagnostic TEXT);
                CREATE TABLE IF NOT EXISTS pending(
                    item_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,body BLOB NOT NULL,
                    body_hash TEXT NOT NULL,priority INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0,blocked INTEGER NOT NULL DEFAULT 0,error TEXT);
                CREATE TABLE IF NOT EXISTS quarantine(
                    item_id TEXT PRIMARY KEY,message_id TEXT NOT NULL,channel_id TEXT NOT NULL,
                    edited_at TEXT NOT NULL,diagnostic TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS photo_waiting(
                    item_id TEXT PRIMARY KEY,message_id TEXT NOT NULL,edited_at TEXT NOT NULL,
                    contract_hash TEXT NOT NULL);
            """)
            pins = {"schema": SCHEMA, "source_id": source_id, "website": self.website, **asdict(self.pins)}
            with self.connection:
                for key, value in pins.items():
                    existing = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                    if existing and existing[0] != value:
                        raise ContractError("journal_identity_or_destination_mismatch")
                    self.connection.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (key, value))
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def bind_shop_reviews(self, shop_channel_id):
        """One-way binding for a NEW review-only journal, never legacy adoption.

        items.message_id contains an explicitly local `shop-review:<item>` marker
        in this mode, not a purported Discord snowflake. Receiver identities and
        versions remain the original Journal source/item/revision contract.
        """
        channel = snowflake(shop_channel_id)
        mode = self.connection.execute("SELECT value FROM metadata WHERE key='delivery_mode'").fetchone()
        bound = self.connection.execute("SELECT value FROM metadata WHERE key='shop_channel_id'").fetchone()
        if mode and (mode[0] != "shop-review-v1" or not bound or bound[0] != channel):
            raise ContractError("journal_review_binding_mismatch")
        if not mode and (bound or self.connection.execute("SELECT 1 FROM items LIMIT 1").fetchone()
                         or self.connection.execute("SELECT 1 FROM pending LIMIT 1").fetchone()
                         or self.connection.execute("SELECT 1 FROM quarantine LIMIT 1").fetchone()):
            raise ContractError("journal_review_requires_fresh_namespace")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES('delivery_mode','shop-review-v1')")
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES('shop_channel_id',?)", (channel,))
            self.connection.execute("""CREATE TABLE IF NOT EXISTS shop_deliveries(
                job_id TEXT PRIMARY KEY,item_id TEXT NOT NULL,review_revision INTEGER NOT NULL,
                content_digest TEXT NOT NULL,journal_revision INTEGER NOT NULL,body_hash TEXT NOT NULL,
                UNIQUE(item_id,journal_revision))""")
        self.shop_channel_id = channel

    def review_delivery(self, job_id):
        row = self.connection.execute("SELECT * FROM shop_deliveries WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def review_delivery_for_version(self, item_id, version):
        row = self.connection.execute("SELECT * FROM shop_deliveries WHERE item_id=? AND journal_revision=?",
                                      (item_id, version)).fetchone()
        return dict(row) if row else None

    def _review_snapshot(self, item_id, sku, status, on_hold, listing, *, job=None):
        if not getattr(self, "shop_channel_id", None):
            raise ContractError("journal_review_not_bound")
        old = self.get_item(item_id)
        marker = "shop-review:" + item_id
        if old and (old["sku"] != sku or old["message_id"] != marker):
            raise ContractError("item_identity_changed")
        owner = self.connection.execute("SELECT item_id FROM items WHERE sku=?", (sku,)).fetchone()
        if owner and owner[0] != item_id:
            raise ContractError("item_identity_collision")
        if old and listing is None:
            previous = json.loads(old["body"])
            if previous["listing"] is None and previous["status"] == status and previous["onHold"] == on_hold:
                return "duplicate"
        version = old["revision"] + 1 if old else 1
        if version > MAX_INTEGER:
            raise ContractError("publisher_revision_limit_exceeded")
        body = canonical({"schemaVersion": 1, "sourceId": self.source_id, "guildId": self.pins.guild_id,
                          "itemId": item_id, "version": version, "sku": sku, "status": status,
                          "onHold": on_hold, "issue": None, "listing": listing})
        if len(body) > MAX_BODY:
            raise ContractError("snapshot_too_large")
        with self.connection:
            body_hash = self._queue(item_id, version, body)
            self.connection.execute("""INSERT INTO items(item_id,sku,message_id,channel_id,edited_at,revision,
                observation_hash,body_hash,body,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET edited_at=excluded.edited_at,revision=excluded.revision,
                observation_hash=excluded.observation_hash,body_hash=excluded.body_hash,body=excluded.body,
                last_seen=excluded.last_seen,missing_since=NULL,diagnostic=NULL""",
                (item_id, sku, marker, self.shop_channel_id, str(version), version, body_hash, body_hash, body, self.now()))
            if job is not None:
                self.connection.execute("INSERT INTO shop_deliveries VALUES(?,?,?,?,?,?)",
                    (job.job_id, item_id, job.revision, job.content_digest, version, body_hash))
        return "queued"

    def queue_approved_review(self, job, content):
        """Internal: caller verifies frozen job/content and current lease first."""
        previous = self.review_delivery(job.job_id)
        if previous:
            if (previous["item_id"], previous["review_revision"], previous["content_digest"]) != (job.item_id, job.revision, job.content_digest):
                raise ContractError("review_delivery_identity_changed")
            return "duplicate"
        listing = {"title": content["title"], "description": content["description"], "condition": content["condition"],
                   "conditionNotes": content["condition_notes"], "photos": content["photos"],
                   "priceCents": job.price_cents, "quantity": job.quantity, "ebayItemId": job.ebay_item_id}
        return self._review_snapshot(job.item_id, job.sku, "listed", False, listing, job=job)

    def queue_review_withdrawal(self, item_id, state):
        if state not in {"sold", "shipped", "held", "withdrawn"}:
            raise ContractError("invalid_review_lifecycle")
        old = self.get_item(item_id)
        if not old:
            return "untracked"
        previous = json.loads(old["body"])
        # Terminal website states must never be softened back to held/deleted.
        if previous["status"] == "shipped" or (previous["status"] == "sold" and state != "shipped"):
            state = previous["status"]
        status = "listed" if state == "held" else "deleted" if state == "withdrawn" else state
        return self._review_snapshot(item_id, old["sku"], status, state == "held", None)

    def get_item(self, item_id):
        row = self.connection.execute("SELECT * FROM items WHERE item_id=?", (str(item_id),)).fetchone()
        return dict(row) if row else None

    def get_by_message(self, message_id):
        row = self.connection.execute("SELECT * FROM items WHERE message_id=?", (str(message_id),)).fetchone()
        return dict(row) if row else None

    def tracked_messages(self):
        """Only identifiers for restricted reconciliation, not public photo blobs."""
        return [dict(row) for row in self.connection.execute(
            "SELECT item_id,message_id,channel_id,edited_at,missing_since FROM items ORDER BY item_id")]

    def _queue(self, item_id, version, body):
        value = json.loads(body)
        priority = 0 if value["status"] in {"sold", "shipped"} else 1 if value["listing"] is None else 2
        body_hash = hashlib.sha256(body).hexdigest()
        self.connection.execute("""INSERT INTO pending(item_id,revision,body,body_hash,priority)
            VALUES(?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision,
            body=excluded.body,body_hash=excluded.body_hash,priority=excluded.priority,
            attempts=0,due=0,blocked=0,error=NULL""", (item_id, version, body, body_hash, priority))
        return body_hash

    def observe(self, card: Card, photos=None, *, photo_pending=False) -> str:
        """Queue one current trusted snapshot. Return queued/duplicate/stale.

        parse_message must run first. No downloading occurs here. All eligible
        photos must already be normalized, in card attachment order.
        """
        if self.connection.execute("SELECT 1 FROM metadata WHERE key='delivery_mode'").fetchone():
            raise ContractError("review_journal_disallows_observer")
        if card.channel_id not in {self.pins.listed_channel_id, self.pins.sold_channel_id}:
            raise ContractError("untrusted_channel")
        expected = {"listed"} if card.channel_id == self.pins.listed_channel_id else {"sold", "shipped"}
        if card.status not in expected:
            raise ContractError("channel_state_mismatch", item_id=card.item_id)
        if photo_pending and not card.requires_photos:
            raise ContractError("invalid_photo_retry_state", item_id=card.item_id)
        contract_hash = hashlib.sha256(canonical(_card_observation_values(card))).hexdigest()
        quarantined = self.connection.execute("SELECT * FROM quarantine WHERE item_id=?", (card.item_id,)).fetchone()
        if quarantined and (int(card.message_id) < int(quarantined["message_id"])
                or (card.message_id == quarantined["message_id"] and card.edited_at <= quarantined["edited_at"])):
            return "quarantined"
        old = self.get_item(card.item_id)
        if old:
            if int(card.message_id) < int(old["message_id"]):
                return "stale"
            if card.message_id == old["message_id"] and card.edited_at < old["edited_at"]:
                return "stale"
            if card.sku != old["sku"]:
                raise ContractError("item_identity_changed", item_id=card.item_id)
        owner = self.connection.execute("SELECT item_id FROM items WHERE message_id=? OR sku=?", (card.message_id, card.sku)).fetchone()
        if owner and owner[0] != card.item_id:
            raise ContractError("item_identity_collision", item_id=card.item_id)
        waiting = self.connection.execute("SELECT * FROM photo_waiting WHERE item_id=?", (card.item_id,)).fetchone()
        recovering = (waiting is not None and waiting["message_id"] == card.message_id
                      and waiting["edited_at"] == card.edited_at and waiting["contract_hash"] == contract_hash
                      and card.requires_photos and not photo_pending)
        effective_card = replace(card, issue="photo_error") if photo_pending else card
        observation_hash = hashlib.sha256(canonical({"card": _card_observation_values(effective_card), "photos": photos or []})).hexdigest()
        if old and observation_hash == old["observation_hash"]:
            with self.connection:
                self.connection.execute("UPDATE items SET last_seen=?,missing_since=NULL WHERE item_id=?", (self.now(), card.item_id))
            return "duplicate"
        if old and card.message_id == old["message_id"] and card.edited_at == old["edited_at"] and not recovering:
            raise ContractError("same_message_revision_changed", item_id=card.item_id)
        version = old["revision"] + 1 if old else 1
        body = make_snapshot(effective_card, self.source_id, self.pins.guild_id, version, photos)
        diagnostic = "photo_download_unavailable" if photo_pending else card.issue
        with self.connection:
            body_hash = self._queue(card.item_id, version, body)
            self.connection.execute("""INSERT INTO items(item_id,sku,message_id,channel_id,edited_at,revision,
                observation_hash,body_hash,body,last_seen,diagnostic) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET message_id=excluded.message_id,channel_id=excluded.channel_id,
                edited_at=excluded.edited_at,revision=excluded.revision,observation_hash=excluded.observation_hash,
                body_hash=excluded.body_hash,body=excluded.body,last_seen=excluded.last_seen,
                missing_since=NULL,diagnostic=excluded.diagnostic""",
                (card.item_id, card.sku, card.message_id, card.channel_id, card.edited_at, version,
                 observation_hash, body_hash, body, self.now(), diagnostic))
            self.connection.execute("DELETE FROM quarantine WHERE item_id=?", (card.item_id,))
            if photo_pending:
                self.connection.execute("INSERT OR REPLACE INTO photo_waiting VALUES(?,?,?,?)",
                    (card.item_id, card.message_id, card.edited_at, contract_hash))
            else:
                self.connection.execute("DELETE FROM photo_waiting WHERE item_id=?", (card.item_id,))
        return "queued"

    def defer_photos(self, card: Card):
        """A transient download failure is retryable, not contract quarantine.

        The public snapshot becomes unavailable while retries use the complete,
        exact same verified card metadata. A newer lifecycle card still wins.
        """
        return self.observe(card, photo_pending=True)

    def _withdraw(self, old, *, message_id=None, channel_id=None, edited_at=None, code="source_card_missing"):
        # Preserve sold state rather than turning an already sold product into a
        # generic deletion when its historical Discord card is later cleaned up.
        current = json.loads(old["body"])
        new_message = message_id or old["message_id"]
        new_channel = channel_id or old["channel_id"]
        new_edited = old["edited_at"] if edited_at is None else edited_at
        if current["listing"] is None:
            with self.connection:
                self.connection.execute("""UPDATE items SET message_id=?,channel_id=?,edited_at=?,
                    diagnostic=?,missing_since=NULL WHERE item_id=?""",
                    (new_message, new_channel, new_edited, code, old["item_id"]))
                self.connection.execute("DELETE FROM photo_waiting WHERE item_id=?", (old["item_id"],))
            return "already_unavailable"
        version = old["revision"] + 1
        if version > MAX_INTEGER:
            raise ContractError("publisher_revision_limit_exceeded")
        current.update(version=version, status="deleted", onHold=False, issue=None, listing=None)
        body = canonical(current)
        with self.connection:
            body_hash = self._queue(old["item_id"], version, body)
            self.connection.execute("""UPDATE items SET message_id=?,channel_id=?,edited_at=?,revision=?,body_hash=?,body=?,
                observation_hash=?,diagnostic=?,missing_since=NULL WHERE item_id=?""",
                (new_message, new_channel, new_edited, version, body_hash, body,
                 "withdrawn:" + body_hash, code, old["item_id"]))
            self.connection.execute("DELETE FROM photo_waiting WHERE item_id=?", (old["item_id"],))
        return "queued"

    def fail_closed(self, message_id, *, item_id=None, channel_id=None, edited_at="", code="malformed_public_card"):
        """Use ONLY after verifying author/guild/channel on the fetched message.

        Known message mapping outranks an untrusted malformed item ID. A newer
        trusted malformed card may hide a known item, but never creates one.
        """
        message_id = snowflake(message_id)
        if channel_id is not None and str(channel_id) not in {self.pins.listed_channel_id, self.pins.sold_channel_id}:
            raise ContractError("untrusted_channel")
        old = self.get_by_message(message_id) or (self.get_item(item_id) if item_id is not None else None)
        if old and (int(message_id) < int(old["message_id"])
                    or (message_id == old["message_id"] and edited_at < old["edited_at"])):
            return "ignored"
        allowed_codes = {"malformed_public_card", "missing_price", "missing_photos", "photo_error", "unsupported_condition",
                         "channel_state_mismatch", "item_identity_changed", "item_identity_collision", "same_message_revision_changed"}
        code = code if code in allowed_codes else "malformed_public_card"
        target_item = old["item_id"] if old else str(item_id) if item_id is not None else None
        if target_item is None or not target_item.isdigit() or not 0 < int(target_item) <= MAX_INTEGER:
            return "unidentified"
        actual_channel = str(channel_id) if channel_id is not None else old["channel_id"] if old else None
        if actual_channel is None:
            # Without a known mapping the caller must prove the channel too.
            return "unidentified"
        previous = self.connection.execute("SELECT * FROM quarantine WHERE item_id=?", (target_item,)).fetchone()
        if previous and (int(message_id) < int(previous["message_id"])
                or (message_id == previous["message_id"] and edited_at < previous["edited_at"])):
            return "ignored"
        with self.connection:
            self.connection.execute("""INSERT INTO quarantine VALUES(?,?,?,?,?) ON CONFLICT(item_id)
                DO UPDATE SET message_id=excluded.message_id,channel_id=excluded.channel_id,
                edited_at=excluded.edited_at,diagnostic=excluded.diagnostic""",
                (target_item, message_id, actual_channel, edited_at, code))
            if old:
                return self._withdraw(old, message_id=message_id, channel_id=actual_channel, edited_at=edited_at, code=code)
        return "quarantined"

    def mark_missing(self, message_id, *, confirmed_404=False):
        """Start a deletion grace period; raw delete events alone are insufficient."""
        if not confirmed_404:
            return "unconfirmed"
        old = self.get_by_message(message_id)
        if not old:
            return "ignored"
        with self.connection:
            self.connection.execute("UPDATE items SET missing_since=COALESCE(missing_since,?) WHERE item_id=?", (self.now(), old["item_id"]))
        return "waiting"

    def confirm_missing(self, message_id, *, confirmed_404=False, grace_seconds=30):
        """After channel reconciliation and a fresh 404, withdraw the same card.

        Root must reconcile both allowed stage channels before calling this so a
        move to sold is recognized even if its Gateway event arrived late.
        """
        if not confirmed_404:
            return "unconfirmed"
        if not 5 <= grace_seconds <= 3600:
            raise ContractError("invalid_deletion_grace")
        old = self.get_by_message(message_id)
        if not old:
            return "ignored"
        if old["missing_since"] is None:
            return self.mark_missing(message_id, confirmed_404=True)
        if self.now() - old["missing_since"] < grace_seconds:
            return "waiting"
        return self._withdraw(old)

    def due(self, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractError("invalid_delivery_limit")
        return [dict(row) for row in self.connection.execute(
            """SELECT p.* FROM pending p JOIN items i ON i.item_id=p.item_id
                WHERE p.blocked=0 AND p.due<=? AND (p.priority<>2 OR i.missing_since IS NULL)
                ORDER BY p.priority,p.item_id LIMIT ?""", (self.now(), limit))]

    def is_current(self, pending):
        row = self.connection.execute("""SELECT p.revision,p.body_hash,p.priority,i.missing_since
            FROM pending p JOIN items i ON i.item_id=p.item_id WHERE p.item_id=?""", (pending["item_id"],)).fetchone()
        return bool(row and row[0] == pending["revision"] and row[1] == pending["body_hash"]
                    and (row[2] != 2 or row[3] is None))

    def acknowledge(self, pending, *, receiver_version=None):
        if receiver_version is not None and receiver_version != pending["revision"]:
            self.fail(pending, "receiver_revision_conflict", False)
            return False
        with self.connection:
            self.connection.execute("UPDATE items SET ack_revision=?,ack_hash=? WHERE item_id=? AND revision=? AND body_hash=?",
                (pending["revision"], pending["body_hash"], pending["item_id"], pending["revision"], pending["body_hash"]))
            self.connection.execute("DELETE FROM pending WHERE item_id=? AND revision=? AND body_hash=?",
                (pending["item_id"], pending["revision"], pending["body_hash"]))
        return True

    def fail(self, pending, code, retryable):
        # Never persist arbitrary server exception strings, URLs, or secrets.
        allowed = {"invalid_server_response", "invalid_payload", "disabled_or_bad_key", "source_not_trusted",
                   "version_or_identity_collision", "payload_too_large", "rate_limited", "http_error",
                   "website_unreachable_or_invalid_response", "receiver_revision_conflict"}
        safe_code = code if code in allowed else "delivery_failed"
        attempts = pending["attempts"] + 1
        delay = min(900, 5 * (2 ** min(attempts - 1, 8)))
        with self.connection:
            self.connection.execute("""UPDATE pending SET attempts=?,due=?,blocked=?,error=?
                WHERE item_id=? AND revision=? AND body_hash=?""", (attempts, self.now() + delay,
                int(not retryable), safe_code, pending["item_id"], pending["revision"], pending["body_hash"]))

    def retry_blocked(self):
        with self.connection:
            self.connection.execute("""UPDATE pending SET blocked=0,due=0 WHERE error NOT IN
                ('version_or_identity_collision','receiver_revision_conflict')""")

    def counts(self):
        pending = self.connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
        failed = self.connection.execute("""SELECT COUNT(*) FROM (
            SELECT item_id FROM items WHERE diagnostic IS NOT NULL
            UNION SELECT item_id FROM pending WHERE error IS NOT NULL
            UNION SELECT item_id FROM quarantine)""").fetchone()[0]
        return {"pending": pending, "failed": failed}

    def heartbeat(self):
        return canonical({"schemaVersion": 1, "sourceId": self.source_id, "guildId": self.pins.guild_id, **self.counts()})
