"""Strict public card contract. Never fetches URLs or reads intake databases."""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import urllib.parse
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MARKER = "4G1P-WEBSITE/1"
MAX_INTEGER = 2_147_483_647
MAX_PHOTOS = 10
MAX_SOURCE_PHOTO = 10 * 1024 * 1024
MAX_TRANSFER_PHOTO = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_BODY = 30 * 1024 * 1024
CONDITIONS = frozenset({"new", "new_damaged_packaging", "open_box", "used", "tested_good", "cosmetic_imperfection", "as_is"})
ISSUES = frozenset({"missing_price", "missing_approved_text", "missing_photos", "unsupported_condition", "auction_listing", "photo_error"})
SOURCE_ID = re.compile(r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\Z")


class ContractError(ValueError):
    """Fixed diagnostic only; item_id may help quarantine a known current card."""
    def __init__(self, code="malformed_public_card", *, item_id=None):
        super().__init__(code)
        self.code = code
        self.item_id = item_id


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def snowflake(value) -> str:
    if (not isinstance(value, (str, int)) or isinstance(value, bool)
            or not re.fullmatch(r"[1-9][0-9]{16,19}", str(value))
            or int(value) > 18_446_744_073_709_551_615):
        raise ContractError("invalid_discord_identity")
    return str(value)


@dataclass(frozen=True)
class TrustPins:
    guild_id: str
    intake_bot_id: str
    listed_channel_id: str
    sold_channel_id: str

    def __post_init__(self):
        for key in ("guild_id", "intake_bot_id", "listed_channel_id", "sold_channel_id"):
            object.__setattr__(self, key, snowflake(getattr(self, key)))
        if self.listed_channel_id == self.sold_channel_id:
            raise ContractError("stage_channels_must_differ")


@dataclass(frozen=True)
class PhotoAttachment:
    id: str
    filename: str
    size: int | None
    content_type: str | None


@dataclass(frozen=True)
class Card:
    item_id: str
    sku: str
    status: str
    on_hold: bool
    issue: str | None
    message_id: str
    channel_id: str
    edited_at: str
    title: str | None = None
    description: str | None = None
    price_cents: int | None = None
    condition: str | None = None
    condition_notes: str | None = None
    attachments: tuple[PhotoAttachment, ...] = ()
    # None preserves the original wire contract (receiver defaults to one unit).
    # Explicit quantity describes sale units/lots, not pieces mentioned in a title.
    quantity: int | None = None
    ebay_item_id: str | None = None

    @property
    def requires_photos(self):
        return self.status == "listed" and not self.on_hold and self.issue is None


def is_trusted_message(raw: dict, pins: TrustPins) -> bool:
    if not isinstance(raw, dict):
        return False
    author = raw.get("author")
    return (isinstance(author, dict) and author.get("bot") is True
            and str(raw.get("guild_id")) == pins.guild_id
            and str(author.get("id")) == pins.intake_bot_id
            and str(raw.get("channel_id")) in {pins.listed_channel_id, pins.sold_channel_id}
            and not raw.get("webhook_id"))


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError()
    return value.strip()


def _item_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > MAX_INTEGER:
        raise ContractError()
    return value


def _edited_at(raw):
    value = raw.get("edited_timestamp")
    if value is None:
        return ""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ContractError("invalid_message_revision") from None


def discord_photo_resource(url, channel_id, expected_attachment_id=None):
    """Return a pinned Discord-upload identity, never a generic image URL."""
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 4096:
            raise ValueError()
        parsed = urllib.parse.urlsplit(url)
        parts = parsed.path.split("/")
        if (parsed.scheme != "https" or parsed.hostname not in {"cdn.discordapp.com", "media.discordapp.net"}
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or "\\" in url or any(ord(c) < 33 for c in url)
                or len(parts) != 5 or parts[0] != "" or parts[1] != "attachments"
                or parts[2] != snowflake(channel_id)):
            raise ValueError()
        attachment_id = snowflake(parts[3])
        if expected_attachment_id is not None and attachment_id != snowflake(expected_attachment_id):
            raise ValueError()
        filename = urllib.parse.unquote(parts[4], errors="strict")
        if (not 1 <= len(filename) <= 255 or filename in {".", ".."} or "/" in filename or "\\" in filename
                or any(ord(c) < 32 for c in filename)):
            raise ValueError()
        return attachment_id, filename
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise ContractError("untrusted_photo_url") from None


def photo_sources(raw: dict, pins: TrustPins) -> list[dict]:
    """Combine real attachments with embed-referenced Discord uploads.

    Discord may omit embed-referenced uploads from `attachments`. Only
    embed.image.url resources pinned to this message's approved channel are
    eligible; proxy URLs, thumbnails, arbitrary external links and text are not
    candidates. Embedded-only resources have genuinely unknown file size.
    """
    if not is_trusted_message(raw, pins):
        raise ContractError("untrusted_message")
    sources = {}
    raw_attachments = raw.get("attachments", [])
    embeds = raw.get("embeds", [])
    if not isinstance(raw_attachments, list) or len(raw_attachments) > MAX_PHOTOS:
        raise ContractError("missing_photos")
    if not isinstance(embeds, list) or len(embeds) > MAX_PHOTOS:
        raise ContractError("photo_error")
    for attachment in raw_attachments:
        if not isinstance(attachment, dict):
            raise ContractError("photo_error")
        content_type = attachment.get("content_type")
        filename = attachment.get("filename")
        if not isinstance(filename, str) or not 1 <= len(filename) <= 255:
            raise ContractError("photo_error")
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif", None}:
            continue
        if content_type is None and not filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            continue
        size = attachment.get("size")
        if type(size) is not int or not 0 < size <= MAX_SOURCE_PHOTO:
            raise ContractError("photo_error")
        attachment_id = snowflake(attachment.get("id"))
        if attachment_id in sources:
            raise ContractError("photo_error")
        sources[attachment_id] = {"id": attachment_id, "filename": filename, "size": size,
                                  "content_type": content_type, "url": attachment.get("url"), "embedded": False}
    for embed in embeds:
        if not isinstance(embed, dict):
            raise ContractError("photo_error")
        image = embed.get("image")
        if image is None:
            continue
        if not isinstance(image, dict) or not image.get("url"):
            raise ContractError("photo_error")
        image_url = image["url"]
        # Pre-REST attachment:// references can only resolve an actual attached
        # image by exact filename; they cannot add an unverified source.
        if isinstance(image_url, str) and image_url.startswith("attachment://"):
            matches = [source for source in sources.values() if source["filename"] == image_url[len("attachment://"):]]
            if len(matches) != 1:
                raise ContractError("photo_error")
            continue
        attachment_id, filename = discord_photo_resource(image_url, raw["channel_id"])
        if attachment_id in sources:
            # Keep known real size/type, using the verified original embed URL.
            sources[attachment_id]["url"] = image_url
            continue
        sources[attachment_id] = {"id": attachment_id, "filename": filename, "size": None,
                                  "content_type": None, "url": image_url, "embedded": True}
    if not 1 <= len(sources) <= MAX_PHOTOS:
        raise ContractError("missing_photos")
    return list(sources.values())


def has_contract_marker(primary) -> bool:
    if not isinstance(primary, dict):
        return False
    footer = primary.get("footer")
    marker = footer.get("text") if isinstance(footer, dict) else None
    return (isinstance(marker, str) and
            (marker == MARKER or marker.startswith(MARKER + " ") or marker.startswith(MARKER + "\n")))


def is_legacy_product_card(raw: dict, pins: TrustPins) -> bool:
    """Recognize unsupported intake cards without guessing their sale details.

    Only the trusted primary embed is examined. Ordinary help text, other
    authors, secondary embeds and unrelated channels are not inventory.
    """
    if not is_trusted_message(raw, pins):
        return False
    embeds = raw.get("embeds")
    if not isinstance(embeds, list) or not embeds or not isinstance(embeds[0], dict):
        return False
    primary = embeds[0]
    if has_contract_marker(primary):
        return False
    fields = primary.get("fields")
    if not isinstance(fields, list):
        return False
    names = {field.get("name") for field in fields
             if isinstance(field, dict) and isinstance(field.get("name"), str)}
    # Almighty's real send_item_card uses the emoji-prefixed label. Accept
    # only these two known labels; recognizing a legacy card BLOCKS the whole
    # scan and never grants it publication eligibility or guesses its values.
    return bool(names & {"Pallet", "📦 Pallet"}) and {"Item #", "Status"}.issubset(names)


def parse_message(raw: dict, pins: TrustPins) -> Card | None:
    """Accept only the trusted primary embed. Non-contract messages return None.

    A missing contract on an already tracked message must be handled by the
    caller with Journal.fail_closed; unrelated help messages are not inventory.
    """
    if not is_trusted_message(raw, pins):
        return None
    embeds = raw.get("embeds")
    if not isinstance(embeds, list) or not embeds or not isinstance(embeds[0], dict):
        return None
    primary = embeds[0]
    if not has_contract_marker(primary):
        return None
    item_id = None
    try:
        fields = primary.get("fields")
        if not isinstance(fields, list):
            raise ContractError()
        public = {}
        for field in fields:
            if not isinstance(field, dict):
                raise ContractError()
            name = field.get("name")
            if isinstance(name, str) and name.startswith("Website "):
                if name in public:
                    raise ContractError()
                public[name] = field.get("value")
        item_id = _item_id(public.get("Website item ID"))
        sku = _text(public.get("Website SKU"), 100)
        status = public.get("Website status")
        expected = {"listed"} if str(raw["channel_id"]) == pins.listed_channel_id else {"sold", "shipped"}
        if status not in expected:
            raise ContractError("channel_state_mismatch")
        hold = public.get("Website hold")
        issue = public.get("Website issue")
        if hold not in {"true", "false"} or issue not in ISSUES | {"none"}:
            raise ContractError()
        if status != "listed" and issue != "none":
            raise ContractError()
        card = Card(item_id, sku, status, hold == "true", None if issue == "none" else issue,
                    snowflake(raw.get("id")), snowflake(raw.get("channel_id")), _edited_at(raw))
        if not card.requires_photos:
            return card
        price = public.get("Website price cents")
        if not isinstance(price, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", price) or int(price) > MAX_INTEGER:
            raise ContractError("missing_price")
        condition = public.get("Website condition")
        if condition not in CONDITIONS:
            raise ContractError("unsupported_condition")
        attachments = tuple(PhotoAttachment(source["id"], source["filename"], source["size"], source["content_type"])
                            for source in photo_sources(raw, pins))
        return replace(card, title=_text(primary.get("title"), 80), description=_text(primary.get("description"), 4000),
                       price_cents=int(price), condition=condition,
                       condition_notes=_text(public.get("Website condition notes"), 200), attachments=attachments)
    except ContractError as error:
        raise ContractError(error.code, item_id=item_id) from None
    except (TypeError, KeyError, ValueError, OverflowError):
        raise ContractError(item_id=item_id) from None


def normalize_photo(data: bytes) -> dict:
    """Decode actual bytes, strip metadata, orient and copy as bounded JPEG."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_SOURCE_PHOTO:
        raise ContractError("photo_error")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP", "GIF"} or original.width * original.height > MAX_PIXELS:
                    raise ContractError("photo_error")
                original.seek(0)
                original.load()
                oriented = ImageOps.exif_transpose(original).convert("RGBA")
                prepared = Image.new("RGB", oriented.size, "white")
                prepared.paste(oriented, mask=oriented.getchannel("A"))
                prepared.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                for quality in (85, 75, 60):
                    output = io.BytesIO()
                    prepared.save(output, format="JPEG", quality=quality, optimize=False, progressive=False)
                    encoded = output.getvalue()
                    if len(encoded) <= MAX_TRANSFER_PHOTO:
                        return {"mimeType": "image/jpeg", "base64": base64.b64encode(encoded).decode("ascii"),
                                "sha256": hashlib.sha256(encoded).hexdigest()}
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        pass
    raise ContractError("photo_error")


def make_snapshot(card: Card, source_id: str, guild_id: str, version: int, photos=None) -> bytes:
    if not SOURCE_ID.fullmatch(source_id) or type(version) is not int or not 0 < version <= MAX_INTEGER:
        raise ContractError("invalid_publisher_identity_or_revision")
    snowflake(guild_id)
    if card.quantity is not None and (type(card.quantity) is not int or not 1 <= card.quantity <= MAX_INTEGER):
        raise ContractError("invalid_listing_quantity")
    if card.ebay_item_id is not None and (not isinstance(card.ebay_item_id, str)
            or not re.fullmatch(r"[1-9][0-9]{8,14}", card.ebay_item_id)):
        raise ContractError("invalid_ebay_item_identity")
    listing = None
    if card.requires_photos:
        if not isinstance(photos, list) or len(photos) != len(card.attachments) or not 1 <= len(photos) <= MAX_PHOTOS:
            raise ContractError("missing_photos")
        for photo in photos:
            if not isinstance(photo, dict) or set(photo) != {"mimeType", "base64", "sha256"} or photo["mimeType"] != "image/jpeg":
                raise ContractError("photo_error")
            try:
                data = base64.b64decode(photo["base64"], validate=True)
                if not 0 < len(data) <= MAX_TRANSFER_PHOTO or hashlib.sha256(data).hexdigest() != photo["sha256"]:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ContractError("photo_error") from None
        listing = {"title": card.title, "description": card.description, "priceCents": card.price_cents,
                   "condition": card.condition, "conditionNotes": card.condition_notes, "photos": photos}
        if card.quantity is not None:
            listing["quantity"] = card.quantity
        if card.ebay_item_id is not None:
            listing["ebayItemId"] = card.ebay_item_id
    body = canonical({"schemaVersion": 1, "sourceId": source_id, "guildId": guild_id, "itemId": card.item_id,
                      "version": version, "sku": card.sku, "status": card.status, "onHold": card.on_hold,
                      "issue": card.issue, "listing": listing})
    if len(body) > MAX_BODY:
        raise ContractError("snapshot_too_large")
    return body
