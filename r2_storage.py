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
