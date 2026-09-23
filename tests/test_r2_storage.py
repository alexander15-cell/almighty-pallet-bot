"""
r2_storage.py's deletion helpers (object_key_from_public_url, delete_photos,
wipe_all_photos) - used by /admin db-wipe (full reset) and
/admin purge-old-photos (retention-based cleanup after a sale). Mocks the
boto3 client via _get_client() so no real network/AWS SDK calls happen.
"""
import os

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import config
import r2_storage


class _FakeS3Client:
    def __init__(self, pages=None):
        self.deleted_batches = []
        self._pages = pages or []
        self._page_index = 0

    def delete_objects(self, Bucket, Delete):
        self.deleted_batches.append([obj["Key"] for obj in Delete["Objects"]])

    def list_objects_v2(self, Bucket, ContinuationToken=None):
        page = self._pages[self._page_index]
        self._page_index += 1
        return page


def _install_fake_client(monkeypatch, client):
    monkeypatch.setattr(r2_storage, "_get_client", lambda: client)


# ------------------------------------------------- object_key_from_public_url


def test_object_key_from_public_url_strips_base(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_URL_BASE", "https://pub-xxxx.r2.dev")
    url = "https://pub-xxxx.r2.dev/items/12/photo_0.jpg"
    assert r2_storage.object_key_from_public_url(url) == "items/12/photo_0.jpg"


def test_object_key_from_public_url_handles_trailing_slash_in_base(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_URL_BASE", "https://pub-xxxx.r2.dev/")
    url = "https://pub-xxxx.r2.dev/items/12/photo_0.jpg"
    assert r2_storage.object_key_from_public_url(url) == "items/12/photo_0.jpg"


def test_object_key_from_public_url_returns_none_for_empty():
    assert r2_storage.object_key_from_public_url("") is None
    assert r2_storage.object_key_from_public_url(None) is None


def test_object_key_from_public_url_returns_none_for_mismatched_base(monkeypatch):
    monkeypatch.setattr(config, "R2_PUBLIC_URL_BASE", "https://pub-xxxx.r2.dev")
    assert r2_storage.object_key_from_public_url("https://some-other-host.com/items/12/photo_0.jpg") is None


# --------------------------------------------------------------- delete_photos


def test_delete_photos_skips_none_and_empty(monkeypatch):
    client = _FakeS3Client()
    _install_fake_client(monkeypatch, client)
    monkeypatch.setattr(config, "R2_BUCKET_NAME", "test-bucket")

    count = r2_storage.delete_photos([None, "", "items/1/a.jpg", None])
    assert count == 1
    assert client.deleted_batches == [["items/1/a.jpg"]]


def test_delete_photos_returns_zero_and_skips_api_call_when_no_keys(monkeypatch):
    client = _FakeS3Client()
    _install_fake_client(monkeypatch, client)
    assert r2_storage.delete_photos([]) == 0
    assert client.deleted_batches == []


def test_delete_photos_batches_over_1000_keys(monkeypatch):
    client = _FakeS3Client()
    _install_fake_client(monkeypatch, client)
    monkeypatch.setattr(config, "R2_BUCKET_NAME", "test-bucket")

    keys = [f"items/{i}/a.jpg" for i in range(2500)]
    count = r2_storage.delete_photos(keys)

    assert count == 2500
    assert len(client.deleted_batches) == 3
    assert [len(b) for b in client.deleted_batches] == [1000, 1000, 500]


# -------------------------------------------------------------- wipe_all_photos


def test_wipe_all_photos_deletes_everything_single_page(monkeypatch):
    client = _FakeS3Client(pages=[
        {"Contents": [{"Key": "items/1/a.jpg"}, {"Key": "items/2/b.jpg"}], "IsTruncated": False},
    ])
    _install_fake_client(monkeypatch, client)
    monkeypatch.setattr(config, "R2_BUCKET_NAME", "test-bucket")

    deleted = r2_storage.wipe_all_photos()

    assert deleted == 2
    assert client.deleted_batches == [["items/1/a.jpg", "items/2/b.jpg"]]


def test_wipe_all_photos_follows_pagination(monkeypatch):
    client = _FakeS3Client(pages=[
        {"Contents": [{"Key": "items/1/a.jpg"}], "IsTruncated": True, "NextContinuationToken": "tok1"},
        {"Contents": [{"Key": "items/2/b.jpg"}], "IsTruncated": False},
    ])
    _install_fake_client(monkeypatch, client)
    monkeypatch.setattr(config, "R2_BUCKET_NAME", "test-bucket")

    deleted = r2_storage.wipe_all_photos()

    assert deleted == 2
    assert client.deleted_batches == [["items/1/a.jpg"], ["items/2/b.jpg"]]


def test_wipe_all_photos_handles_empty_bucket(monkeypatch):
    client = _FakeS3Client(pages=[{"Contents": [], "IsTruncated": False}])
    _install_fake_client(monkeypatch, client)
    monkeypatch.setattr(config, "R2_BUCKET_NAME", "test-bucket")

    assert r2_storage.wipe_all_photos() == 0
    assert client.deleted_batches == []
