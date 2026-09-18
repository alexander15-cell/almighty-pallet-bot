"""
Calls Claude's vision-capable API to draft a listing description and flag
possible mismatches between the submitted description and what's visible
in the photos. This module NEVER modifies or regenerates the photos
themselves - only text is produced.

Photos are read from local disk (see item_flow.py, which saves every
submitted photo under data/photos/<item_id>/ the moment it's received).
This deliberately avoids relying on Discord's CDN URLs, which are
signed/time-limited and can stop working once the original message
is deleted - which happens as part of this bot's normal "move item to
the next channel" flow.
"""
import base64
import json
from pathlib import Path

import anthropic

import config

# Client is only created if a key is actually present - calling review_item()
# without one will raise clearly rather than silently failing, but in normal
# operation the bot checks config.AI_ENABLED first and never calls this at all.
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


def _image_block(image_bytes: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        },
    }


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
    to Claude, and returns a dict matching the JSON shape in SYSTEM_PROMPT.
    Falls back to a safe default if anything goes wrong so a single AI
    hiccup never silently loses an item.

    Should only be called when config.AI_ENABLED is True - the caller
    (item_flow.py) checks this before invoking the AI step at all.
    """
    if _client is None:
        raise RuntimeError(
            "review_item() called but no ANTHROPIC_API_KEY is set. "
            "The bot should check config.AI_ENABLED before calling this."
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
            model=config.AI_MODEL,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content_blocks}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        # Strip accidental markdown code fences if the model adds them
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(text)
        return parsed
    except Exception as e:
        print(f"[ai_review] AI review failed, returning fallback: {e}")
        return {
            "identified_item": "Unknown - AI review failed",
            "suggested_title": (raw_description or "Untitled item")[:80],
            "suggested_description": raw_description or "No description provided.",
            "flags": ["AI review failed - needs manual write-up"],
            "confidence": "low",
        }
