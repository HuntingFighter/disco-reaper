"""
Tests for FluxerWriter's retry on 429s that fluxer.py cannot parse.

Fluxer sometimes answers with a 429 whose body is text/plain. fluxer.py calls resp.json() on it,
gets aiohttp.ContentTypeError, retries for ~14 s and re-raises, which aborted the batch clone.
FluxerWriter now retries those calls itself, honouring Retry-After.
"""
import aiohttp
import pytest
from multidict import CIMultiDict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import src.fluxer.writer as writer_mod
from src.fluxer.writer import FluxerWriter, RATE_LIMIT_MAX_RETRIES


def plain_429(headers=None):
    return aiohttp.ContentTypeError(
        MagicMock(), (), status=429,
        message="Attempt to decode JSON with unexpected mimetype: text/plain",
        headers=CIMultiDict(headers or {}),
    )


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(writer_mod.asyncio, "sleep", fake_sleep)
    return recorded


@pytest.fixture
def writer():
    w = FluxerWriter(token="t", community_id="1")
    w.bot = NS(_http=MagicMock())
    w._ready_event.set()
    return w


@pytest.mark.asyncio
async def test_modify_channel_retries_plain_429_using_retry_after(writer, sleeps):
    writer.client.modify_channel = AsyncMock(side_effect=[plain_429({"Retry-After": "7"}), {"id": "5"}])

    assert await writer.modify_channel("5", name="general") is True
    assert writer.client.modify_channel.await_count == 2
    assert sleeps == [7.0]


@pytest.mark.asyncio
async def test_reset_after_header_is_used_when_retry_after_missing(writer, sleeps):
    writer.client.create_guild_channel = AsyncMock(
        side_effect=[plain_429({"x-ratelimit-reset-after": "2.5"}), {"id": "42"}]
    )

    assert await writer.create_channel("general") == "42"
    assert sleeps == [2.5]


@pytest.mark.asyncio
async def test_plain_429_without_hint_backs_off_exponentially(writer, sleeps):
    writer.client.modify_channel = AsyncMock(side_effect=[plain_429(), plain_429(), plain_429(), {"id": "5"}])

    assert await writer.modify_channel("5") is True
    assert sleeps == [5.0, 10.0, 20.0]


@pytest.mark.asyncio
async def test_wait_is_capped(writer, sleeps):
    writer.client.modify_channel = AsyncMock(side_effect=[plain_429({"Retry-After": "3600"}), {"id": "5"}])

    await writer.modify_channel("5")
    assert sleeps == [writer_mod.RATE_LIMIT_MAX_WAIT]


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(writer, sleeps):
    writer.client.modify_channel = AsyncMock(side_effect=plain_429())

    with pytest.raises(aiohttp.ContentTypeError):
        await writer.modify_channel("5")
    assert writer.client.modify_channel.await_count == RATE_LIMIT_MAX_RETRIES + 1
    assert len(sleeps) == RATE_LIMIT_MAX_RETRIES


@pytest.mark.asyncio
async def test_other_response_errors_are_not_retried(writer, sleeps):
    err = aiohttp.ContentTypeError(MagicMock(), (), status=502, message="bad gateway", headers=CIMultiDict())
    writer.client.modify_channel = AsyncMock(side_effect=err)

    with pytest.raises(aiohttp.ContentTypeError):
        await writer.modify_channel("5")
    assert writer.client.modify_channel.await_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_create_role_succeeds_after_plain_429(writer, sleeps):
    # create_role swallows errors and returns "" - before the retry, a plain 429 lost the role
    writer.client.create_guild_role = AsyncMock(side_effect=[plain_429({"Retry-After": "1"}), {"id": "77"}])

    assert await writer.create_role("Mods", 0, False, False, 0) == "77"


@pytest.mark.asyncio
async def test_webhook_send_retry_uses_fresh_file_buffers(writer, sleeps):
    sent_files = []

    async def send(**kwargs):
        files = kwargs["files"]
        sent_files.append(files)
        contents = [f.fp.read() for f in files]  # an upload consumes the buffer
        if len(sent_files) == 1:
            raise plain_429({"Retry-After": "1"})
        assert contents == [b"payload"]
        return NS(id=900)

    writer._webhooks["10"] = NS(id=1, send=send)

    msg_id = await writer.send_message(
        "10", "alice", "hi", 0, files=[{"data": b"payload", "filename": "a.txt"}]
    )

    assert msg_id == "900"
    assert len(sent_files) == 2
    assert sent_files[0][0] is not sent_files[1][0]


@pytest.mark.asyncio
async def test_bot_reply_send_retries_with_files(writer, sleeps):
    calls = []

    async def send_message(**kwargs):
        calls.append(kwargs)
        data = kwargs["files"][0].fp.read()
        if len(calls) == 1:
            raise plain_429()
        assert data == b"img"
        assert kwargs["message_reference"] == {"message_id": "3", "channel_id": "10"}
        return {"id": "901"}

    writer.client.send_message = send_message

    msg_id = await writer.send_message(
        "10", "bob", "re", 0, files=[{"data": b"img", "filename": "i.png"}], reply_to_message_id="3"
    )

    assert msg_id == "901"
    assert len(calls) == 2
    assert sleeps == [5.0]


@pytest.mark.asyncio
async def test_danger_zone_delete_retries(writer, sleeps):
    writer.client.get_guild_channels = AsyncMock(return_value=[{"id": "1", "name": "a", "type": 0}])
    writer.client.delete_channel = AsyncMock(side_effect=[plain_429({"Retry-After": "1"}), None])

    assert await writer.delete_all_channels() == 1
    assert writer.client.delete_channel.await_count == 2


# --- Log rewrite of fluxer.py's internal retry of non-JSON 429s ---

import logging
from aiohttp import web
from multidict import CIMultiDictProxy
from yarl import URL


def _request_info(method="PATCH", url="https://api.fluxer.app/v1/channels/5"):
    return aiohttp.RequestInfo(URL(url), method, CIMultiDictProxy(CIMultiDict()), URL(url))


def test_fluxer_connection_error_for_plain_429_is_logged_as_rate_limit(caplog):
    exc = aiohttp.ContentTypeError(
        _request_info(), (), status=429,
        message="Attempt to decode JSON with unexpected mimetype: text/plain", headers=CIMultiDict(),
    )
    with caplog.at_level(logging.WARNING, logger="fluxer.http"):
        logging.getLogger("fluxer.http").warning("Connection error: %s, retrying (attempt %d)", exc, 2)

    assert caplog.messages == [
        "Fluxer: Rate limited (unparsed 429) on PATCH https://api.fluxer.app/v1/channels/5, retrying in 3.0s (attempt 2)"
    ]
    assert caplog.records[0].levelno == logging.WARNING


def test_other_fluxer_connection_errors_are_left_alone(caplog):
    exc = aiohttp.ClientConnectionError("connection reset")
    with caplog.at_level(logging.WARNING, logger="fluxer.http"):
        logging.getLogger("fluxer.http").warning("Connection error: %s, retrying (attempt %d)", exc, 1)

    assert caplog.messages == ["Connection error: connection reset, retrying (attempt 1)"]


@pytest.mark.asyncio
async def test_real_fluxer_client_plain_429_logs_rate_limit(caplog, monkeypatch):
    """End to end through fluxer.py's HTTPClient, so the filter tracks the library's real log format."""
    from fluxer import HTTPClient
    import fluxer.http as fluxer_http

    real_sleep = fluxer_http.asyncio.sleep

    async def no_wait(delay, *args, **kwargs):
        await real_sleep(0)

    responses = [
        lambda: web.Response(status=429, text="Too Many Requests", content_type="text/plain"),
        lambda: web.json_response({"id": "5"}),
    ]

    async def handler(request):
        return responses.pop(0)()

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(fluxer_http.asyncio, "sleep", no_wait)
    try:
        async with HTTPClient("t", api_url=f"http://127.0.0.1:{port}/v1") as http:
            with caplog.at_level(logging.WARNING, logger="fluxer.http"):
                assert await http.modify_channel(channel_id="5", name="general") == {"id": "5"}
    finally:
        await runner.cleanup()

    messages = [r.getMessage() for r in caplog.records if r.name == "fluxer.http"]
    assert messages == [
        f"Fluxer: Rate limited (unparsed 429) on PATCH http://127.0.0.1:{port}/v1/channels/5, retrying in 2.0s (attempt 1)"
    ]
    assert not any("mimetype" in m for m in messages)
