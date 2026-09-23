"""ai_review.py - the fallback shape, the free-text category-guess prompt
(resolved against eBay's real taxonomy separately, see
tests/test_ebay_taxonomy.py and test_item_flow_ebay_category_search.py),
and the timeout path (a hung backend call must not block forever)."""
import asyncio
import time

import ai_review
import config


def test_fallback_has_expected_shape_and_null_suggestions():
    result = ai_review._fallback("a raw note", "some reason")
    assert result["suggested_title"] == "a raw note"
    assert result["suggested_category"] is None
    assert result["suggested_price"] is None
    assert "some reason" in result["flags"][0]


def test_fallback_has_null_weight_and_dimensions():
    # A failed AI call must never invent a weight/dimensions guess - those
    # feed eBay's Calculated shipping and have a real dollar cost if wrong,
    # so a fallback item should always need a human to fill these in.
    result = ai_review._fallback("a raw note", "some reason")
    assert result["estimated_weight_lb"] is None
    assert result["estimated_length_in"] is None
    assert result["estimated_width_in"] is None
    assert result["estimated_height_in"] is None


def test_system_prompt_asks_for_weight_and_dimension_estimate():
    prompt = ai_review._build_system_prompt()
    assert "estimated_weight_lb" in prompt
    assert "estimated_length_in" in prompt
    assert "estimated_width_in" in prompt
    assert "estimated_height_in" in prompt


def test_fallback_truncates_and_defaults_title():
    result = ai_review._fallback("", "reason")
    assert result["suggested_title"] == "Untitled item"

    long_note = "x" * 200
    result2 = ai_review._fallback(long_note, "reason")
    assert len(result2["suggested_title"]) == 80


def test_system_prompt_asks_for_free_text_category_not_a_fixed_list():
    # eBay's real taxonomy has ~18,000 leaf categories (ebay_taxonomy.py) -
    # far too many to hand the model as a fixed enum, and the old ~30-entry
    # hand-typed list this used to pick from is gone. The model should be
    # asked for a short descriptive phrase instead, resolved against the
    # real taxonomy afterward (see cogs/item_flow.py).
    prompt = ai_review._build_system_prompt()
    assert "suggested_category" in prompt
    assert "category search" in prompt.lower()


def test_review_item_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(config, "AI_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(config, "AI_REVIEW_BACKEND", "ollama")

    def _slow_call(prompt, images_b64):
        time.sleep(5)
        return '{"identified_item": "x"}'

    monkeypatch.setattr(ai_review, "_call_ollama", _slow_call)

    async def _timed_review():
        # Measured *inside* the running loop, not around asyncio.run() as a
        # whole - asyncio.run()'s own cleanup waits for the orphaned
        # background thread running _slow_call's time.sleep(5) to finish
        # (shutdown_default_executor), which is an asyncio.run()/test-harness
        # artifact, not something review_item's caller (the bot's long-lived
        # event loop) ever waits on in production.
        start = time.monotonic()
        result = await ai_review.review_item([], "a note")
        return time.monotonic() - start, result

    elapsed, result = asyncio.run(_timed_review())

    assert elapsed < 2.0, f"review_item should give up around the configured timeout, took {elapsed:.1f}s"
    assert "timed out" in result["flags"][0].lower()
    assert result["suggested_title"] == "a note"
