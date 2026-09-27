"""Bounded website transport; never follows redirects or exposes raw errors."""
from dataclasses import dataclass
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .contract import ContractError, MAX_BODY

MAX_RESPONSE = 16 * 1024


def validate_website(value: str, allow_loopback=False) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except (ValueError, AttributeError):
        raise ContractError("invalid_website_origin") from None
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/") or "\\" in value or any(ord(c) < 33 for c in value.strip())
            or (port is not None and not 1 <= port <= 65535)):
        raise ContractError("invalid_website_origin")
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (allow_loopback and loopback and parsed.scheme == "http"):
        raise ContractError("website_requires_https")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Delivery:
    ok: bool
    retryable: bool = False
    code: str = ""
    value: dict | None = None


class Transport:
    def __init__(self, website: str, secret: str, allow_loopback=False):
        self.website = validate_website(website, allow_loopback)
        if not isinstance(secret, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
            raise ContractError("shared_secret_must_be_64_hex_characters")
        self._secret = secret
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def post(self, suffix: str, body: bytes) -> Delivery:
        if suffix not in {"sync", "heartbeat"} or not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY:
            raise ContractError("invalid_delivery_request")
        request = urllib.request.Request(self.website + "/api/integrations/discord/" + suffix,
            data=body, method="POST", headers={"Authorization": "Bearer " + self._secret,
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "PalletWebsitePublisher/1"})
        try:
            with self.opener.open(request, timeout=15) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE + 1)
                content_type = response.headers.get_content_type()
            if len(raw) > MAX_RESPONSE or content_type != "application/json":
                return Delivery(False, True, "invalid_server_response")
            value = json.loads(raw)
            if status != 200 or not isinstance(value, dict) or value.get("ok") is not True:
                return Delivery(False, True, "invalid_server_response")
            if suffix == "sync" and (value.get("outcome") not in {"applied", "replayed", "ignored"}
                                     or type(value.get("version")) is not int):
                return Delivery(False, True, "invalid_server_response")
            return Delivery(True, value=value)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            codes = {400: "invalid_payload", 401: "disabled_or_bad_key", 403: "source_not_trusted",
                     409: "version_or_identity_collision", 413: "payload_too_large", 429: "rate_limited"}
            return Delivery(False, status >= 500 or status in {408, 425, 429}, codes.get(status, "http_error"))
        except (OSError, urllib.error.URLError, ValueError, UnicodeError):
            return Delivery(False, True, "website_unreachable_or_invalid_response")
