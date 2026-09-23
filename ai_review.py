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

                Context window: Ollama's own default (2048 tokens) is easy
                to exceed once the system prompt, note, and an encoded image
                are all in one request. config.OLLAMA_NUM_CTX (default 4096)
                is passed as a per-request "options": {"num_ctx": ...} value
                on the plain configured model - confirmed working directly
                against Ollama's /api/generate. (An earlier version of this
                code tried baking num_ctx into a derived model instead; that
                added complexity, produced mangled model names on repeat
                runs, and wasn't actually needed - the request-level option
                works fine.) Raise OLLAMA_NUM_CTX in .env if you still see a
                "request (N tokens) exceeds the available context size"
                error, meaning images/prompts have grown past the window.

Both backends return the same dict shape (see SYSTEM_PROMPT_TEMPLATE) and
this module NEVER modifies or regenerates the photos themselves - only text
is produced.

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
import urllib.error
import urllib.request
from pathlib import Path

import anthropic

import config

# Anthropic client is only created if a key is actually present - calling
# review_item() with the "anthropic" backend and no key will raise clearly
# rather than silently failing, but in normal operation the bot checks
# config.AI_ENABLED before invoking the AI step at all.
_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY) if config.ANTHROPIC_API_KEY else None

# suggested_category (step 6 below) is a short free-text product-type guess,
# not a pick from a fixed list - eBay's real taxonomy has ~18,000 leaf
# categories (see ebay_categories.json/ebay_taxonomy.py), far too many to
# hand the model as an enum in every request. item_flow.py resolves this
# guess against the real taxonomy via ebay_taxonomy.search() after the
# model responds, so the model's job is just to describe the item type
# plainly - the keyword search (backed by eBay's own official category
# data) does the actual matching to a real, guaranteed-listable leaf ID.
SYSTEM_PROMPT_TEMPLATE = """You are helping a small resale business turn a quick warehouse note \
into an accurate, honest resale listing draft. You will be shown one or more photos of a \
single physical item plus the short note the intake person wrote.

The submitted note was written by someone who physically handled and examined this exact \
item - not a guess made from the photo alone. TRUST IT. If the note names a brand, character, \
edition, model, material, or any other identifying detail, treat that as a known fact and use \
it, even when the photo alone wouldn't be enough to confirm it independently (a photo often \
can't show a maker's mark, a tag on the underside, a stamp worn smooth, etc. - that doesn't \
make the claim wrong, it just means the camera didn't happen to catch it). The photo's job \
here is to confirm the item's presence/condition and to catch outright contradictions - it is \
NOT a checklist every claim in the note must independently pass before you'll believe it. \
"The photo doesn't show a marking confirming this" is never, by itself, a reason to doubt, \
hedge on, flag, or omit something the note told you.

Your job:
1. Identify what the item actually appears to be - START from what the note says (brand/model/ \
character/edition/etc. if the note gives it), then use the photo to confirm general appearance, \
condition, and completeness. Only depart from the note's stated identity if the photo actively \
and clearly shows something that contradicts it (e.g. note says "Marvel figure" but the photo \
clearly shows a DC logo) - not merely because the photo doesn't independently prove it too.
2. Write a concise, honest listing title (under 80 characters) that USES the identifying \
details the note gave you (brand, character/line name, edition, etc.) - don't write a vaguer, \
generic title just because the photo alone couldn't confirm those same details.
3. Write a short listing description (2-3 sentences) based on the note's stated facts plus \
what is visible in the photos, describing the item itself plainly and directly - the way a \
person would describe the physical item in front of them, not like a summary of a data \
source. NEVER write phrases like "per the intake note", "per the note", "as noted", \
"according to the submission", or any other reference to the note/submission process itself \
- just state what the item is and its condition as fact, including the identifying details \
the note gave you. Do not invent condition details, functionality claims, or included \
accessories that are neither visible nor mentioned in the note - "not invented" means not \
present in EITHER source, not "not independently visible in the photo." After the \
description, add exactly ONE short disclaimer sentence and nothing more - do not stack \
multiple caveats/reminders/hedges into separate sentences. Use this sentence verbatim as \
that one disclaimer: "Sold as-is; please review photos closely for exact condition."
4. Flag only a REAL, DIRECT conflict between the submitted note and what the photo clearly \
shows (e.g. note says "new" but photo shows visible wear; note says "complete" but a \
component looks missing; note says one character/brand but the photo clearly shows a \
different, identifiable one). Do NOT flag a note-stated detail just because the photo doesn't \
independently confirm it - that is expected and normal, not a discrepancy worth a human's \
attention. If the note provided a detail (brand, condition, contents, anything) and the photo \
neither confirms nor contradicts it, say nothing about it at all - do not manufacture \
uncertainty. Never claim no note was provided when the "Submitted note from intake" text in \
this request is non-empty.
5. If the note gives you nothing to go on AND the item truly cannot be identified from the \
photo either, say so plainly rather than guessing.
6. Describe what kind of product this is in a few plain words, suited for a category search \
(e.g. "cordless impact wrench", "ceiling fan", "kitchen faucet") - NOT eBay's own category \
naming, just a short, specific product-type phrase. Use null only if you truly cannot tell \
what kind of item it is at all.
7. Suggest a realistic USD starting price, based on your general knowledge of resale values \
for this kind of item. This is a rough estimate from general knowledge, NOT real market \
data - it always needs a human to confirm or adjust it before the item is actually listed.
8. Estimate the item's shipping weight in pounds, and its packaged dimensions (length, width, \
height in inches) - a rough visual estimate from the photo and your general knowledge of \
similar items, NOT an actual measurement. This is only ever a starting point for a human to \
weigh/measure and correct before it's used for real shipping calculations - never treat it as \
accurate. If you cannot make any reasonable estimate, use null instead of guessing wildly.

Respond ONLY with valid JSON, no other text, in this exact shape:
{
  "identified_item": "short string",
  "suggested_title": "string",
  "suggested_description": "string",
  "flags": ["list of strings, empty list if nothing to flag"],
  "confidence": "high" | "medium" | "low",
  "suggested_category": "short product-type phrase for a category search, or null",
  "suggested_price": number or null,
  "estimated_weight_lb": number or null,
  "estimated_length_in": number or null,
  "estimated_width_in": number or null,
  "estimated_height_in": number or null
}"""


def _build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE


# suggested_price (step 7 above) is a guess from the model's general
# training knowledge of resale values - it has no access to real market
# data and is never treated as authoritative anywhere downstream (Queue
# Review's price field just pre-fills with it, always human-editable before
# Approve). POSSIBLE FUTURE IMPROVEMENT: pull actual eBay "sold" comps via
# eBay's Browse API (item_summary/search with a sold/completed filter) for
# a real market-data-backed estimate instead of a general-knowledge guess -
# not implemented here, since it needs its own eBay API credentials/calls
# beyond what config.EBAY_ENABLED currently gates.

# estimated_weight_lb/estimated_*_in (step 8 above) are visual guesses from
# the photo, same "never authoritative" status as suggested_price - the
# eBay listing modal pre-fills these fields but always requires a human to
# confirm or correct them before Approve, since the Pirate Ship CSV export
# (pirate_ship_csv.py) uses this weight/dimensions to buy real shipping
# labels - a wildly wrong AI guess here has a real dollar cost, not just a
# cosmetic one.


def _fallback(raw_description: str, reason: str) -> dict:
    """Safe default so a single AI hiccup (either backend) never silently
    loses an item - it just falls through to the raw submitted note."""
    return {
        "identified_item": "Unknown - AI review failed",
        "suggested_title": (raw_description or "Untitled item")[:80],
        "suggested_description": raw_description or "No description provided.",
        "flags": [f"AI review failed ({reason}) - needs manual write-up"],
        "confidence": "low",
        "suggested_category": None,
        "suggested_price": None,
        "estimated_weight_lb": None,
        "estimated_length_in": None,
        "estimated_width_in": None,
        "estimated_height_in": None,
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
    dict matching the JSON shape in SYSTEM_PROMPT_TEMPLATE.

    Should only be called when config.AI_ENABLED is True - the caller
    (item_flow.py) checks this before invoking the AI step at all.
    """
    backend_call = _review_item_ollama if config.AI_REVIEW_BACKEND == "ollama" else _review_item_anthropic
    try:
        return await asyncio.wait_for(backend_call(photo_paths, raw_description), timeout=config.AI_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # The backend call already catches its own errors and returns a
        # fallback dict internally - this only fires if the whole call
        # (including Ollama's own inner socket timeout) took longer than
        # config.AI_TIMEOUT_SECONDS. The abandoned network call may still
        # finish in its background thread; its result is just discarded.
        print(f"[ai_review] {config.AI_REVIEW_BACKEND} review timed out after {config.AI_TIMEOUT_SECONDS:.0f}s, returning fallback")
        return _fallback(raw_description, f"timed out after {config.AI_TIMEOUT_SECONDS:.0f}s")


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


def _call_anthropic(content_blocks: list) -> str:
    """
    Blocking call to the Anthropic SDK (it has no native async client here).
    Run via asyncio.to_thread by _review_item_anthropic, same as the Ollama
    backend's own blocking HTTP call - without that, this would block the
    bot's entire event loop (every other command, every other item's
    review) for the whole duration of each cloud request.
    """
    message = _client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1000,
        system=_build_system_prompt(),
        messages=[{"role": "user", "content": content_blocks}],
    )
    return "".join(block.text for block in message.content if block.type == "text")


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
        text = await asyncio.to_thread(_call_anthropic, content_blocks)
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

    Request shape (model/prompt/images/options.num_ctx) matches a manual
    curl test against a live Ollama server that confirmed the plain
    configured model, with num_ctx passed as a per-request option, is
    enough - no custom/derived model needed.

    format="json" asks Ollama to constrain the output to valid JSON - it
    still isn't a guarantee with a small model like moondream, hence the
    fallback in _review_item_ollama if parsing fails anyway.
    """
    payload = json.dumps({
        "model": config.OLLAMA_VISION_MODEL,
        "system": _build_system_prompt(),
        "prompt": prompt,
        "images": images_b64,
        "format": "json",
        "stream": False,
        "options": {"num_ctx": config.OLLAMA_NUM_CTX},
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # This socket-level timeout is a backstop; the outer
        # asyncio.wait_for in review_item() (config.AI_TIMEOUT_SECONDS)
        # is what actually enforces the configured limit and returns the
        # fallback promptly, so both agree on the same number here.
        with urllib.request.urlopen(request, timeout=config.AI_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # urllib's default str(e) for an HTTPError is just "HTTP Error 400:
        # Bad Request" - it drops the response body, which is where Ollama
        # actually puts the useful part (e.g. {"error": "model 'x' not
        # found, try pulling it first"}). Read it explicitly so that ends
        # up in the console log / fallback flag instead of a bare status code.
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {e.code}: {error_body}") from e
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
