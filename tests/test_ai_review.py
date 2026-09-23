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


def test_system_prompt_excludes_fallback_only_categories():
    prompt = ai_review._build_system_prompt()
    for name in config.EBAY_CATEGORIES:
        if ("(top-level)" in name or "(parent/fallback)" in name
                or "(NOT A LEAF" in name or "(DUPLICATE ID" in name):
            assert name not in prompt, f"fallback-only/invalid category {name!r} should never be offered to the AI"
    # at least one real category should still be listed
    assert any(name in prompt for name in config.EBAY_CATEGORIES_FOR_AI_SUGGESTION)


def test_no_two_categories_share_the_same_id():
    # Discord's select menu flatly rejects two options with the same value
    # ("The specified option value is already used") - this once took down
    # EVERY item approval at once (see EBAY_CATEGORIES' "(DUPLICATE ID"
    # comment), not just the categories actually at fault. Any future
    # duplicate must be caught and tagged before it reaches a live select
    # menu again, not just defended against at runtime.
    ids = list(config.EBAY_CATEGORIES.values())
    duplicates = {cid for cid in ids if ids.count(cid) > 1}
    for category_id in duplicates:
        names = [name for name, cid in config.EBAY_CATEGORIES.items() if cid == category_id]
        assert all("(DUPLICATE ID" in name for name in names), (
            f"id {category_id!r} is shared by {names!r} but not all are tagged "
            f"'(DUPLICATE ID' - untag once corrected, or tag immediately if new"
        )


def test_categories_confirmed_not_a_leaf_are_quarantined():
    # "Electrical Supplies (general)" (259482) and "Hand Tools" (3244) both
    # failed a real eBay upload with error 87 ("category selected is not a
    # leaf category") - guards against either silently losing its
    # quarantine tag and being suggested/picked again before its ID is
    # actually corrected.
    for category_id in ("259482", "3244"):
        matching_names = [name for name, cid in config.EBAY_CATEGORIES.items() if cid == category_id]
        assert matching_names, f"category id {category_id} should still be present (just quarantined, not removed)"
        for name in matching_names:
            assert "(NOT A LEAF" in name, f"{name!r} (id {category_id}) confirmed failing but isn't quarantined"
            assert name not in config.EBAY_CATEGORIES_FOR_AI_SUGGESTION


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
