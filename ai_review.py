"""
Drafts a listing title/description and flags photo/note mismatches for the
Automated Review step. Two backends, switched by config.AI_REVIEW_BACKEND:

  "anthropic" (default) - Claude's cloud vision API, requires ANTHROPIC_API_KEY.
  "ollama"    - a local Ollama server (https://ollama.com), talked to over
                its local HTTP API (config.OLLAMA_BASE_URL, default
                http://localhost:11434) running a vision model
                (config.OLLAMA_VISION_MODEL, default "moondream" - pull it
                first with `ollama pull moondream`). No API key, no per-item
                cost, no network egress - but moondream is a ~1.8B-parameter
                model built mainly for image captioning/VQA, not a general
                instruction-following LLM like Claude, so expect noticeably
                rougher titles/descriptions, less reliable mismatch-flagging,
                and (depending on the host machine) latency that's
                comparable to or worse than the cloud call despite running
                locally. Queue Review's Edit button exists for exactly this
                kind of touch-up. Switch AI_REVIEW_BACKEND back to
                "anthropic" in .env any time local quality isn't good enough.

Both backends return the same dict shape (see SYSTEM_PROMPT) and this
module NEVER modifies or regenerates the photos themselves - only text is
produced.

Photos are read from local disk (see item_flow.py, which saves every
submitted photo under data/photos/<item_id>/ the moment it's received).
This deliberately avoids relying on Discord's CDN URLs, which are
signed/time-limited and can stop working once the original message
is deleted - which happens as part of this bot's normal "move item to
the next channel" flow.
"""
import asyncio
import base64
import json
import urllib.request
from pathlib import Path

import anthropic

import config

# Anthropic client is only created if a key is actually present - calling
# review_item() with the "anthropic" backend and no key will raise clearly
# rather than silently failing, but in normal operation the bot checks
# config.AI_ENABLED before invoking the AI step at all.
_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY) if config.ANTHROPIC_API_KEY else None

SYSTEM_PROMPT = """You are helping a small resale business turn a quick warehouse note \
into an accurate, honest resale listing draft. You will be shown one or more photos of a \
single physical item plus the short note the intake person wrote.

Your job:
1. Identify what the item actually appears to be (brand/model if visible or inferable).
2. Write a concise, honest listing title (under 80 characters).
3. Write a short listing description (2-4 sentences) based on what is ACTUALLY VISIBLE \
in the photos plus the submitted note. Do not invent condition details, functionality \
claims, or included accessories that are not visible or mentioned.
4. Flag any conflict between the submitted note and what the photo shows (e.g. note says \
"new" but photo shows visible wear; note says "complete" but a component looks missing).
5. If the item cannot be confidently identified from the photo, say so plainly rather than \
guessing.

Respond ONLY with valid JSON, no other text, in this exact shape:
{
  "identified_item": "short string",
  "suggested_title": "string",
  "suggested_description": "string",
  "flags": ["list of strings, empty list if nothing to flag"],
  "confidence": "high" | "medium" | "low"
}"""


def _fallback(raw_description: str, reason: str) -> dict:
    """Safe default so a single AI hiccup (either backend) never silently
    loses an item - it just falls through to the raw submitted note."""
    return {
        "identified_item": "Unknown - AI review failed",
        "suggested_title": (raw_description or "Untitled item")[:80],
        "suggested_description": raw_description or "No description provided.",
        "flags": [f"AI review failed ({reason}) - needs manual write-up"],
        "confidence": "low",
    }


def _strip_json_fences(text: str) -> str:
    # Strips accidental markdown code fences if a model adds them despite
    # being asked for raw JSON - both backends are prone to this.
    return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def _guess_media_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


async def review_item(photo_paths: list[str], raw_description: str) -> dict:
    """
    Reads the submitted photos from local disk, sends them plus the raw note
    to whichever backend config.AI_REVIEW_BACKEND selects, and returns a
    dict matching the JSON shape in SYSTEM_PROMPT.

    Should only be called when config.AI_ENABLED is True - the caller
    (item_flow.py) checks this before invoking the AI step at all.
    """
    if config.AI_REVIEW_BACKEND == "ollama":
        return await _review_item_ollama(photo_paths, raw_description)
    return await _review_item_anthropic(photo_paths, raw_description)


# --------------------------------------------------------------- anthropic --

def _image_block(image_bytes: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        },
    }


async def _review_item_anthropic(photo_paths: list[str], raw_description: str) -> dict:
    if _client is None:
        raise RuntimeError(
            "review_item() called with AI_REVIEW_BACKEND=anthropic but no ANTHROPIC_API_KEY "
            "is set. The bot should check config.AI_ENABLED before calling this."
        )

    content_blocks = []
    for path in photo_paths[:5]:  # cap at 5 images per item to control cost/latency
        try:
            data = Path(path).read_bytes()
            content_blocks.append(_image_block(data, _guess_media_type(path)))
        except Exception as e:
            # A missing/unreadable image shouldn't kill the whole review -
            # continue with whatever images are available.
            print(f"[ai_review] failed to read image {path}: {e}")

    content_blocks.append(
        {
            "type": "text",
            "text": f"Submitted note from intake: {raw_description or '(no note provided)'}",
        }
    )

    try:
        message = _client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        return json.loads(_strip_json_fences(text))
    except Exception as e:
        print(f"[ai_review] Anthropic review failed, returning fallback: {e}")
        return _fallback(raw_description, str(e))


# ------------------------------------------------------------------ ollama --

def _call_ollama(prompt: str, images_b64: list[str]) -> str:
    """
    Blocking HTTP call to Ollama's /api/generate endpoint. Run via
    asyncio.to_thread by _review_item_ollama so a slow local generation
    doesn't block the bot's event loop (and every other server it's in)
    the way a synchronous call here would. Uses urllib (stdlib) rather than
    adding a new HTTP dependency, since this is one simple local request.

    format="json" asks Ollama to constrain the output to valid JSON - it
    still isn't a guarantee with a small model like moondream, hence the
    fallback in _review_item_ollama if parsing fails anyway.
    """
    payload = json.dumps({
        "model": config.OLLAMA_VISION_MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "images": images_b64,
        "format": "json",
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # Local vision-model generation can take a while on modest hardware -
    # generous timeout rather than a fast fail into the fallback path.
    with urllib.request.urlopen(request, timeout=120) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body.get("response", "")


async def _review_item_ollama(photo_paths: list[str], raw_description: str) -> dict:
    images_b64 = []
    for path in photo_paths[:5]:  # same cap as the anthropic backend, for consistency
        try:
            images_b64.append(base64.b64encode(Path(path).read_bytes()).decode("utf-8"))
        except Exception as e:
            print(f"[ai_review] failed to read image {path}: {e}")

    prompt = f"Submitted note from intake: {raw_description or '(no note provided)'}"

    try:
        text = await asyncio.to_thread(_call_ollama, prompt, images_b64)
        return json.loads(_strip_json_fences(text))
    except Exception as e:
        print(f"[ai_review] Ollama review failed, returning fallback: {e}")
        return _fallback(raw_description, str(e))
