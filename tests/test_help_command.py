"""
/help (cogs/help.py + help_content.py) - lets someone unsure which command
they need search by what they're trying to do instead of already knowing
the command name. Covers: the search ranking itself, Discord's embed-field
limits on the static entry data (same guard idea as test_information_content.py),
and the command's three response shapes (no query, matches found, no matches).
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import help_content
from cogs.help import Help


def _interaction():
    return SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def test_every_entry_is_within_discords_embed_field_limits():
    for entry in help_content.ENTRIES:
        assert 1 <= len(entry.usage) <= 256, f"{entry.usage!r} usage out of range for an embed field name"
        assert 1 <= len(entry.description) <= 1024, f"{entry.usage!r} description out of range for an embed field value"
        assert entry.keywords, f"{entry.usage!r} has no keywords - would never be found by search()"


def test_a_result_page_never_exceeds_discords_25_field_cap():
    # search() defaults to 8, but a future caller passing a bigger limit
    # shouldn't be able to silently build an embed Discord would reject.
    assert len(help_content.search("pallet", limit=30)) <= 25


def test_blank_query_returns_no_matches():
    assert help_content.search("") == []
    assert help_content.search("   ") == []


def test_nonsense_query_returns_no_matches():
    assert help_content.search("qwertyuiop zzz nonsense") == []


def test_submit_invoice_surfaces_the_submit_invoices_channel_first():
    results = help_content.search("submit invoice")
    assert results
    assert results[0].usage == "Post in #submit-invoices"


def test_delete_a_pallet_surfaces_pallet_delete_first():
    # A previous scoring approach let "pallet" 's sheer repetition across
    # OTHER entries' own keyword phrases outrank the actually-relevant
    # /pallet delete entry - this is the regression guard for that.
    results = help_content.search("delete a pallet")
    assert results
    assert results[0].usage == "/pallet delete <pallet_name> [confirm]"


def test_confirm_invoice_quickbooks_surfaces_the_confirm_button_first():
    results = help_content.search("confirm invoice quickbooks")
    assert results
    assert "Confirm logged in QuickBooks" in results[0].usage


def test_search_respects_the_limit():
    results = help_content.search("pallet", limit=3)
    assert len(results) <= 3


def test_help_command_with_no_query_points_at_how_to_search():
    cog = Help(bot=SimpleNamespace())
    interaction = _interaction()

    asyncio.run(cog.help_command.callback(cog, interaction, None))

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert "query" in embed.description
    assert "#information" in embed.description


def test_help_command_with_a_matching_query_lists_results():
    cog = Help(bot=SimpleNamespace())
    interaction = _interaction()

    asyncio.run(cog.help_command.callback(cog, interaction, "submit invoice"))

    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert embed.fields
    assert embed.fields[0].name == "Post in #submit-invoices"


def test_help_command_with_no_matches_suggests_alternatives():
    cog = Help(bot=SimpleNamespace())
    interaction = _interaction()

    asyncio.run(cog.help_command.callback(cog, interaction, "qwertyuiop zzz nonsense"))

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs["ephemeral"] is True
    assert "#information" in args[0]
