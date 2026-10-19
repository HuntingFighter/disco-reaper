"""
channel_mappings: one row per Discord category/channel/thread with its Discord data (no permissions)
and the target channel ID once the channel exists on the target.
"""
import json
import sqlite3
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from src.core.database import MigrationDatabase, CHANNEL_RECORD_COLUMNS
from src.core.state import MigrationState, channel_record

# Snowflake with a known timestamp: 2026-01-01T00:00:00Z
SNOWFLAKE_2026 = (int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000) - 1420070400000) << 22


def _cols(db):
    return {c[1]: c[2] for c in db._get_conn().execute("PRAGMA table_info(channel_mappings)").fetchall()}


@pytest.fixture
def state(tmp_path):
    s = MigrationState()
    s.set_folder("999", "RecordTest", platform="fluxer", base_dir=tmp_path)
    yield s
    s.db.close()


def test_table_schema_per_platform(tmp_path):
    fluxer = MigrationDatabase(tmp_path / "f.db", platform="fluxer")
    stoat = MigrationDatabase(tmp_path / "s.db", platform="stoat")

    cols = _cols(fluxer)
    assert cols["discord_channel_id"] == "INTEGER" and cols["target_channel_id"] == "INTEGER"
    assert set(CHANNEL_RECORD_COLUMNS) <= set(cols)
    assert _cols(stoat)["target_channel_id"] == "TEXT"
    pk = [c[1] for c in fluxer._get_conn().execute("PRAGMA table_info(channel_mappings)").fetchall() if c[5]]
    assert pk == ["discord_channel_id"]


def test_upsert_refreshes_data_but_keeps_target(tmp_path):
    db = MigrationDatabase(tmp_path / "f.db", platform="fluxer")
    db.upsert_channel_record({"discord_channel_id": "11", "name": "general", "type": 0})
    assert db.get_channel_record("11")["target_channel_id"] is None   # not created on the target yet

    db.set_channel_record_target("11", "501")
    db.upsert_channel_record({"discord_channel_id": "11", "name": "renamed", "type": 0})

    row = db.get_channel_record("11")
    assert (row["name"], str(row["target_channel_id"])) == ("renamed", "501")
    assert db.find_channel_record_by_target("501")["discord_channel_id"] == 11


def test_target_can_be_set_before_data_and_cleared(tmp_path):
    db = MigrationDatabase(tmp_path / "f.db", platform="fluxer")
    db.set_channel_record_target("11", "501")
    db.upsert_channel_record({"discord_channel_id": "11", "name": "general"})
    assert str(db.get_channel_record("11")["target_channel_id"]) == "501"

    db.set_channel_record_target("11", None)
    row = db.get_channel_record("11")
    assert row["target_channel_id"] is None and row["name"] == "general"   # data stays


def test_existing_database_gets_table_and_old_mappings(tmp_path):
    path = tmp_path / "old.db"
    MigrationDatabase(path, platform="fluxer").set_server_mapping("channel", "11", "501")
    conn = sqlite3.connect(path)   # simulate a database from before channel_mappings existed
    conn.execute("DROP TABLE channel_mappings")
    conn.execute("INSERT INTO server_mappings VALUES ('category', 10, 500)")
    conn.execute("INSERT INTO server_mappings VALUES ('role', 20, 600)")
    conn.commit()
    conn.close()

    db = MigrationDatabase(path, platform="fluxer")

    assert {r["discord_channel_id"]: r["target_channel_id"] for r in db.get_all_channel_records()} == {10: 500, 11: 501}


def test_existing_table_gets_new_columns(tmp_path):
    path = tmp_path / "partial.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE channel_mappings (discord_channel_id INTEGER PRIMARY KEY, target_channel_id INTEGER, name TEXT)")
    conn.execute("INSERT INTO channel_mappings VALUES (11, 501, 'general')")
    conn.commit()
    conn.close()

    db = MigrationDatabase(path, platform="fluxer")

    assert set(CHANNEL_RECORD_COLUMNS) <= set(_cols(db))
    assert db.get_channel_record("11")["name"] == "general"


def test_platform_switch_keeps_rows(tmp_path):
    path = tmp_path / "switch.db"
    db = MigrationDatabase(path, platform="fluxer")
    db.upsert_channel_record({"discord_channel_id": "11", "name": "general"})
    db.set_channel_record_target("11", "501")
    db.close()

    stoat = MigrationDatabase(path, platform="stoat")

    assert _cols(stoat)["target_channel_id"] == "TEXT"
    row = stoat.get_channel_record("11")
    assert (row["name"], row["target_channel_id"]) == ("general", "501")


def test_channel_record_text_channel():
    channel = NS(id=SNOWFLAKE_2026, name="general", type=NS(value=0), position=3, category_id=10, parent_id=10,
                 topic="hi", nsfw=True, slowmode_delay=5, flags=NS(value=0), default_auto_archive_duration=1440,
                 overwrites={"role": "deny"})

    record = channel_record(channel)

    assert record["discord_channel_id"] == str(SNOWFLAKE_2026)
    assert (record["name"], record["type"], record["position"], record["category_id"]) == ("general", 0, 3, 10)
    assert record["parent_id"] is None          # only threads have a parent channel
    assert (record["topic"], record["nsfw"], record["slowmode_delay"]) == ("hi", 1, 5)
    assert record["created_at"] == "2026-01-01T00:00:00+00:00"
    assert "overwrites" not in record           # no permissions
    assert set(record) - {"discord_channel_id"} <= set(CHANNEL_RECORD_COLUMNS)


def test_channel_record_forum_and_thread():
    tag = NS(id=7, name="bug", moderated=False, emoji=NS(id=None, name="🐛"))
    forum = NS(id=13, name="forum", type=NS(value=15), available_tags=[tag], default_reaction_emoji=NS(id=None, name="👍"),
               default_sort_order=NS(value=1), default_layout=NS(value=2))
    post = NS(id=20, name="post", type=NS(value=11), parent_id=13, archived=True, locked=False,
              auto_archive_duration=4320, applied_tags=[tag], message_count=4,
              archive_timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc))

    f, p = channel_record(forum), channel_record(post)

    assert json.loads(f["available_tags"]) == [{"id": "7", "name": "bug", "moderated": False, "emoji_id": None, "emoji_name": "🐛"}]
    assert json.loads(f["default_reaction_emoji"]) == {"id": None, "name": "👍"}
    assert (f["default_sort_order"], f["default_layout"]) == (1, 2)
    assert (p["parent_id"], p["archived"], p["locked"], p["auto_archive_duration"]) == (13, 1, 0, 4320)
    assert json.loads(p["applied_tags"]) == ["7"]
    assert p["archive_timestamp"] == "2026-02-01T00:00:00+00:00"


def test_state_mapping_setters_keep_target_column_current(state):
    state.record_source_channels([NS(id=10, name="cat", type=NS(value=4)), NS(id=11, name="general", type=NS(value=0))])

    state.set_category_mapping("10", "500")
    state.set_channel_mapping("11", "501")
    assert str(state.get_channel_record("10")["target_channel_id"]) == "500"
    assert str(state.get_channel_record("11")["target_channel_id"]) == "501"

    state.remove_channel_mapping("11")
    assert state.get_channel_record("11")["target_channel_id"] is None
    assert state.get_channel_record("11")["name"] == "general"

    state.clear_channel_mappings()
    assert all(r["target_channel_id"] is None for r in state.get_all_channel_records())


def test_record_source_channels_skips_unusable_objects(state):
    state.record_source_channels([NS(name="no id"), NS(id=11, name="general", type=NS(value=0))])
    assert [r["discord_channel_id"] for r in state.get_all_channel_records()] == [11]


def test_channel_record_thread_with_uncached_parent(state):
    """discord.Thread.category_id raises ClientException when the parent isn't cached (always, with REST-only reads)."""
    class UncachedThread:
        id, name, type, parent_id = 21, "help", NS(value=11), 11

        @property
        def category_id(self):
            raise RuntimeError("Parent channel not found")

    record = channel_record(UncachedThread())

    assert (record["name"], record["parent_id"], record["category_id"]) == ("help", 11, None)
    state.record_source_channels([UncachedThread()])
    assert state.get_channel_record("21")["name"] == "help"
