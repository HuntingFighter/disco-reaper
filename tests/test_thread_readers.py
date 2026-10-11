"""
get_threads(include_archived=...) on both readers (they share an interface, see AGENTS.md §4).
Threads are not part of the guild channel list in Discord; they are listed separately.
"""
import discord
import pytest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from src.core.backup_database import BackupDatabase
from src.core.backup_reader import BackupReader
from src.core.discord_reader import DiscordReader


def _thread_row(id, name, archived, parent_id=123, type=11):
    return {
        "id": id, "name": name, "type": type, "parent_id": parent_id, "message_count": 0, "member_count": 0,
        "archived": archived, "archive_timestamp": None, "auto_archive_duration": 1440, "locked": 0,
        "applied_tags": None,
    }


@pytest.fixture
def reader_with_threads(tmp_path):
    db = BackupDatabase(tmp_path / "backup.db")
    db._conn.execute("INSERT INTO guild_profile (id, name) VALUES (?, ?)", (1, "Guild"))
    db._conn.execute("INSERT INTO channels (id, name, type) VALUES (?, ?, ?)", (123, "general", 0))
    db._conn.commit()
    db.save_threads([
        _thread_row(1001, "active-thread", 0),
        _thread_row(1002, "archived-thread", 1),
        _thread_row(1003, "archived-forum-post", 1, type=11),
    ])
    reader = BackupReader(tmp_path)
    reader.db = db
    return reader


@pytest.mark.asyncio
async def test_backup_reader_get_threads_includes_archived(reader_with_threads):
    threads = await reader_with_threads.get_threads(True)
    assert sorted(t.id for t in threads) == [1001, 1002, 1003]


@pytest.mark.asyncio
async def test_backup_reader_get_threads_active_only(reader_with_threads):
    threads = await reader_with_threads.get_threads(include_archived=False)
    assert [t.id for t in threads] == [1001]

@pytest.mark.asyncio
async def test_backup_reader_get_threads_active_only_by_default(reader_with_threads):
    threads = await reader_with_threads.get_threads()
    assert [t.id for t in threads] == [1001]


def _archived(*threads):
    async def archived_threads(limit=100):
        assert limit is None  # must page through all archived threads, not just the first 100
        for t in threads:
            yield t
    return archived_threads


def _discord_reader(active, channels):
    reader = DiscordReader.__new__(DiscordReader)
    reader.guild = MagicMock()
    reader.guild.active_threads = AsyncMock(return_value=active)
    reader.guild.fetch_channels = AsyncMock(return_value=channels)
    return reader


@pytest.mark.asyncio
async def test_discord_reader_get_threads_merges_active_and_archived():
    active = [NS(id=1, name="active")]
    text = MagicMock(spec=discord.TextChannel)
    text.archived_threads = _archived(NS(id=2, name="archived"), NS(id=1, name="active-dup"))
    forum = MagicMock(spec=discord.ForumChannel)
    forum.archived_threads = _archived(NS(id=3, name="old-post"))
    voice = MagicMock(spec=discord.VoiceChannel)  # voice channels are skipped
    reader = _discord_reader(active, [text, forum, voice])

    threads = await reader.get_threads(True)

    assert [t.id for t in threads] == [1, 2, 3]
    assert threads[0].name == "active"  # duplicates keep the active entry


@pytest.mark.asyncio
async def test_discord_reader_get_threads_active_only_skips_archived_calls():
    text = MagicMock(spec=discord.TextChannel)
    text.archived_threads = MagicMock(side_effect=AssertionError("archived threads must not be fetched"))
    reader = _discord_reader([NS(id=1, name="active")], [text])

    threads = await reader.get_threads(include_archived=False)

    assert [t.id for t in threads] == [1]
    reader.guild.fetch_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_reader_get_threads_skips_forbidden_channel():
    async def forbidden(limit=100):
        raise discord.Forbidden(MagicMock(status=403), "Missing Access")
        yield  # pragma: no cover

    locked = MagicMock(spec=discord.TextChannel)
    locked.name, locked.id, locked.archived_threads = "staff", 9, forbidden
    ok = MagicMock(spec=discord.TextChannel)
    ok.archived_threads = _archived(NS(id=5, name="archived"))
    reader = _discord_reader([], [locked, ok])

    threads = await reader.get_threads(True )

    assert [t.id for t in threads] == [5]


# --- has_archived_threads (used to decide whether to warn before a migration) ---

@pytest.mark.asyncio
async def test_backup_reader_has_archived_threads(reader_with_threads, tmp_path):
    assert await reader_with_threads.has_archived_threads(123) is True
    assert await reader_with_threads.has_archived_threads(999) is False

    db = BackupDatabase(tmp_path / "active_only.db")
    db._conn.execute("INSERT INTO guild_profile (id, name) VALUES (?, ?)", (1, "Guild"))
    db._conn.commit()
    db.save_threads([_thread_row(1001, "active-thread", 0)])
    active_only = BackupReader(tmp_path)
    active_only.db = db
    assert await active_only.has_archived_threads(123) is False


@pytest.mark.asyncio
async def test_discord_reader_has_archived_threads_reads_one_entry():
    calls = []

    def archived(*items):
        async def gen(limit=100):
            calls.append(limit)
            for item in items:
                yield item
        return gen

    with_archive = MagicMock(spec=discord.TextChannel)
    with_archive.archived_threads = archived(NS(id=2), NS(id=3))
    empty = MagicMock(spec=discord.ForumChannel)
    empty.archived_threads = archived()
    voice = MagicMock(spec=discord.VoiceChannel)
    reader = _discord_reader([], [])
    channels = {1: with_archive, 2: empty, 3: voice}
    reader.get_channel = AsyncMock(side_effect=lambda cid: channels[cid])

    assert await reader.has_archived_threads(1) is True
    assert await reader.has_archived_threads(2) is False
    assert await reader.has_archived_threads(3) is False
    assert calls == [1, 1]
