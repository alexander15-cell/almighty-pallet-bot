"""ai_review.py - the fallback shape, the category list fed to the model
(top-level/parent categories must never be offered as a suggestion), and
the timeout path (a hung backend call must not block forever)."""
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


def test_fallback_truncates_and_defaults_title():
    result = ai_review._fallback("", "reason")
    assert result["suggested_title"] == "Untitled item"

    long_note = "x" * 200
    result2 = ai_review._fallback(long_note, "reason")
    assert len(result2["suggested_title"]) == 80


def test_system_prompt_excludes_fallback_only_categories():
    prompt = ai_review._build_system_prompt()
    for name in config.EBAY_CATEGORIES:
        if "(top-level)" in name or "(parent/fallback)" in name:
            assert name not in prompt, f"fallback-only category {name!r} should never be offered to the AI"
    # at least one real category should still be listed
    assert any(name in prompt for name in config.EBAY_CATEGORIES_FOR_AI_SUGGESTION)


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
