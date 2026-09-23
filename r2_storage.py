"""
Optional Cloudflare R2 upload for item photos, so anything that needs a
photo URL to stay valid long-term (the eBay CSV batch's PicURL column,
future item records) can use a durable public URL instead of Discord's
attachment links - those expire once the originating message is deleted,
which happens routinely as items move through this bot's pipeline.

Gated behind config.R2_ENABLED (all five R2_* config values set). Data Entry
(see item_flow.py's on_message) still saves the local copy either way -
that's what Discord item cards render from via send_item_card - this only
adds a second, durable copy in R2.
"""
import boto3
from botocore.client import Config as BotoConfig

import config

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=config.R2_ACCESS_KEY_ID,
            aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
            config=BotoConfig(signature_version="s3v4"),
            region_name="auto",
        )
    return _client


def upload_photo(local_path, object_key: str) -> str:
    """
    Uploads the file at `local_path` to the R2 bucket under `object_key` and
    returns its public URL (config.R2_PUBLIC_URL_BASE + object_key). Only
    meaningful when config.R2_ENABLED is True - callers check that first, so
    this never runs without real credentials configured.
    """
    client = _get_client()
    with open(local_path, "rb") as f:
        client.put_object(
            Bucket=config.R2_BUCKET_NAME,
            Key=object_key,
            Body=f,
            ContentType=_guess_content_type(str(local_path)),
        )
    return f"{config.R2_PUBLIC_URL_BASE.rstrip('/')}/{object_key}"


def _guess_content_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")


def object_key_from_public_url(url: str):
    """
    The reverse of upload_photo()'s return value: strips
    config.R2_PUBLIC_URL_BASE back off a URL saved in items.photo_public_urls
    to recover the object key to delete. Returns None if `url` is empty or
    doesn't actually start with the configured base (e.g. it was uploaded
    under a different R2_PUBLIC_URL_BASE at some point) - callers should
    skip those rather than guess at a key that might delete the wrong object.
    """
    if not url:
        return None
    base = config.R2_PUBLIC_URL_BASE.rstrip("/") + "/"
    if not url.startswith(base):
        return None
    return url[len(base):]


def delete_photos(object_keys: list) -> int:
    """
    Deletes the given R2 object keys (None/empty entries are skipped).
    Batches in groups of up to 1000 - R2 mirrors S3's DeleteObjects limit.
    Deleting an already-gone key isn't an error (S3/R2's DeleteObjects is
    idempotent), so this is safe to retry. Returns how many keys were
    requested for deletion, not necessarily how many objects actually
    existed.
    """
    keys = [k for k in object_keys if k]
    if not keys:
        return 0
    client = _get_client()
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        client.delete_objects(
            Bucket=config.R2_BUCKET_NAME,
            Delete={"Objects": [{"Key": k} for k in batch]},
        )
    return len(keys)


def wipe_all_photos() -> int:
    """
    Deletes EVERY object in the R2 bucket - used by /admin db-wipe's full
    reset, so photos don't silently keep accumulating (and costing storage)
    in R2 after every other trace of the data they belonged to is gone.
    Assumes R2_BUCKET_NAME is dedicated to this bot's item photos (see this
    module's docstring) - if it's ever shared with unrelated data, this
    would delete that too, so don't point R2_BUCKET_NAME at a shared bucket.
    Returns how many objects were deleted.
    """
    client = _get_client()
    deleted = 0
    continuation_token = None
    while True:
        kwargs = {"Bucket": config.R2_BUCKET_NAME}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = client.list_objects_v2(**kwargs)
        contents = response.get("Contents", [])
        if contents:
            client.delete_objects(
                Bucket=config.R2_BUCKET_NAME,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in contents]},
            )
            deleted += len(contents)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
    return deleted
