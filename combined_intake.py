"""Reconcile this installation's NEW physical intake items into #website_shop.

The caller owns the shared runtime lock for reconciliation, interactions and
delivery. No ordinary Listed card grants website approval. No old Discord card
or missing original database is adopted. A physical intake row is exactly one
sale unit: Almighty's quantity parser creates separate rows, not a stock count.

Shop review posts are tracked in a separate scoped database, never in the
intake item's current_message_id. All recorded posts remain and their controls
are restored after restart. A crash after Discord accepts a post but before its
receipt is stored can append a duplicate review on retry; revision/state guards
make duplicates harmless, and no old post is deleted to compensate.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from publisher.contract import ContractError, MAX_SOURCE_PHOTO, normalize_photo
from shop_approval import Review, ShopApprovalError, _safe_path
from shop_content import ShopContentError, verify_content
from shop_discord import ShopReviewView, post_review
from shop_values import MAX_INTEGER

MIN_NEW_ITEM_ID = 1_000_000_000
TRACKING_APPLICATION_ID = 0x46474941
CONDITIONS = {
    "1000": ("new", "New; functional testing has not been independently verified."),
    "1500": ("open_box", "Open box; functional testing has not been independently verified."),
    "3000": ("used", "Returned; functional testing has not been independently verified."),
}


class IntakeAdapterError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "intake_adapter_error"
        super().__init__(self.code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class IntakeAdapter:
    def __init__(self, bot, settings, store):
        # Import only after the guarded entry point has configured isolated
        # intake paths. Importing combined_intake itself never loads dotenv.
        import config
        import database

        if (not getattr(config, "COMBINED_MODE", False) or not config.PRESERVE_DISCORD_HISTORY
                or str(settings.guild_id) != store.guild_id
                or str(settings.channels["website_shop"]) != store.shop_channel_id
                or type(settings.item_id_floor) is not int
                or not MIN_NEW_ITEM_ID <= settings.item_id_floor <= MAX_INTEGER
                or settings.sku_prefix != "FGNEW-"):
            raise IntakeAdapterError("invalid_combined_intake_scope")
        self.bot, self.settings, self.store, self.db = bot, settings, store, database
        self.photo_root = _safe_path(settings.photo_directory)
        if not self.photo_root.is_absolute():
            raise IntakeAdapterError("invalid_photo_root")
        if _safe_path(config.PHOTO_DIR) != self.photo_root:
            raise IntakeAdapterError("intake_photo_root_mismatch")
        self._registered = set()
        self._tracking_path = _safe_path(store.path.parent / "intake-shop-tracking.sqlite")
        self._tracking = self._open_tracking()
        # shop_discord invokes this only after a successfully appended card.
        store.record_review_message = self.record_review_message

    def _open_tracking(self):
        scope = _canonical({"schema": 1, "guild": self.store.guild_id,
                            "shop": self.store.shop_channel_id,
                            "application": self.settings.application_id,
                            "item_floor": self.settings.item_id_floor,
                            "sku_prefix": self.settings.sku_prefix})
        path = self._tracking_path
        for suffix in ("-wal", "-shm", "-journal"):
            _safe_path(str(path) + suffix)
        exists = path.exists()
        if exists:
            probe = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                probe.execute("PRAGMA query_only=ON")
                if probe.execute("PRAGMA application_id").fetchone()[0] != TRACKING_APPLICATION_ID:
                    raise IntakeAdapterError("foreign_intake_tracking_database")
                if dict(probe.execute("SELECT key,value FROM adapter_meta")) != {"scope": scope}:
                    raise IntakeAdapterError("intake_tracking_scope_changed")
            except sqlite3.Error:
                raise IntakeAdapterError("foreign_intake_tracking_database") from None
            finally:
                probe.close()
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        if not exists:
            with connection:
                connection.executescript("""
                    CREATE TABLE adapter_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE review_posts(
                        message_id TEXT PRIMARY KEY,item_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,state TEXT NOT NULL,
                        title TEXT NOT NULL,review_json TEXT NOT NULL);
                    CREATE TRIGGER review_posts_keep_update BEFORE UPDATE ON review_posts
                    BEGIN SELECT RAISE(ABORT,'review posts are immutable'); END;
                    CREATE TRIGGER review_posts_keep_delete BEFORE DELETE ON review_posts
                    BEGIN SELECT RAISE(ABORT,'review posts are immutable'); END;
                """)
                connection.execute("INSERT INTO adapter_meta VALUES ('scope',?)", (scope,))
                connection.execute(f"PRAGMA application_id={TRACKING_APPLICATION_ID}")
        return connection

    def close(self):
        if getattr(self.store, "record_review_message", None) == self.record_review_message:
            del self.store.record_review_message
        self._tracking.close()

    def _item_id(self, value):
        if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,9}", str(value)):
            raise IntakeAdapterError("unowned_intake_item")
        number = int(value)
        if not self.settings.item_id_floor <= number <= MAX_INTEGER:
            raise IntakeAdapterError("unowned_intake_item")
        return number

    def _reference_id(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r"intake-[1-9][0-9]{0,9}", reference):
            raise IntakeAdapterError("unowned_intake_reference")
        return self._item_id(reference[7:])

    def _owned_item(self, item_id):
        number = self._item_id(item_id)
        item = self.db.get_item(number)
        if item is None:
            return None
        pallet_id = item.get("pallet_id")
        if (item.get("id") != number or type(pallet_id) is not int or pallet_id <= 0
                or type(item.get("item_number")) is not int or item["item_number"] <= 0
                or str(self.db.get_stage_channel_id(pallet_id, "data-entry")) != str(self.settings.channels["data-entry"])
                or str(self.db.resolve_channel_id(pallet_id, "listed")) != str(self.settings.channels["listed"])
                or str(self.db.resolve_channel_id(pallet_id, "sold")) != str(self.settings.channels["sold"])):
            raise IntakeAdapterError("intake_channel_mapping_changed")
        return item

    async def lifecycle(self, item_id):
        item = self._owned_item(item_id)
        if item is None:
            return "withdrawn"
        pallet = self.db.get_pallet(item["pallet_id"])
        if not pallet or pallet.get("archived"):
            return "withdrawn"
        if item["status"] in {"sold", "shipped", "picked_up"}:
            return "sold"
        if item["status"] == "on_hold":
            return "held"
        if item["status"] == "listed":
            return "listed"
        if item["status"] in {"deleted", "disposed", "returned"}:
            return "withdrawn"
        # A previously approved item sent back to intake/review must leave the
        # website, but can later return only through a new approval.
        return "held"

    def _photos(self, item):
        try:
            paths = json.loads(item.get("photo_urls") or "null")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
                raise ValueError()
            item_root = self.photo_root / str(item["id"])
            photos = []
            for raw_path in paths:
                if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
                    raise ValueError()
                path = _safe_path(raw_path)
                path.relative_to(item_root)
                before = path.stat()
                if not path.is_file() or not 0 < before.st_size <= MAX_SOURCE_PHOTO:
                    raise ValueError()
                with path.open("rb") as stream:
                    contents = stream.read(MAX_SOURCE_PHOTO + 1)
                after = path.stat()
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                    raise ValueError()
                photos.append(normalize_photo(contents))
            return photos
        except (OSError, ValueError, TypeError, KeyError, ContractError, ShopApprovalError):
            raise IntakeAdapterError("invalid_intake_photos") from None

    async def resolve_content(self, product_ref):
        item_id = self._reference_id(product_ref)
        item = self._owned_item(item_id)
        if item is None or await self.lifecycle(item_id) != "listed":
            raise ShopContentError("item_not_available")
        listing = self.db.get_ebay_listing_data(item_id)
        if not listing or listing.get("listing_format") != "FixedPrice":
            raise IntakeAdapterError("intake_listing_not_ready")
        condition = CONDITIONS.get(str(listing.get("condition_id")))
        if condition is None:
            # Never recast known defects/not-working as merely untested.
            raise ShopContentError("unsupported_condition")
        content = {"product_ref": product_ref, "title": listing.get("ebay_title"),
                   "description": item.get("ai_description") or item.get("raw_description"),
                   "condition": condition[0], "condition_notes": condition[1],
                   "quantity": 1, "source_state": "listed", "photos": self._photos(item)}
        verify_content(content)
        return content

    def record_review_message(self, review, title, message_id):
        self._reference_id(review.product_ref)
        if str(self._reference_id(review.product_ref)) != review.item_id:
            raise IntakeAdapterError("intake_review_identity_changed")
        message_id = str(message_id)
        if not re.fullmatch(r"[1-9][0-9]{16,19}", message_id) or int(message_id) >= 2**64:
            raise IntakeAdapterError("invalid_review_message_identity")
        values = (message_id, review.item_id, review.revision, review.state, title, _canonical(asdict(review)))
        existing = self._tracking.execute("SELECT * FROM review_posts WHERE message_id=?", (message_id,)).fetchone()
        if existing and tuple(existing) != values:
            raise IntakeAdapterError("intake_review_message_conflict")
        with self._tracking:
            self._tracking.execute("INSERT OR IGNORE INTO review_posts VALUES (?,?,?,?,?,?)", values)
        self._registered.add(message_id)

    def _restore_views(self):
        for row in self._tracking.execute("SELECT * FROM review_posts ORDER BY rowid"):
            if row["message_id"] in self._registered:
                continue
            review = Review(**json.loads(row["review_json"]))
            self._reference_id(review.product_ref)
            self.bot.add_view(ShopReviewView(store=self.store, review=review, title=row["title"],
                resolver=self.resolve_content, application_id=self.settings.application_id), message_id=int(row["message_id"]))
            self._registered.add(row["message_id"])

    async def restore_views(self):
        """Register retained shop controls; never posts or rewrites a card."""
        self._restore_views()

    def _request(self, item_id, action, revision, digest=""):
        token = hashlib.sha256(f"{item_id}:{action}:{revision}:{digest}".encode()).hexdigest()
        return "system-intake-" + action + "-" + token

    async def _post_if_needed(self, review, title):
        existing = self._tracking.execute("SELECT 1 FROM review_posts WHERE item_id=? AND revision=? AND state=? LIMIT 1",
                                          (review.item_id, review.revision, review.state)).fetchone()
        if existing:
            return False
        channel = self.bot.get_channel(int(self.store.shop_channel_id))
        if channel is None:
            raise IntakeAdapterError("shop_channel_unavailable")
        message = await post_review(channel, store=self.store, review=review, title=title,
                                    resolver=self.resolve_content, application_id=self.settings.application_id)
        # Also retain receipt when a caller uses an earlier post_review without
        # the optional callback. The idempotent record makes both paths safe.
        self.record_review_message(review, title, message.id)
        return True

    async def reconcile(self):
        """Return safe counts; no publishing, human approval, or old-post edits."""
        self._restore_views()
        counts = {"created": 0, "refreshed": 0, "invalidated": 0, "posted": 0, "blocked": 0}
        with self.db.get_conn() as connection:
            numbers = {str(row[0]) for row in connection.execute("SELECT id FROM items WHERE id>=?", (self.settings.item_id_floor,))}
        # Include tracked records whose source row is now missing, for safe withdrawal.
        for review in self.store.delivery_reviews():
            if review.product_ref.startswith("intake-"):
                numbers.add(str(self._reference_id(review.product_ref)))
        for item_id in sorted(numbers, key=int):
            review = self.store.system_get_review(item_id)
            try:
                item = self._owned_item(item_id)
                state = await self.lifecycle(item_id)
                if review is not None and (review.product_ref != f"intake-{item_id}"
                        or (item is not None and review.sku != f"{self.settings.sku_prefix}PALLET-{item['pallet_id']}-ITEM-{item['item_number']}")):
                    raise IntakeAdapterError("intake_review_identity_changed")
                if state != "listed":
                    if review is not None:
                        if review.state not in {"sold", "withdrawn"} and review.state != state:
                            review = self.store.invalidate_from_source(item_id, state)
                            counts["invalidated"] += 1
                        counts["posted"] += int(await self._post_if_needed(review, f"Item {item_id}"))
                    continue
                content = await self.resolve_content(f"intake-{item_id}")
                verified = verify_content(content)
                if review is None:
                    # Deliberately do not copy raw stored REAL prices or guessed
                    # eBay links: the operator enters both in the shop form.
                    sku = f"{self.settings.sku_prefix}PALLET-{item['pallet_id']}-ITEM-{item['item_number']}"
                    review = self.store.system_create_draft(item_id=item_id, sku=sku,
                        product_ref=verified.product_ref, content_digest=verified.digest, quantity=1,
                        request_id=self._request(item_id, "create", 1, verified.digest))
                    counts["created"] += 1
                elif review.state in {"sold", "withdrawn"}:
                    # Retained legacy/new terminal states are never resurrected.
                    continue
                else:
                    if review.state == "held":
                        review = self.store.system_release_hold(item_id, expected_revision=review.revision,
                            request_id=self._request(item_id, "release", review.revision))
                    if review.content_digest != verified.digest or review.quantity != 1:
                        review = self.store.system_refresh(item_id, expected_revision=review.revision,
                            content_digest=verified.digest, quantity=1,
                            request_id=self._request(item_id, "refresh", review.revision, verified.digest))
                        counts["refreshed"] += 1
                counts["posted"] += int(await self._post_if_needed(review, verified.title))
            except (IntakeAdapterError, ShopContentError, ShopApprovalError, ContractError):
                counts["blocked"] += 1
                # Bad/missing photos, condition or text must cancel an existing
                # approval. Sender handles the resulting website withdrawal.
                if (review is not None and review.product_ref == f"intake-{item_id}"
                        and review.sku.startswith(self.settings.sku_prefix)
                        and review.state not in {"held", "sold", "withdrawn"}):
                    self.store.invalidate_from_source(item_id, "held")
                    counts["invalidated"] += 1
            except Exception:
                # A failed append is retryable next cycle; never approve or delete
                # a post to make a failed UI send look successful.
                counts["blocked"] += 1
        return counts
