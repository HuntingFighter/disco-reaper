"""Threads, forum posts and forum settings in the backup structure (exporter -> BackupDatabase -> BackupReader)."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.core.backup_database import BackupDatabase
from src.core.backup_reader import BackupReader
from src.core.exporter import DiscordExporter
from src.core.state import channel_record

TEXT_ID, FORUM_ID = 100, 200
THREAD_ID, POST_ID, UNTAGGED_POST_ID = 1001, 2001, 2002
TAG_ID = 5


def _text_channel():
    return SimpleNamespace(id=TEXT_ID, name="general", type=discord.ChannelType.text, position=0, category_id=None,
                           overwrites={}, topic="hi", nsfw=False, slowmode_delay=0, flags=discord.ChannelFlags(),
                           default_auto_archive_duration=4320, default_thread_slowmode_delay=0)


def _forum_channel():
    forum = MagicMock(spec=discord.ForumChannel)
    forum.id, forum.name, forum.type, forum.position, forum.category_id = FORUM_ID, "help", discord.ChannelType.forum, 1, None
    forum.overwrites, forum.topic, forum.nsfw, forum.slowmode_delay = {}, "Ask here", False, 0
    forum.available_tags = [SimpleNamespace(id=TAG_ID, name="bug", moderated=False, emoji=None)]
    forum.flags = discord.ChannelFlags(require_tag=True)
    forum.default_auto_archive_duration = 10080
    forum.default_thread_slowmode_delay = 30
    forum.default_sort_order = discord.ForumOrderType.creation_date
    forum.default_layout = discord.ForumLayoutType.gallery_view
    forum.default_reaction_emoji = discord.PartialEmoji(name="👍")
    return forum


def _thread(thread_id, parent_id, name, tags=(), archived=False, **extra):
    data = dict(id=thread_id, name=name, type=discord.ChannelType.public_thread, parent_id=parent_id, parent=None,
                message_count=3, member_count=2, archived=archived, archive_timestamp=None,
                auto_archive_duration=1440, locked=False, applied_tags=[SimpleNamespace(id=t) for t in tags],
                slowmode_delay=10, owner_id=777, invitable=True, flags=discord.ChannelFlags())
    data.update(extra)
    return SimpleNamespace(**data)


@pytest.fixture
def exporter(tmp_path):
    threads = [
        _thread(THREAD_ID, TEXT_ID, "side chat", archived=True),
        _thread(POST_ID, FORUM_ID, "crash on start", tags=[TAG_ID], flags=discord.ChannelFlags(pinned=True)),
        _thread(UNTAGGED_POST_ID, FORUM_ID, "untagged in bulk"),
    ]
    reader = MagicMock()
    reader.get_categories = AsyncMock(return_value=[])
    reader.get_channels = AsyncMock(return_value=[_text_channel(), _forum_channel()])
    reader.get_threads = AsyncMock(return_value=threads)
    # Bulk listings can miss forum tags; the exporter then fetches the single post
    reader.client.fetch_channel = AsyncMock(return_value=SimpleNamespace(applied_tags=[SimpleNamespace(id=TAG_ID)]))
    exp = DiscordExporter(reader, base_dir=tmp_path)
    exp.db = BackupDatabase(tmp_path / "backup.db")
    return exp


@pytest.mark.asyncio
async def test_structure_backup_saves_threads_of_all_channels(exporter):
    """No message export ran, yet every thread and forum post (archived too) is in the backup."""
    await exporter.export_channels_structure()

    exporter.reader.get_threads.assert_awaited_once_with(include_archived=True)
    threads = {t["id"]: t for t in exporter.db.get_all_threads()}
    assert set(threads) == {THREAD_ID, POST_ID, UNTAGGED_POST_ID}

    side = threads[THREAD_ID]
    assert (side["parent_id"], side["archived"], side["type"]) == (TEXT_ID, 1, 11)
    assert (side["slowmode_delay"], side["owner_id"], side["invitable"]) == (10, 777, 1)

    assert json.loads(threads[POST_ID]["applied_tags"]) == [str(TAG_ID)]
    assert threads[POST_ID]["flags"] == discord.ChannelFlags(pinned=True).value
    assert json.loads(threads[UNTAGGED_POST_ID]["applied_tags"]) == [str(TAG_ID)]  # from the single fetch


@pytest.mark.asyncio
async def test_structure_backup_saves_forum_settings(exporter):
    await exporter.export_channels_structure()

    forum = next(c for c in exporter.db.get_all_channels() if c["id"] == FORUM_ID)
    assert forum["type"] == 15
    assert forum["flags"] == discord.ChannelFlags(require_tag=True).value
    assert forum["default_auto_archive_duration"] == 10080
    assert forum["default_thread_slowmode_delay"] == 30
    assert forum["default_sort_order"] == discord.ForumOrderType.creation_date.value
    assert forum["default_layout"] == discord.ForumLayoutType.gallery_view.value
    assert json.loads(forum["default_reaction_emoji"]) == {"id": None, "name": "👍"}
    assert [t["name"] for t in forum["available_tags"]] == ["bug"]


@pytest.mark.asyncio
async def test_rebackup_drops_removed_forum_tags(exporter):
    await exporter.export_channels_structure()
    forum = _forum_channel()
    forum.available_tags = []  # all tags removed on Discord
    exporter.reader.get_channels = AsyncMock(return_value=[_text_channel(), forum])

    await exporter.export_channels_structure()

    assert exporter.db.get_forum_tags(str(FORUM_ID)) == []


@pytest.mark.asyncio
async def test_structure_backup_survives_thread_listing_failure(exporter):
    exporter.reader.get_threads = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))

    await exporter.export_channels_structure()

    assert len(exporter.db.get_all_channels()) == 2  # channels are still saved
    assert exporter.db.get_all_threads() == []


@pytest.mark.asyncio
async def test_backup_reader_exposes_backed_up_threads_and_forums(exporter, tmp_path):
    await exporter.export_channels_structure()

    reader = BackupReader(tmp_path)
    await reader.start()
    threads = {t.id: t for t in await reader.get_threads(include_archived=True)}
    assert set(threads) == {THREAD_ID, POST_ID, UNTAGGED_POST_ID}
    assert {t.id for t in await reader.get_threads()} == {POST_ID, UNTAGGED_POST_ID}  # active only
    assert await reader.has_archived_threads(TEXT_ID)

    post = threads[POST_ID]
    assert [t.name for t in post.applied_tags] == ["bug"]
    assert (post.slowmode_delay, post.owner_id, post.flags) == (10, 777, discord.ChannelFlags(pinned=True).value)

    forum = await reader.get_channel(FORUM_ID)
    assert forum.default_reaction_emoji.name == "👍" and forum.default_reaction_emoji.id is None
    assert forum.default_thread_slowmode_delay == 30

    # channel_mappings records built from backup objects carry the same thread / forum data as live ones
    rec = channel_record(threads[THREAD_ID])
    assert (rec["parent_id"], rec["archived"], rec["slowmode_delay"], rec["owner_id"]) == (TEXT_ID, 1, 10, 777)
    forum_rec = channel_record(forum)
    assert forum_rec["default_auto_archive_duration"] == 10080
    assert json.loads(forum_rec["default_reaction_emoji"]) == {"id": None, "name": "👍"}


def test_old_backup_gets_new_columns_and_keeps_rows(tmp_path):
    """Backups made before thread/forum settings were saved open fine; the new fields read as None."""
    db_path = tmp_path / "backup.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, type INTEGER, position INTEGER, "
                 "category_id INTEGER, topic TEXT, nsfw INTEGER, bitrate INTEGER, slowmode_delay INTEGER)")
    conn.execute("CREATE TABLE threads (id INTEGER PRIMARY KEY, name TEXT, type INTEGER, parent_id INTEGER, "
                 "message_count INTEGER, member_count INTEGER, archived INTEGER, archive_timestamp TEXT, "
                 "auto_archive_duration INTEGER, locked INTEGER, applied_tags TEXT)")
    conn.execute("INSERT INTO channels (id, name, type, position) VALUES (200, 'help', 15, 0)")
    conn.execute("INSERT INTO threads (id, name, type, parent_id, archived) VALUES (2001, 'post', 11, 200, 0)")
    conn.commit()
    conn.close()

    db = BackupDatabase(db_path)

    channel = db.get_all_channels()[0]
    assert channel["name"] == "help" and channel["default_layout"] is None
    thread = db.get_all_threads()[0]
    assert thread["name"] == "post" and thread["slowmode_delay"] is None
    # Re-opening doesn't try to add the columns again
    BackupDatabase(db_path)


def test_save_channels_and_threads_accept_partial_rows(tmp_path):
    db = BackupDatabase(tmp_path / "backup.db")
    db.save_channels([{"id": "10", "name": "cat", "type": 4, "position": 0}])
    db.save_threads([{"id": "11", "name": "t", "type": 11, "parent_id": "10"}])
    assert db.get_all_channels()[0]["flags"] is None
    assert db.get_all_threads()[0]["owner_id"] is None


def test_backup_threads_know_their_newest_message(tmp_path):
    """BackupThread.last_message_id mirrors discord.Thread.last_message_id (used to resume finished threads)."""
    db = BackupDatabase(tmp_path / "backup.db")
    db.save_threads([{"id": "11", "name": "t", "type": 11, "parent_id": "10"},
                     {"id": "12", "name": "empty", "type": 11, "parent_id": "10"}])
    db.save_messages_batch([{"id": i, "channel_id": 11, "author_id": 1, "content": "x", "timestamp": "2026-01-01",
                             "type": 0, "custom_display_name": None, "custom_avatar_url": None,
                             "message_reference": None, "is_pinned": 0, "extra_data": None} for i in (100, 105, 103)])
    reader = BackupReader(tmp_path)
    reader.db = db
    threads = {t.id: t for t in reader.threads}
    assert threads[11].last_message_id == 105
    assert threads[12].last_message_id is None


@pytest.mark.asyncio
async def test_backup_thread_messages_have_their_thread_as_channel(tmp_path):
    """F17: like discord.py, a thread message's channel is the thread (the waterfall skipped messages without one)."""
    db = BackupDatabase(tmp_path / "backup.db")
    db.save_threads([{"id": "11", "name": "t", "type": 11, "parent_id": "10"}])
    db.save_messages_batch([{"id": 100, "channel_id": 11, "author_id": 1, "content": "x", "timestamp": "2026-01-01",
                             "type": 0, "custom_display_name": None, "custom_avatar_url": None,
                             "message_reference": None, "is_pinned": 0, "extra_data": None}])
    reader = BackupReader(tmp_path)
    reader.db = db

    msg = await reader.get_message(11, 100)

    assert msg.channel is not None and msg.channel.id == 11 and msg.channel.parent_id == 10


def test_backup_roles_keep_bot_role_info_and_old_backups_migrate(tmp_path):
    """F25: roles.managed / roles.bot_id are added to existing backups; old rows read as regular roles."""
    path = tmp_path / "backup.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT, color INTEGER, position INTEGER, "
                "permissions INTEGER, hoist INTEGER, mentionable INTEGER)")
    old.execute("INSERT INTO roles VALUES (5, 'Old', 0, 1, 0, 0, 0)")
    old.commit(); old.close()

    db = BackupDatabase(path)
    db.save_roles([{"id": "6", "name": "Reaper", "color": 0, "position": 2, "permissions": 8, "hoist": 0,
                    "mentionable": 0, "managed": 1, "bot_id": "99"}])
    from src.core.backup_reader import BackupRole
    roles = {r["name"]: BackupRole(r) for r in db.get_all_roles()}

    assert roles["Reaper"].is_bot_managed() and roles["Reaper"].managed
    assert not roles["Old"].is_bot_managed() and not roles["Old"].managed
    assert BackupRole(None).is_bot_managed() is False
