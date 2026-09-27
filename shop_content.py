"""Validate and fingerprint the exact public content reviewed in #shop.

No files, configuration, Discord, or website access. The guarded merged runtime
must resolve current inventory and normalized photos, never a chat-supplied URL.
"""
from dataclasses import dataclass
import base64
import hashlib
from io import BytesIO
import json
import re
import unicodedata
import warnings

from PIL import Image
from shop_values import validate_quantity

MAX_PHOTO_BYTES = 2 * 1024 * 1024
CONTENT_KEYS = frozenset({'product_ref', 'title', 'description', 'condition', 'condition_notes', 'quantity', 'photos', 'source_state'})
# Preserve the current intake contract; do not infer testing from eBay condition.
CONDITIONS = frozenset({'new', 'open_box', 'used'})

class ShopContentError(ValueError):
    def __init__(self, code):
        self.code = code if code in {'invalid_public_content', 'item_not_available', 'invalid_product_reference', 'unsupported_condition', 'invalid_product_photos', 'reviewed_content_changed'} else 'invalid_public_content'
        super().__init__(self.code)

@dataclass(frozen=True)
class VerifiedContent:
    product_ref: str
    title: str
    quantity: int
    digest: str
    photo_count: int

def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum:
        raise ShopContentError('invalid_public_content')
    if any(unicodedata.category(character) in {'Cc', 'Cf', 'Cs'} and character not in '\n\r\t' for character in value):
        raise ShopContentError('invalid_public_content')
    return value

def verify_content(value):
    if not isinstance(value, dict) or set(value) != CONTENT_KEYS:
        raise ShopContentError('invalid_public_content')
    if value['source_state'] != 'listed':
        raise ShopContentError('item_not_available')
    reference = value['product_ref']
    if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', reference):
        raise ShopContentError('invalid_product_reference')
    title = text(value['title'], 80)
    description = text(value['description'], 4000)
    notes = text(value['condition_notes'], 200)
    if not isinstance(value['condition'], str) or value['condition'] not in CONDITIONS:
        raise ShopContentError('unsupported_condition')
    quantity = validate_quantity(value['quantity'])
    photos = value['photos']
    if not isinstance(photos, list) or not 1 <= len(photos) <= 10:
        raise ShopContentError('invalid_product_photos')
    photo_facts = []
    for photo in photos:
        if not isinstance(photo, dict) or set(photo) != {'mimeType', 'base64', 'sha256'}:
            raise ShopContentError('invalid_product_photos')
        if not isinstance(photo['mimeType'], str):
            raise ShopContentError('invalid_product_photos')
        expected_format = {'image/jpeg': 'JPEG', 'image/png': 'PNG', 'image/webp': 'WEBP'}.get(photo['mimeType'])
        if (not expected_format or not isinstance(photo['base64'], str)
                or not 4 <= len(photo['base64']) <= 4 * ((MAX_PHOTO_BYTES + 2) // 3)
                or not isinstance(photo['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', photo['sha256'])):
            raise ShopContentError('invalid_product_photos')
        try:
            raw = base64.b64decode(photo['base64'], validate=True)
            if not 1 <= len(raw) <= MAX_PHOTO_BYTES or hashlib.sha256(raw).hexdigest() != photo['sha256']:
                raise ValueError('photo_mismatch')
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as picture:
                    if picture.format != expected_format or getattr(picture, 'n_frames', 1) != 1:
                        raise ValueError('photo_format')
                    if not 1 <= picture.width <= 10000 or not 1 <= picture.height <= 10000 or picture.width * picture.height > 25_000_000:
                        raise ValueError('photo_dimensions')
                    picture.verify()
        except Exception:
            raise ShopContentError('invalid_product_photos') from None
        photo_facts.append({'mimeType': photo['mimeType'], 'sha256': photo['sha256']})
    canonical = {'product_ref': reference, 'title': title, 'description': description,
                 'condition': value['condition'], 'condition_notes': notes,
                 'quantity': quantity, 'photos': photo_facts}
    encoded = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf8')
    return VerifiedContent(reference, title, quantity, hashlib.sha256(encoded).hexdigest(), len(photos))

def verify_job_content(job, value):
    verified = verify_content(value)
    if verified.product_ref != job.product_ref or verified.digest != job.content_digest or verified.quantity != job.quantity:
        raise ShopContentError('reviewed_content_changed')
    return verified
