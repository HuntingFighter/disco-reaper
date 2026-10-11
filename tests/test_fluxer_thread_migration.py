"""
Message migration into real Fluxer threads.

Thread messages used to be sent inline into the parent channel between "<<< THREAD >>>" / "END OF THREAD"
markers. They now go into a Fluxer thread (created on demand, or a forum post created with its first
message). The inline markers remain the fallback when the thread can't be created.
"""
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from src.core.state import MigrationState
from src.fluxer.migrate_message import migrate_messages, migrate_global_messages


@pytest.fixture
def state(tmp_path):
    s = MigrationState()
    s.set_folder("999", "ThreadTest", platform="fluxer", base_dir=tmp_path)
    s.set_channel_mapping("11", "501")   # text channel
    s.set_channel_mapping("13", "503")   # forum channel
    yield s
    s.db.close()


def _reader(history):
    """history: {source channel id: [messages]}"""
    reader = MagicMock()
    reader.db = object()       # backup-style reader: threads come from reader.threads / msg.thread
    reader.threads = []
    reader.guild = MagicMock()
    reader.fetch_channels = AsyncMock(return_value=[])
    reader.get_active_threads = AsyncMock(return_value=[])

    async def fetch_message_history(channel_id, after_id=None, inclusive=False):
        for m in history.get(int(channel_id), []):
            if after_id is None or m.id > after_id:
                yield m

    async def fetch_global_message_history(after_id=None):
        for m in sorted((m for msgs in history.values() for m in msgs), key=lambda m: m.id):
            yield m

    reader.fetch_message_history = fetch_message_history
    reader.fetch_global_message_history = fetch_global_message_history
    return reader


def _msg(reader, id, content, thread=None, channel=None):
    return NS(id=id, type=reader.MESSAGE_TYPE_DEFAULT, content=content, mentions=[], role_mentions=[],
              channel_mentions=[], attachments=[], embeds=[], stickers=[], reference=None, thread=thread,
              flags=NS(forwarded=False), author=NS(id=1, display_name="Alice", avatar=None),
              created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), jump_url=f"https://discord/{id}",
              channel=channel or NS(id=0))


def _thread(id, name, parent_id, type=11, archived=False, locked=False):
    return NS(id=id, name=name, parent_id=parent_id, type=NS(value=type), archived=archived, locked=locked,
              auto_archive_duration=4320, slowmode_delay=0)


def _context(state, reader, sent_ids):
    writer = MagicMock()
    writer.community_id = "999"
    writer.send_message = AsyncMock(side_effect=list(sent_ids))
    writer.send_marker = AsyncMock()
    writer.get_channels = AsyncMock(return_value=[{"id": "501", "type": 0}, {"id": "503", "type": 15}])
    writer.create_thread = AsyncMock(return_value="8888")
    writer.create_thread_from_message = AsyncMock(return_value="8889")
    writer.modify_thread = AsyncMock(return_value={})
    return NS(discord_reader=reader, fluxer_writer=writer, state=state, is_running=True,
              config=NS(anonymize_users=False))


def _sends(ctx):
    return [c.kwargs for c in ctx.fluxer_writer.send_message.await_args_list]


@pytest.mark.asyncio
async def test_thread_on_message_becomes_fluxer_thread_from_that_message(state):
    thread = _thread(12, "help", parent_id=11)
    reader = _reader({})
    reader_history = {11: [_msg(reader, 100, "question", thread=thread)], 12: [_msg(reader, 200, "answer")]}
    reader.fetch_message_history = _reader(reader_history).fetch_message_history
    ctx = _context(state, reader, ["f100", "f200"])

    stats = await migrate_messages(ctx, source_channel_id=11, target_channel_id="501")

    ctx.fluxer_writer.create_thread_from_message.assert_awaited_once_with(
        "501", "f100", "help", auto_archive_duration=4320, slowmode_delay=None)
    parent_send, thread_send = _sends(ctx)
    assert parent_send["thread_id"] is None
    assert thread_send["channel_id"] == "501"          # the parent owns the webhook
    assert thread_send["thread_id"] == "8889"
    assert "THREAD" not in thread_send["content"]
    assert thread_send["reply_to_message_id"] is None
    ctx.fluxer_writer.send_marker.assert_not_awaited()  # no END OF THREAD marker
    assert str(state.get_target_channel_id("12")) == "8889"
    assert str(state.get_thread_message_id("501", "12", "200")) == "f200"
    assert stats["messages"] == 2 and stats["threads"] == 1


@pytest.mark.asyncio
async def test_already_cloned_thread_is_reused(state):
    state.set_channel_mapping("12", "7000")
    thread = _thread(12, "help", parent_id=11)
    reader = _reader({})
    reader.fetch_message_history = _reader({12: [_msg(reader, 200, "answer")]}).fetch_message_history
    ctx = _context(state, reader, ["f200"])

    await migrate_messages(ctx, source_channel_id=12, target_channel_id="501", thread_id="12",
                           thread_name="help", thread=thread)

    ctx.fluxer_writer.create_thread.assert_not_awaited()
    ctx.fluxer_writer.create_thread_from_message.assert_not_awaited()
    assert _sends(ctx)[0]["thread_id"] == "7000"


@pytest.mark.asyncio
async def test_forum_post_is_created_by_its_first_message(state):
    post = _thread(20, "Bug report", parent_id=13)
    reader = _reader({})
    reader.fetch_message_history = _reader({20: [_msg(reader, 20, "it broke"), _msg(reader, 21, "same here")]}).fetch_message_history
    ctx = _context(state, reader, ["p1", "m2"])

    await migrate_messages(ctx, source_channel_id=20, target_channel_id="503", thread_id="20",
                           thread_name="Bug report", thread=post)

    first, second = _sends(ctx)
    assert (first["channel_id"], first["thread_name"], first["thread_id"]) == ("503", "Bug report", None)
    assert (second["thread_id"], second["thread_name"]) == ("p1", None)
    ctx.fluxer_writer.create_thread.assert_not_awaited()
    assert str(state.get_target_channel_id("20")) == "p1"


@pytest.mark.asyncio
async def test_falls_back_to_inline_markers_when_thread_creation_fails(state):
    thread = _thread(12, "help", parent_id=11)
    reader = _reader({})
    reader.fetch_message_history = _reader({12: [_msg(reader, 200, "answer")]}).fetch_message_history
    ctx = _context(state, reader, ["f200"])
    ctx.fluxer_writer.create_thread = AsyncMock(side_effect=RuntimeError("MISSING_PERMISSIONS"))

    stats = await migrate_messages(ctx, source_channel_id=12, target_channel_id="501", thread_id="12",
                                   thread_name="help", thread=thread)

    send = _sends(ctx)[0]
    assert send["thread_id"] is None
    assert "<<< THREAD: **help** >>>" in send["content"]
    assert stats["native_thread"] is False


@pytest.mark.asyncio
async def test_archived_and_locked_state_applied_after_messages(state):
    thread = _thread(12, "old", parent_id=11, archived=True, locked=True)
    reader = _reader({})
    reader.fetch_message_history = _reader({12: [_msg(reader, 200, "answer")]}).fetch_message_history
    ctx = _context(state, reader, ["f200"])

    await migrate_messages(ctx, source_channel_id=12, target_channel_id="501", thread_id="12",
                           thread_name="old", thread=thread)

    ctx.fluxer_writer.modify_thread.assert_awaited_once_with("8888", archived=True, locked=True)
    assert state.is_thread_completed("501", "12")


@pytest.mark.asyncio
async def test_waterfall_sends_thread_messages_into_fluxer_thread(state):
    state.set_channel_mapping("12", "7000")
    thread = _thread(12, "help", parent_id=11)
    reader = _reader({})
    reader.fetch_global_message_history = _reader({
        11: [_msg(reader, 100, "question", channel=NS(id=11, type=NS(value=0)))],
        12: [_msg(reader, 200, "answer", channel=thread)],
    }).fetch_global_message_history
    ctx = _context(state, reader, ["f100", "f200"])

    stats = await migrate_global_messages(ctx)

    parent_send, thread_send = _sends(ctx)
    # the Fluxer DB returns target IDs as int; writer routes stringify them
    assert (str(parent_send["channel_id"]), parent_send["thread_id"]) == ("501", None)
    assert (str(thread_send["channel_id"]), thread_send["thread_id"]) == ("501", "7000")
    assert str(state.get_thread_message_id("501", "12", "200")) == "f200"
    assert stats["threads"] == 1


def _forum_reader(posts, history):
    reader = _reader(history)
    reader.threads = posts
    reader.CHANNEL_TYPE_FORUM = NS(value=15)

    async def get_channel(channel_id):
        return NS(id=channel_id, type=NS(value=15 if channel_id == 13 else 0))

    reader.get_channel = get_channel
    return reader


@pytest.mark.asyncio
async def test_forum_channel_migrates_each_post_as_thread(state):
    """A forum has no messages of its own: each post is created on Fluxer by its first message."""
    posts = [_thread(31, "second", parent_id=13), _thread(30, "first", parent_id=13)]
    reader = _forum_reader(posts, {})
    reader.fetch_message_history = _reader({
        30: [_msg(reader, 30, "post one"), _msg(reader, 32, "reply one")],
        31: [_msg(reader, 31, "post two")],
    }).fetch_message_history
    ctx = _context(state, reader, ["p30", "m32", "p31"])

    stats = await migrate_messages(ctx, source_channel_id=13, target_channel_id="503")

    sends = _sends(ctx)
    assert [(s["channel_id"], s["thread_name"], s["thread_id"]) for s in sends] == [
        ("503", "first", None), ("503", None, "p30"), ("503", "second", None)]   # oldest post first
    ctx.fluxer_writer.send_marker.assert_not_awaited()
    assert str(state.get_target_channel_id("30")) == "p30"
    assert str(state.get_target_channel_id("31")) == "p31"
    assert stats["threads"] == 2 and stats["messages"] == 3


@pytest.mark.asyncio
async def test_forum_resume_skips_completed_posts(state):
    state.set_channel_mapping("30", "p30")
    state.update_thread_completed("503", "30", completed=True)
    posts = [_thread(30, "first", parent_id=13), _thread(31, "second", parent_id=13)]
    reader = _forum_reader(posts, {})
    reader.fetch_message_history = _reader({30: [_msg(reader, 30, "post one")],
                                            31: [_msg(reader, 31, "post two")]}).fetch_message_history
    ctx = _context(state, reader, ["p31"])

    await migrate_messages(ctx, source_channel_id=13, target_channel_id="503")

    assert [s["thread_name"] for s in _sends(ctx)] == ["second"]


@pytest.mark.asyncio
async def test_analyze_forum_counts_posts_not_starters_twice(state):
    from src.fluxer.migrate_message import analyze_migration
    posts = [_thread(30, "first", parent_id=13)]
    reader = _forum_reader(posts, {})
    reader.fetch_message_history = _reader({13: [_msg(reader, 30, "starter via forum history")],
                                            30: [_msg(reader, 30, "post one"), _msg(reader, 32, "reply")]}).fetch_message_history
    ctx = _context(state, reader, [])

    stats = await analyze_migration(ctx, source_channel_id=13)

    assert (stats["threads"], stats["messages"]) == (1, 2)


@pytest.mark.asyncio
async def test_thread_of_other_target_channel_is_sent_inline(state):
    """Picker target differs from the cloned parent: Fluxer would reject thread_id, so don't create/use a thread."""
    thread = _thread(12, "help", parent_id=11)   # cloned parent is 501
    reader = _reader({})
    reader.fetch_message_history = _reader({12: [_msg(reader, 200, "answer")]}).fetch_message_history
    ctx = _context(state, reader, ["f200"])

    await migrate_messages(ctx, source_channel_id=12, target_channel_id="600", thread_id="12",
                           thread_name="help", thread=thread)

    ctx.fluxer_writer.create_thread.assert_not_awaited()
    assert _sends(ctx)[0]["thread_id"] is None


@pytest.mark.asyncio
async def test_get_channel_threads_live_includes_active_posts():
    """discord.py's Guild.active_threads is a coroutine method; it used to be iterated uncalled."""
    from src.fluxer.migrate_message import get_channel_threads

    async def archived_threads(limit=None):
        yield NS(id=3, parent_id=13)

    active = [NS(id=1, parent_id=13), NS(id=2, parent_id=99)]
    reader = NS(guild=NS(active_threads=AsyncMock(return_value=active)),
                get_channel=AsyncMock(return_value=NS(archived_threads=archived_threads)))

    assert [t.id for t in await get_channel_threads(reader, 13)] == [1, 3]
