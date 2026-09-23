"""
Shared "this Discord call is best-effort, a transient failure shouldn't
crash whatever it's part of" exception tuple.

discord.HTTPException only covers actual Discord API error RESPONSES
(4xx/5xx) - a network-level failure before a response is even received
(a DNS lookup failure, a dropped connection, a timeout) raises straight
from aiohttp/asyncio instead, and none of those are subclasses of
discord.HTTPException:

    >>> issubclass(aiohttp.ClientConnectorDNSError, discord.HTTPException)
    False

A bare `except discord.HTTPException` around a best-effort cleanup call
(delete a placeholder message, pin a card, edit a status embed) therefore
does NOT actually catch a real-world network blip - it slips through,
aborts whatever the cleanup call was part of, and can leave an item/pallet
stuck mid-flow (this happened in practice: a DNS hiccup during
ItemFlow.run_ai_review's placeholder-message cleanup left an item's AI
review saved but never posted to Queue Review). Use TRANSIENT_DISCORD_ERRORS
in every `except` clause guarding a call like that instead of
`discord.HTTPException` alone.

This is deliberately NOT used around calls where a network failure SHOULD
propagate (e.g. the bot's actual gateway connection, or an operation whose
success the caller needs to know about) - only around the "try it, log it,
move on" cleanup/notification calls throughout the cogs.
"""
import asyncio

import aiohttp
import discord

TRANSIENT_DISCORD_ERRORS = (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError)
