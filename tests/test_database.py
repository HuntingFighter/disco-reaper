import pytest
from src.core.backup_database import BackupDatabase


@pytest.fixture
def db():
    from tests.conftest import _log
    msg = "[DATA_SOURCE] UNIT_TEST: Using in-memory database"
    print(msg)
    _log(msg)
    return BackupDatabase(":memory:")


def test_db_initialization(db):
    res = db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_profile'"
    ).fetchone()
    assert res is not None
    assert res["name"] == "guild_profile"


def test_save_get_guild_profile(db):
    profile = {
        "id": "123456789",
        "name": "Test Server",
        "description": "Testing",
        "owner_id": "987654321",
        "ignore_channels": ["111", "222"],
    }
    db.set_guild_profile(profile)
    got = db.get_guild_profile()
    assert got["name"] == "Test Server"
    assert got["id"] == 123456789
    assert got["ignore_channels"] == ["111", "222"]


def test_save_get_roles(db):
    roles = [
        {"id": "1", "name": "Admin", "color": 0xFF0000, "position": 1, "permissions": 8, "hoist": True, "mentionable": True},
        {"id": "2", "name": "User",  "color": 0x0000FF, "position": 2, "permissions": 0, "hoist": False, "mentionable": False},
    ]
    db.save_roles(roles)
    got = db.get_all_roles()
    assert len(got) == 2
    assert {r["name"] for r in got} == {"Admin", "User"}


def test_save_get_channels(db):
    channels = [
        {"id": 10, "name": "general", "type": 0, "position": 0, "category_id": None,
         "topic": "Talk", "nsfw": 0, "bitrate": None, "slowmode_delay": None},
        {"id": 20, "name": "voice", "type": 2, "position": 1, "category_id": None,
         "topic": None, "nsfw": 0, "bitrate": 64000, "slowmode_delay": None},
    ]
    db.save_channels(channels)
    got = db.get_all_channels()
    assert len(got) == 2
    assert {c["name"] for c in got} == {"general", "voice"}


def test_save_messages_and_attachments(db):
    messages = [
        {
            "id": 101, "channel_id": 10, "author_id": 999, "content": "Hello",
            "timestamp": "2023-01-01T00:00:00Z", "type": 0,
            "message_reference": None, "is_pinned": 0, "extra_data": None,
            "custom_display_name": None, "custom_avatar_url": None,
            "attachments": [
                {"id": 1, "filename": "file.png", "size": 100,
                 "url": "http://cdn.test/file.png", "content_type": "image/png", "local_hash": "abc"}
            ],
        }
    ]
    db.save_messages_batch(messages)
    msg = db._conn.execute("SELECT content FROM messages WHERE id=101").fetchone()
    assert msg["content"] == "Hello"
    att = db._conn.execute("SELECT filename FROM attachments WHERE message_id=101").fetchone()
    assert att["filename"] == "file.png"


def test_get_last_message_id(db):
    msgs = [
        {"id": 200, "channel_id": 10, "author_id": 1, "content": "a",
         "timestamp": "2023-01-01T00:00:00Z", "type": 0,
         "custom_display_name": None, "custom_avatar_url": None,
         "message_reference": None, "is_pinned": 0, "extra_data": None},
        {"id": 201, "channel_id": 10, "author_id": 1, "content": "b",
         "timestamp": "2023-01-01T00:01:00Z", "type": 0,
         "custom_display_name": None, "custom_avatar_url": None,
         "message_reference": None, "is_pinned": 0, "extra_data": None},
    ]
    db.save_messages_batch(msgs)
    assert db.get_last_message_id("10") == 201


def test_stats_by_channel(db):
    msgs = [
        {"id": 101, "channel_id": 10, "author_id": 1, "content": "Hi",
         "timestamp": "2023-01-01T00:00:00Z", "type": 0,
         "custom_display_name": None, "custom_avatar_url": None,
         "message_reference": None, "is_pinned": 0, "extra_data": None},
        {"id": 102, "channel_id": 10, "author_id": 1, "content": "Bye",
         "timestamp": "2023-01-01T00:01:00Z", "type": 0,
        "custom_display_name": None, "custom_avatar_url": None,
         "message_reference": None, "is_pinned": 0, "extra_data": None},
    ]
    db.save_messages_batch(msgs)
    stats = db.get_stats_by_channel()
    assert stats[10]["message_count"] == 2


def test_save_threads(db):
    threads = [
        {"id": 300, "name": "thread-1", "type": 11, "parent_id": 10,
         "message_count": 5, "member_count": 2, "archived": 0,
         "archive_timestamp": None, "auto_archive_duration": 1440,
         "locked": 0, "applied_tags": None},
    ]
    db.save_threads(threads)
    got = db.get_threads_by_parent("10")
    assert len(got) == 1
    assert got[0]["name"] == "thread-1"


def test_media_pool(db):
    db.add_media_to_pool("hash123", "/path/file.png", 512, "image/png", "http://cdn.test/file.png")
    db._conn.commit()
    entry = db.get_media_by_hash("hash123")
    assert entry is not None
    assert entry["local_path"] == "/path/file.png"


def test_get_messages_paged_after_id(db):
    msgs = [
        {"id": i, "channel_id": 10, "author_id": 1, "content": f"msg{i}",
         "timestamp": f"2023-01-01T00:0{i}:00Z", "type": 0,
         "custom_display_name": None, "custom_avatar_url": None,
         "message_reference": None, "is_pinned": 0, "extra_data": None}
        for i in range(5)
    ]
    db.save_messages_batch(msgs)
    page = db.get_messages_paged("10", limit=3, offset=0)
    assert len(page) == 3
    page_after = db.get_messages_paged("10", limit=10, after_id="2")
    assert all(m["id"] > 2 for m in page_after)


def _perm(channel_id, target_id, allow=0, deny=0):
    return {"channel_id": str(channel_id), "target_id": str(target_id), "target_type": "role",
            "allow": allow, "deny": deny}


def _perm_rows(db):
    return [tuple(r) for r in db._conn.execute(
        "SELECT channel_id, target_id, allow, deny FROM permissions ORDER BY channel_id, target_id").fetchall()]


def test_save_permissions_rebackup_replaces_instead_of_duplicating(db):
    db.save_permissions([_perm(10, 1, allow=1024), _perm(10, 2, deny=1024)], channel_ids=["10"])
    # Re-backup: role 2's overwrite was removed and role 1's changed on Discord
    db.save_permissions([_perm(10, 1, allow=2048)], channel_ids=["10"])
    assert _perm_rows(db) == [(10, 1, 2048, 0)]


def test_save_permissions_clears_channels_without_overwrites(db):
    db.save_permissions([_perm(10, 1, allow=1024), _perm(20, 1, deny=1024)], channel_ids=["10", "20"])
    # Channel 10 has no overwrites any more; it's still listed as backed up
    db.save_permissions([_perm(20, 1, deny=1024)], channel_ids=["10", "20"])
    assert _perm_rows(db) == [(20, 1, 0, 1024)]


def test_save_permissions_keeps_other_channels(db):
    db.save_permissions([_perm(10, 1, allow=1024), _perm(20, 1, deny=1024)])
    # Without channel_ids only the channels in the batch are replaced
    db.save_permissions([_perm(10, 3, allow=8)])
    assert _perm_rows(db) == [(10, 3, 8, 0), (20, 1, 0, 1024)]


def test_get_all_channels_overwrites_not_duplicated_after_rebackup(db):
    db.save_channels([{"id": "10", "name": "general", "type": 0, "position": 0, "category_id": None,
                       "topic": None, "nsfw": 0, "bitrate": None, "slowmode_delay": None}])
    for _ in range(3):
        db.save_permissions([_perm(10, 1, allow=1024)], channel_ids=["10"])
    channel = db.get_all_channels()[0]
    assert len(channel["overwrites"]) == 1
