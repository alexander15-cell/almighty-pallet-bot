import base64
from copy import deepcopy
import hashlib
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from shop_content import ShopContentError, verify_content, verify_job_content

def fixture_content():
    image = BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')
    raw = image.getvalue()
    return {'product_ref': 'intake_1', 'title': 'Desk lamp', 'description': 'Includes the pictured lamp.',
            'condition': 'used', 'condition_notes': 'Used; testing not verified.', 'quantity': 2,
            'source_state': 'listed', 'photos': [{'mimeType': 'image/png', 'base64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest()}]}

def test_digest_is_stable_and_exactly_fingerprints_public_content():
    value = fixture_content()
    result = verify_content(value)
    assert result == verify_content(deepcopy(value))
    assert result.photo_count == 1 and result.quantity == 2
    for key, changed in [('title', 'Other lamp'), ('quantity', 3), ('condition_notes', 'Scratched.')]:
        copy = deepcopy(value)
        copy[key] = changed
        assert verify_content(copy).digest != result.digest

@pytest.mark.parametrize('key,value', [
    ('source_state', 'sold'), ('source_state', 'held'), ('source_state', 'draft'),
    ('product_ref', '../private'), ('product_ref', 'https://example.com'),
    ('title', ''), ('title', 'x' * 81), ('title', '\ud800'),
    ('description', ''), ('description', 'x' * 4001), ('condition_notes', 'x' * 201),
    ('condition', 'tested_good'), ('condition', '1750'), ('condition', []),
    ('photos', []), ('photos', None), ('photos', [{}] * 11),
])
def test_rejects_unpublishable_content(key, value):
    payload = fixture_content()
    payload[key] = value
    with pytest.raises(ShopContentError):
        verify_content(payload)

@pytest.mark.parametrize('field,value', [('sha256', '0' * 64), ('base64', 'not-base64'), ('mimeType', 'image/jpeg'), ('mimeType', {})])
def test_rejects_mismatched_or_corrupt_photos(field, value):
    payload = fixture_content()
    payload['photos'][0][field] = value
    with pytest.raises(ShopContentError):
        verify_content(payload)

def test_private_fields_cannot_enter_public_content():
    payload = fixture_content()
    payload['supplier_cost'] = 123
    with pytest.raises(ShopContentError):
        verify_content(payload)

def test_job_requires_exact_frozen_content_and_quantity():
    payload = fixture_content()
    content = verify_content(payload)
    job = SimpleNamespace(product_ref=content.product_ref, content_digest=content.digest, quantity=2)
    assert verify_job_content(job, payload) == content
    payload['quantity'] = 1
    with pytest.raises(ShopContentError):
        verify_job_content(job, payload)
