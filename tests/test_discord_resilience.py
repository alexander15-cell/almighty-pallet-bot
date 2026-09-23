"""
discord_resilience.TRANSIENT_DISCORD_ERRORS - the shared exception tuple
that fixes a real bug: a bare `except discord.HTTPException` around a
best-effort Discord cleanup call doesn't catch a network-level failure
(DNS resolution failure, dropped connection, timeout), since none of
those are subclasses of discord.HTTPException. This left an item stuck
mid-review in production when a DNS blip hit during placeholder-message
cleanup. See discord_resilience.py's module docstring for the full story.
"""
import asyncio

import aiohttp
import discord

import discord_resilience as dr


def test_covers_discord_http_exception():
    class _FakeResponse:
        status = 500
        reason = "Internal Server Error"

    exc = discord.HTTPException(_FakeResponse(), "boom")
    assert isinstance(exc, dr.TRANSIENT_DISCORD_ERRORS)


def test_covers_dns_resolution_failure():
    exc = aiohttp.ClientConnectorDNSError(connection_key=None, os_error=OSError("getaddrinfo failed"))
    assert isinstance(exc, dr.TRANSIENT_DISCORD_ERRORS)


def test_covers_generic_connection_error():
    exc = aiohttp.ClientConnectorError(connection_key=None, os_error=OSError("connection refused"))
    assert isinstance(exc, dr.TRANSIENT_DISCORD_ERRORS)


def test_covers_server_disconnected():
    assert isinstance(aiohttp.ServerDisconnectedError(), dr.TRANSIENT_DISCORD_ERRORS)


def test_covers_asyncio_timeout():
    assert isinstance(asyncio.TimeoutError(), dr.TRANSIENT_DISCORD_ERRORS)
