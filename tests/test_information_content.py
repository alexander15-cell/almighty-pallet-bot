"""
information_content.py's guide embeds (posted by /setup info-channel) - a
static-content regression guard, same idea as test_command_lengths.py:
Discord silently rejects an embed that exceeds its limits (256-char field
names, 1024-char field values, 25 fields, 6000 total chars per embed), so
this catches a future content edit that overflows one before it ever
reaches a live Discord API call.
"""
import discord

import information_content


def test_build_embeds_returns_a_nonempty_list_of_embeds():
    embeds = information_content.build_embeds()
    assert embeds
    assert all(isinstance(e, discord.Embed) for e in embeds)


def test_every_embed_field_is_within_discords_limits():
    for embed in information_content.build_embeds():
        assert len(embed.fields) <= 25, f"{embed.title!r} has {len(embed.fields)} fields (Discord caps at 25)"
        for field in embed.fields:
            assert 1 <= len(field.name) <= 256, f"{embed.title!r} field name {field.name!r} out of range"
            assert 1 <= len(field.value) <= 1024, f"{embed.title!r} field value for {field.name!r} out of range"


def test_every_embed_total_length_is_within_discords_limit():
    # Discord's combined limit across title + description + every field's
    # name/value + footer/author text is 6000 characters per embed.
    for embed in information_content.build_embeds():
        total = len(embed.title or "") + len(embed.description or "")
        for field in embed.fields:
            total += len(field.name) + len(field.value)
        if embed.footer:
            total += len(embed.footer.text or "")
        assert total <= 6000, f"{embed.title!r} totals {total} chars (Discord caps at 6000)"


def test_every_embed_has_a_title():
    for embed in information_content.build_embeds():
        assert embed.title
