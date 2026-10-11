"""
Tests for FluxerWriter's thread and forum methods.

fluxer.py has no thread routes, so FluxerWriter calls the plain REST API (https://docs.fluxer.app/http-api/threads/)
through fluxer.py's HTTPClient.request. The unit tests check the routes and bodies; the end-to-end tests run
the requests through the real HTTPClient against a local server.
"""
import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from multidict import CIMultiDict
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import src.fluxer.writer as writer_mod
from src.fluxer.writer import FluxerWriter

API = "https://api.test/v1"


@pytest.fixture
def writer(monkeypatch):
    async def fake_sleep(delay):
        pass

    monkeypatch.setattr(writer_mod.asyncio, "sleep", fake_sleep)
    w = FluxerWriter(token="t", community_id="1")
    w.bot = NS(_http=MagicMock(api_url=API))
    w._ready_event.set()
    return w


def call(writer, index=-1):
    """(method, url, kwargs) of a client.request call."""
    args = writer.client.request.await_args_list[index]
    route = args.args[0]
    return route.method, route.url, args.kwargs


@pytest.mark.asyncio
async def test_create_thread_posts_to_channel_threads(writer):
    writer.client.request = AsyncMock(return_value={"id": 77})

    assert await writer.create_thread("10", "help", auto_archive_duration=1440, slowmode_delay=5) == "77"

    method, url, kwargs = call(writer)
    assert (method, url) == ("POST", f"{API}/channels/10/threads")
    assert kwargs["json"] == {"name": "help", "type": 11, "auto_archive_duration": 1440, "rate_limit_per_user": 5}


@pytest.mark.asyncio
async def test_create_thread_sends_invitable_only_for_private_threads(writer):
    writer.client.request = AsyncMock(return_value={"id": 1})

    await writer.create_thread("10", "public", invitable=False)
    await writer.create_thread("10", "private", type=12, invitable=False)

    assert "invitable" not in call(writer, 0)[2]["json"]
    assert call(writer, 1)[2]["json"] == {"name": "private", "type": 12, "invitable": False}


@pytest.mark.asyncio
async def test_create_thread_retries_plain_429(writer):
    plain_429 = aiohttp.ContentTypeError(MagicMock(), (), status=429, message="text/plain",
                                         headers=CIMultiDict({"Retry-After": "1"}))
    writer.client.request = AsyncMock(side_effect=[plain_429, {"id": 5}])

    assert await writer.create_thread("10", "help") == "5"
    assert writer.client.request.await_count == 2


@pytest.mark.asyncio
async def test_create_thread_from_message(writer):
    writer.client.request = AsyncMock(return_value={"id": 8})

    assert await writer.create_thread_from_message("10", "99", "discussion") == "8"

    method, url, kwargs = call(writer)
    assert (method, url) == ("POST", f"{API}/channels/10/messages/99/threads")
    assert kwargs["json"] == {"name": "discussion"}


@pytest.mark.asyncio
async def test_create_forum_post_sends_first_message_and_caps_tags(writer):
    writer.client.request = AsyncMock(return_value={"id": 12})

    post_id = await writer.create_forum_post("20", "Bug report", "it broke",
                                             applied_tags=[1, 2, 3, 4, 5, 6])

    assert post_id == "12"
    method, url, kwargs = call(writer)
    assert (method, url) == ("POST", f"{API}/channels/20/threads")
    assert kwargs["json"] == {"name": "Bug report", "message": {"content": "it broke"},
                              "applied_tags": ["1", "2", "3", "4", "5"]}


@pytest.mark.asyncio
async def test_modify_thread_sends_only_given_fields(writer):
    writer.client.request = AsyncMock(return_value={"id": "7"})

    await writer.modify_thread("7", archived=True, locked=False)

    method, url, kwargs = call(writer)
    assert (method, url) == ("PATCH", f"{API}/channels/7")
    assert kwargs["json"] == {"archived": True, "locked": False}


@pytest.mark.asyncio
async def test_get_active_threads_uses_community(writer):
    writer.client.request = AsyncMock(return_value={"threads": [{"id": "1"}], "members": []})

    assert await writer.get_active_threads() == [{"id": "1"}]
    assert call(writer)[:2] == ("GET", f"{API}/guilds/1/threads/active")


@pytest.mark.asyncio
async def test_get_archived_threads_follows_pagination(writer):
    page1 = {"threads": [{"id": "1", "thread_metadata": {"archive_timestamp": "2026-01-02T00:00:00Z"}},
                         {"id": "2", "thread_metadata": {"archive_timestamp": "2026-01-01T00:00:00Z"}}],
             "has_more": True}
    page2 = {"threads": [{"id": "3", "thread_metadata": {"archive_timestamp": "2025-12-31T00:00:00Z"}}],
             "has_more": False}
    writer.client.request = AsyncMock(side_effect=[page1, page2])

    threads = await writer.get_archived_threads("10")

    assert [t["id"] for t in threads] == ["1", "2", "3"]
    assert call(writer, 0)[1] == f"{API}/channels/10/threads/archived/public"
    assert call(writer, 0)[2]["params"] == {"limit": 100}
    assert call(writer, 1)[2]["params"] == {"limit": 100, "before": "2026-01-01T00:00:00Z"}


@pytest.mark.asyncio
async def test_get_archived_private_threads_path(writer):
    writer.client.request = AsyncMock(return_value={"threads": [], "has_more": False})

    assert await writer.get_archived_threads("10", private=True) == []
    assert call(writer)[1] == f"{API}/channels/10/threads/archived/private"


@pytest.mark.asyncio
async def test_create_forum_tag_sends_one_emoji_field(writer):
    writer.client.request = AsyncMock(return_value={"id": "20", "available_tags": []})

    await writer.create_forum_tag("20", "bug", emoji_id=5, emoji_name="🐛")

    method, url, kwargs = call(writer)
    assert (method, url) == ("POST", f"{API}/channels/20/tags")
    assert kwargs["json"] == {"name": "bug", "moderated": False, "emoji_id": "5"}


# -- send_message into threads --

@pytest.fixture
def webhook(writer):
    hook = NS(id="w1", token="tok", send=AsyncMock())
    writer._get_or_create_webhook = AsyncMock(return_value=hook)
    return hook


@pytest.mark.asyncio
async def test_send_message_into_thread_uses_webhook_with_thread_id(writer, webhook):
    writer.client.request = AsyncMock(return_value={"id": "m1"})

    msg_id = await writer.send_message("10", "Alice", "hi", 1700000000,
                                       author_avatar_url="https://a/b.png", thread_id="55")

    assert msg_id == "m1"
    webhook.send.assert_not_awaited()
    writer._get_or_create_webhook.assert_awaited_once_with("10")  # the parent owns the webhook
    method, url, kwargs = call(writer)
    assert (method, url) == ("POST", f"{API}/webhooks/w1/tok")
    assert kwargs["params"] == {"wait": "true", "thread_id": "55"}
    assert kwargs["json"]["username"] == "Alice (discord)"
    assert kwargs["json"]["avatar_url"] == "https://a/b.png"
    assert kwargs["json"]["content"].endswith("hi")


@pytest.mark.asyncio
async def test_send_message_without_thread_keeps_webhook_send(writer, webhook):
    webhook.send.return_value = NS(id="m2")
    writer.client.request = AsyncMock()

    assert await writer.send_message("10", "Alice", "hi", 1700000000) == "m2"
    writer.client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_in_thread_goes_to_thread_via_bot(writer, webhook):
    writer.client.send_message = AsyncMock(return_value={"id": "m3"})

    assert await writer.send_message("10", "Alice", "hi", 1700000000,
                                      reply_to_message_id="m1", thread_id="55") == "m3"

    kwargs = writer.client.send_message.await_args.kwargs
    assert kwargs["channel_id"] == "55"
    assert kwargs["message_reference"] == {"message_id": "m1", "channel_id": "55"}


@pytest.mark.asyncio
async def test_forum_post_without_webhook_falls_back_to_bot_post(writer):
    writer._get_or_create_webhook = AsyncMock(return_value=None)
    writer.client.request = AsyncMock(return_value={"id": "p1"})

    assert await writer.send_message("20", "Alice", "first", 1700000000, thread_name="Bug report") == "p1"

    method, url, kwargs = call(writer)
    assert (method, url) == ("POST", f"{API}/channels/20/threads")
    assert kwargs["json"]["name"] == "Bug report"
    assert "Alice" in kwargs["json"]["message"]["content"]


# -- End to end through fluxer.py's real HTTPClient --

@pytest_asyncio.fixture
async def local_api():
    """Local server that records requests and answers with the next queued JSON body."""
    seen, replies = [], []

    async def handler(request):
        body = None
        if request.content_type == "application/json":
            body = await request.json()
        elif request.content_type.startswith("multipart/"):
            body = {}
            async for part in await request.multipart():
                body[part.name] = (part.filename, await part.read())
        seen.append((request.method, request.path, dict(request.query), body))
        return web.json_response(replies.pop(0))

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/v1", seen, replies
    await runner.cleanup()


@pytest.mark.asyncio
async def test_real_client_create_thread_and_forum_post_with_files(local_api):
    from fluxer import HTTPClient

    api_url, seen, replies = local_api
    replies += [{"id": "77"}, {"id": "p1"}]
    w = FluxerWriter(token="t", community_id="1")
    async with HTTPClient("t", api_url=api_url) as http:
        w.bot = NS(_http=http)
        w._ready_event.set()
        w._get_or_create_webhook = AsyncMock(return_value=NS(id="w1", token="tok"))

        assert await w.create_thread("10", "help") == "77"
        post_id = await w.send_message("20", "Alice", "first", 1700000000, thread_name="Bug report",
                                       files=[{"filename": "a.txt", "data": b"hello"}])

    assert post_id == "p1"
    assert seen[0] == ("POST", "/v1/channels/10/threads", {}, {"name": "help", "type": 11})
    method, path, query, parts = seen[1]
    assert (method, path, query) == ("POST", "/v1/webhooks/w1/tok", {"wait": "true"})
    assert parts["files[0]"] == ("a.txt", b"hello")
    import json
    payload = json.loads(parts["payload_json"][1])
    assert payload["thread_name"] == "Bug report"
    assert payload["attachments"] == [{"id": 0, "filename": "a.txt"}]
