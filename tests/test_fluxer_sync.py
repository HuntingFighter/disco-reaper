"""
Regression tests for the Fluxer sync_*_state functions.

On Fluxer the migration DB returns target IDs as int, while the Fluxer API returns IDs as str.
The sync functions used to compare the two directly, which dropped every valid mapping and made
"Start Cloning" / "Start Syncing" re-create (duplicate) existing channels, roles, emojis and stickers.
"""
import pytest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from src.core.state import MigrationState
from src.fluxer.clone_server import sync_channel_state, migrate_channels, changed_channel_ids, find_deleted_channels
from src.fluxer.danger_zone import danger_delete_removed_channels
from src.fluxer.roles_permissions import sync_roles_state
from src.fluxer.emoji_stickers import sync_assets_state


@pytest.fixture
def fluxer_state(tmp_path):
    state = MigrationState()
    state.set_folder("999", "SyncTest", platform="fluxer", base_dir=tmp_path)
    yield state
    state.db.close()


def _category(id, name, position=0):
    return NS(id=id, name=name, position=position)


def _channel(id, name, category_id=None, position=0):
    return NS(id=id, name=name, category_id=category_id, position=position,
              type=NS(value=0), topic="", nsfw=False, slowmode_delay=0)

def _thread(id, name, category_id=None, position=0, parent=None):
    return NS(id=id, name=name, category_id=category_id, position=position,
              type=NS(value=0), topic="", nsfw=False, slowmode_delay=0, parent_id=parent)


def _channel_context(state, categories, channels, threads, target_channels, target_threads=()):
    reader = NS(
        get_categories=AsyncMock(return_value=categories),
        get_channels=AsyncMock(return_value=channels),
        get_threads=AsyncMock(return_value=threads),
        CHANNEL_TYPE_FORUM=NS(value=15),
        CHANNEL_TYPE_VOICE=NS(value=2),
        CHANNEL_TYPE_TEXT=NS(value=0),
        CHANNEL_TYPE_NEWS=NS(value=5),
    )
    writer = NS(
        get_channels=AsyncMock(return_value=target_channels),
        create_channel=AsyncMock(return_value="9999"),
        modify_channel=AsyncMock(return_value=True),
        reorder_channels=AsyncMock(return_value=None),
        get_active_threads=AsyncMock(return_value=list(target_threads)),
        get_archived_threads=AsyncMock(return_value=[]),
        create_thread=AsyncMock(return_value="8888"),
        create_thread_from_message=AsyncMock(return_value="8889"),
    )
    return NS(discord_reader=reader, sync_reader=reader, fluxer_writer=writer, state=state, is_running=True)


def _asset_context(state, roles=(), emojis=(), stickers=(), t_roles=(), t_emojis=(), t_stickers=()):
    reader = NS(
        get_roles=AsyncMock(return_value=list(roles)),
        get_emojis=AsyncMock(return_value=list(emojis)),
        get_stickers=AsyncMock(return_value=list(stickers)),
    )
    client = NS(
        get_guild_roles=AsyncMock(return_value=list(t_roles)),
        get_guild_emojis=AsyncMock(return_value=list(t_emojis)),
        get_guild_stickers=AsyncMock(return_value=list(t_stickers)),
    )
    return NS(discord_reader=reader, sync_reader=reader, fluxer_writer=NS(client=client), state=state,
              config=NS(fluxer_server_id="999"))


# --- Channels & categories ---

@pytest.mark.asyncio
async def test_sync_channel_state_keeps_existing_mappings(fluxer_state):
    fluxer_state.set_category_mapping("10", "500")
    fluxer_state.set_channel_mapping("11", "501")
    ctx = _channel_context(
        fluxer_state,
        categories=[_category(10, "cat")],
        channels=[_channel(11, "general", category_id=10)],
        threads=[_thread(12, "general", category_id=10, parent=11)],
        target_channels=[{"id": "500", "name": "cat", "type": 4},
                         {"id": "501", "name": "general", "type": 0, "parent_id": "500"}],
    )
    await sync_channel_state(ctx)
    assert str(fluxer_state.get_category_mapping("10")) == "500"
    assert str(fluxer_state.get_target_channel_id("11")) == "501"


@pytest.mark.asyncio
async def test_sync_channel_state_removes_deleted_targets(fluxer_state):
    fluxer_state.set_category_mapping("10", "500")
    fluxer_state.set_channel_mapping("11", "501")
    ctx = _channel_context(
        fluxer_state,
        categories=[_category(10, "cat")],
        channels=[_channel(11, "general", category_id=10)],
        threads=[_thread(12, "general", category_id=10, parent=11)],
        target_channels=[],
    )
    await sync_channel_state(ctx)
    assert fluxer_state.get_category_mapping("10") is None
    assert fluxer_state.get_target_channel_id("11") is None


@pytest.mark.asyncio
async def test_sync_channel_state_matches_channel_by_name_under_mapped_category(fluxer_state):
    fluxer_state.set_category_mapping("10", "500")
    ctx = _channel_context(
        fluxer_state,
        categories=[_category(10, "cat")],
        channels=[_channel(11, "general", category_id=10)],
        threads=[_thread(12, "general", category_id=10, parent=11)],
        target_channels=[{"id": "500", "name": "cat", "type": 4},
                         {"id": "501", "name": "general", "type": 0, "parent_id": "500"}],
    )
    await sync_channel_state(ctx)
    assert str(fluxer_state.get_target_channel_id("11")) == "501"


@pytest.mark.asyncio
async def test_reclone_creates_no_duplicates(fluxer_state):
    """End to end: sync + migrate_channels on an already cloned server creates nothing new."""
    fluxer_state.set_category_mapping("10", "500")
    fluxer_state.set_channel_mapping("11", "501")
    ctx = _channel_context(
        fluxer_state,
        categories=[_category(10, "cat")],
        channels=[_channel(11, "general", category_id=10)],
        threads=[_thread(12, "general", category_id=10, parent=11)],
        target_channels=[{"id": "500", "name": "cat", "type": 4},
                         {"id": "501", "name": "general", "type": 0, "parent_id": "500"}],
        target_threads=[{"id": "502", "name": "general", "type": 11, "parent_id": "501"}],
    )
    await sync_channel_state(ctx)
    result = await migrate_channels(ctx, force=False)
    ctx.fluxer_writer.create_channel.assert_not_called()
    ctx.fluxer_writer.create_thread.assert_not_called()
    assert str(fluxer_state.get_target_channel_id("12")) == "502"
    assert result["categories_created"] == []
    assert result["channels_created"] == []
    assert result["channels_synced"] == ["general"]


# --- Roles ---

@pytest.mark.asyncio
async def test_sync_roles_state_keeps_existing_mapping(fluxer_state):
    fluxer_state.set_role_mapping("20", "600")
    ctx = _asset_context(fluxer_state, roles=[NS(id=20, name="Mod")],
                         t_roles=[{"id": "600", "name": "Mod"}])
    await sync_roles_state(ctx)
    assert str(fluxer_state.get_role_mapping("20")) == "600"


@pytest.mark.asyncio
async def test_sync_roles_state_removes_deleted_target(fluxer_state):
    fluxer_state.set_role_mapping("20", "600")
    ctx = _asset_context(fluxer_state, roles=[NS(id=20, name="Mod")], t_roles=[])
    await sync_roles_state(ctx)
    assert fluxer_state.get_role_mapping("20") is None


# --- Emojis & stickers ---

@pytest.mark.asyncio
async def test_sync_assets_state_keeps_existing_mappings(fluxer_state):
    fluxer_state.set_emoji_mapping("30", "700")
    fluxer_state.set_sticker_mapping("40", "800")
    ctx = _asset_context(fluxer_state,
                         emojis=[NS(id=30, name="pog")], stickers=[NS(id=40, name="wave")],
                         t_emojis=[{"id": "700", "name": "pog"}], t_stickers=[{"id": "800", "name": "wave"}])
    await sync_assets_state(ctx)
    assert str(fluxer_state.get_emoji_mapping("30")) == "700"
    assert str(fluxer_state.get_sticker_mapping("40")) == "800"


@pytest.mark.asyncio
async def test_sync_assets_state_removes_deleted_targets(fluxer_state):
    fluxer_state.set_emoji_mapping("30", "700")
    fluxer_state.set_sticker_mapping("40", "800")
    ctx = _asset_context(fluxer_state,
                         emojis=[NS(id=30, name="pog")], stickers=[NS(id=40, name="wave")])
    await sync_assets_state(ctx)
    assert fluxer_state.get_emoji_mapping("30") is None
    assert fluxer_state.get_sticker_mapping("40") is None


# --- Threads ---

def _cloned_ctx(state, threads, target_threads=(), channels=None, target_channels=None):
    """Category 10 -> channel 11 already cloned as 500 -> 501."""
    state.set_category_mapping("10", "500")
    state.set_channel_mapping("11", "501")
    return _channel_context(
        state,
        categories=[_category(10, "cat")],
        channels=channels or [_channel(11, "general", category_id=10)],
        threads=threads,
        target_channels=target_channels or [{"id": "500", "name": "cat", "type": 4},
                                            {"id": "501", "name": "general", "type": 0, "parent_id": "500"}],
        target_threads=target_threads,
    )


@pytest.mark.asyncio
async def test_migrate_channels_creates_missing_threads_under_parent(fluxer_state):
    thread = NS(id=12, name="help", parent_id=11, type=NS(value=12), auto_archive_duration=1440,
                slowmode_delay=0, archived=False, locked=False)
    ctx = _cloned_ctx(fluxer_state, threads=[thread])

    result = await migrate_channels(ctx)

    ctx.fluxer_writer.create_thread.assert_awaited_once_with(
        "501", "help", type=12, auto_archive_duration=1440, slowmode_delay=None)
    assert result["threads_created"] == ["help"]
    assert str(fluxer_state.get_target_channel_id("12")) == "8888"


@pytest.mark.asyncio
async def test_migrate_channels_skips_forum_posts_and_unmapped_parents(fluxer_state):
    forum = NS(id=13, name="forum", category_id=10, position=1, type=NS(value=15), topic="", nsfw=False, slowmode_delay=0)
    fluxer_state.set_channel_mapping("13", "503")
    post = NS(id=20, name="post", parent_id=13, type=NS(value=11))
    orphan = NS(id=21, name="orphan", parent_id=99, type=NS(value=11))
    ctx = _cloned_ctx(fluxer_state, threads=[post, orphan],
                      channels=[_channel(11, "general", category_id=10), forum])

    result = await migrate_channels(ctx)

    ctx.fluxer_writer.create_thread.assert_not_called()
    assert result["threads_created"] == []
    assert fluxer_state.get_target_channel_id("20") is None


@pytest.mark.asyncio
async def test_migrate_channels_announcement_parent_gets_announcement_thread(fluxer_state):
    thread = NS(id=12, name="news-talk", parent_id=11, type=NS(value=11))
    ctx = _cloned_ctx(fluxer_state, threads=[thread],
                      target_channels=[{"id": "500", "name": "cat", "type": 4},
                                       {"id": "501", "name": "general", "type": 5, "parent_id": "500"}])

    await migrate_channels(ctx)

    assert ctx.fluxer_writer.create_thread.await_args.kwargs["type"] == 10


@pytest.mark.asyncio
async def test_migrate_channels_continues_after_failing_thread(fluxer_state):
    threads = [NS(id=12, name="a", parent_id=11, type=NS(value=11)), NS(id=13, name="b", parent_id=11, type=NS(value=11))]
    ctx = _cloned_ctx(fluxer_state, threads=threads)
    ctx.fluxer_writer.create_thread = AsyncMock(side_effect=[RuntimeError("MAX_ACTIVE_THREADS"), "8890"])

    result = await migrate_channels(ctx)

    assert result["threads_created"] == ["b"]
    assert fluxer_state.get_target_channel_id("12") is None
    assert str(fluxer_state.get_target_channel_id("13")) == "8890"


@pytest.mark.asyncio
async def test_sync_removes_stale_thread_mapping(fluxer_state):
    fluxer_state.set_channel_mapping("12", "777")
    ctx = _cloned_ctx(fluxer_state, threads=[NS(id=12, name="help", parent_id=11, type=NS(value=11))])

    await sync_channel_state(ctx)

    assert fluxer_state.get_target_channel_id("12") is None
    ctx.fluxer_writer.get_archived_threads.assert_any_await("501")
    ctx.fluxer_writer.get_archived_threads.assert_any_await("501", private=True)


@pytest.mark.asyncio
async def test_sync_keeps_thread_mappings_when_thread_lists_fail(fluxer_state):
    fluxer_state.set_channel_mapping("12", "777")
    ctx = _cloned_ctx(fluxer_state, threads=[NS(id=12, name="help", parent_id=11, type=NS(value=11))])
    ctx.fluxer_writer.get_archived_threads = AsyncMock(side_effect=RuntimeError("403"))

    await sync_channel_state(ctx)

    assert str(fluxer_state.get_target_channel_id("12")) == "777"


@pytest.mark.asyncio
async def test_sync_records_discord_channels_with_target_ids(fluxer_state):
    thread = NS(id=12, name="help", parent_id=11, type=NS(value=11), archived=False)
    ctx = _cloned_ctx(fluxer_state, threads=[thread])

    await sync_channel_state(ctx)

    records = {r["discord_channel_id"]: r for r in fluxer_state.get_all_channel_records()}
    assert {k: (r["name"], r["target_channel_id"]) for k, r in records.items()} == {
        10: ("cat", 500), 11: ("general", 501), 12: ("help", None)}
    assert records[12]["parent_id"] == 11


# --- Renamed / moved channels ---

def _renamed_setup(state):
    """Categories 10, 20 -> 500, 600; channels 11, 12 in category 10 -> 501, 502; thread 13 under 11 -> 503."""
    state.set_category_mapping("10", "500")
    state.set_category_mapping("20", "600")
    state.set_channel_mapping("11", "501")
    state.set_channel_mapping("12", "502")
    state.set_channel_mapping("13", "503")
    categories = [_category(10, "cat", position=0), _category(20, "other", position=1)]
    channels = [_channel(11, "general", category_id=10, position=0), _channel(12, "memes", category_id=10, position=1)]
    threads = [NS(id=13, name="help", parent_id=11, type=NS(value=11))]
    # Fluxer renumbers positions; only the order matters
    target_channels = [{"id": "500", "name": "cat", "type": 4, "position": 5},
                       {"id": "600", "name": "other", "type": 4, "position": 9},
                       {"id": "501", "name": "general", "type": 0, "parent_id": "500", "position": 3},
                       {"id": "502", "name": "memes", "type": 0, "parent_id": "500", "position": 4}]
    target_threads = [{"id": "503", "name": "help", "type": 11, "parent_id": "501"}]
    return categories, channels, threads, target_channels, target_threads


def test_changed_channel_ids_unchanged_server(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    assert changed_channel_ids(NS(state=fluxer_state), cats, chans, threads, t_chans, t_threads) == set()


def test_changed_channel_ids_detects_renames(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    cats[1].name = "Other"
    chans[0].name = "general-chat"
    threads[0].name = "support"
    assert changed_channel_ids(NS(state=fluxer_state), cats, chans, threads, t_chans, t_threads) == {"20", "11", "13"}


def test_changed_channel_ids_detects_moves(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    chans[1].category_id = 20                               # memes moved to another category
    cats[0].position, cats[1].position = 1, 0               # categories swapped
    assert changed_channel_ids(NS(state=fluxer_state), cats, chans, threads, t_chans, t_threads) == {"12", "10", "20"}


def test_changed_channel_ids_detects_reorder_within_category(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    chans[0].position, chans[1].position = 1, 0
    assert changed_channel_ids(NS(state=fluxer_state), cats, chans, threads, t_chans, t_threads) == {"11", "12"}


def test_changed_channel_ids_ignores_unmapped_and_missing_targets(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    chans.append(_channel(14, "new", category_id=10, position=2))   # not cloned yet
    threads[0].name = "renamed"
    assert changed_channel_ids(NS(state=fluxer_state), cats, chans, threads, t_chans, []) == set()


@pytest.mark.asyncio
async def test_migrate_channels_updates_renamed_category_and_thread(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    cats[1].name = "Other"
    threads[0].name = "support"
    ctx = _channel_context(fluxer_state, cats, chans, threads, t_chans, t_threads)
    ctx.fluxer_writer.modify_thread = AsyncMock(return_value={})

    result = await migrate_channels(ctx)

    ctx.fluxer_writer.modify_channel.assert_any_await(channel_id="600", name="Other")
    category_edits = [c for c in ctx.fluxer_writer.modify_channel.await_args_list if str(c.kwargs["channel_id"]) == "500"]
    assert category_edits == []                              # unchanged category is left alone
    ctx.fluxer_writer.modify_thread.assert_awaited_once_with("503", name="support")
    ctx.fluxer_writer.create_channel.assert_not_called()
    assert sorted(result["updated"]) == ["Other", "support"]


@pytest.mark.asyncio
async def test_migrate_channels_renames_archived_thread_and_rearchives(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    threads[0].name = "support"
    t_threads[0]["thread_metadata"] = {"archived": True, "locked": True}
    ctx = _channel_context(fluxer_state, cats, chans, threads, t_chans, t_threads)
    ctx.fluxer_writer.modify_thread = AsyncMock(return_value={})

    await migrate_channels(ctx)

    assert [c.kwargs for c in ctx.fluxer_writer.modify_thread.await_args_list] == [
        {"name": "support", "archived": False},
        {"archived": True, "locked": True},
    ]


@pytest.mark.asyncio
async def test_migrate_channels_unchanged_server_sends_no_reorder(fluxer_state):
    ctx = _channel_context(fluxer_state, *_renamed_setup(fluxer_state))
    ctx.fluxer_writer.modify_thread = AsyncMock(return_value={})

    await migrate_channels(ctx)

    ctx.fluxer_writer.reorder_channels.assert_not_called()
    assert all("position" not in c.kwargs for c in ctx.fluxer_writer.modify_channel.await_args_list)


@pytest.mark.asyncio
async def test_migrate_channels_reorders_channels_within_category(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    chans[0].position, chans[1].position = 1, 0
    ctx = _channel_context(fluxer_state, cats, chans, threads, t_chans, t_threads)

    result = await migrate_channels(ctx)

    ctx.fluxer_writer.reorder_channels.assert_awaited_once_with([
        {"id": "502", "preceding_sibling_id": None, "parent_id": "500"},
        {"id": "501", "preceding_sibling_id": "502", "parent_id": "500"},
    ])
    assert sorted(result["updated"]) == ["general", "memes"]


@pytest.mark.asyncio
async def test_migrate_channels_reorders_categories_at_root(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    cats[0].position, cats[1].position = 1, 0
    ctx = _channel_context(fluxer_state, cats, chans, threads, t_chans, t_threads)

    await migrate_channels(ctx)

    ctx.fluxer_writer.reorder_channels.assert_awaited_once_with([
        {"id": "600", "preceding_sibling_id": None},
        {"id": "500", "preceding_sibling_id": "600"},
    ])
    # names unchanged -> no category rename
    assert not [c for c in ctx.fluxer_writer.modify_channel.await_args_list if str(c.kwargs["channel_id"]) in ("500", "600")]


@pytest.mark.asyncio
async def test_migrate_channels_moves_channel_to_top_level(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    chans[1].category_id = None
    ctx = _channel_context(fluxer_state, cats, chans, threads, t_chans, t_threads)

    await migrate_channels(ctx)

    memes = [c.kwargs for c in ctx.fluxer_writer.modify_channel.await_args_list if str(c.kwargs["channel_id"]) == "502"]
    assert memes[0]["parent_id"] == "0"                      # fluxer.py drops None; 0 = top level
    ctx.fluxer_writer.reorder_channels.assert_awaited_once_with([
        {"id": "502", "preceding_sibling_id": None, "parent_id": None},   # uncategorized channels first
        {"id": "500", "preceding_sibling_id": "502"},
        {"id": "600", "preceding_sibling_id": "500"},
    ])


@pytest.mark.asyncio
async def test_migrate_channels_moves_channel_to_other_category_and_skips_forums(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    chans[1].category_id = 20
    forum = NS(id=15, name="forum", category_id=20, position=0, type=NS(value=15), topic="", nsfw=False, slowmode_delay=0)
    fluxer_state.set_channel_mapping("15", "505")
    t_chans.append({"id": "505", "name": "forum", "type": 15, "parent_id": "600", "position": 1})
    ctx = _channel_context(fluxer_state, cats, chans + [forum], threads, t_chans, t_threads)

    await migrate_channels(ctx)

    memes = [c.kwargs for c in ctx.fluxer_writer.modify_channel.await_args_list if str(c.kwargs["channel_id"]) == "502"]
    assert str(memes[0]["parent_id"]) == "600"
    ctx.fluxer_writer.reorder_channels.assert_awaited_once_with([
        {"id": "502", "preceding_sibling_id": None, "parent_id": "600"},
    ])


def test_changed_channel_ids_ignores_forum_order(fluxer_state):
    cats, chans, threads, t_chans, t_threads = _renamed_setup(fluxer_state)
    forum = NS(id=15, name="forum", category_id=10, position=0, type=NS(value=15))
    fluxer_state.set_channel_mapping("15", "505")
    t_chans.append({"id": "505", "name": "forum", "type": 15, "parent_id": "500", "position": 99})
    assert changed_channel_ids(NS(state=fluxer_state), cats, chans + [forum], threads, t_chans, t_threads) == set()


# --- Deleted on Discord ---

def _deleted_ctx(state, discord_exists: dict, target_channels, target_threads=()):
    """Discord now has category 10 + channel 11 only; channel_exists answers from discord_exists (default True)."""
    ctx = _channel_context(
        state,
        categories=[_category(10, "cat")],
        channels=[_channel(11, "general", category_id=10)],
        threads=[],
        target_channels=target_channels,
        target_threads=target_threads,
    )
    ctx.sync_reader.channel_exists = AsyncMock(side_effect=lambda cid: discord_exists.get(cid, True))
    return ctx


@pytest.mark.asyncio
async def test_find_deleted_channels_lists_only_migrated_channels_gone_on_discord(fluxer_state):
    fluxer_state.record_source_channels([
        _category(20, "old-cat"), _category(10, "cat"),
        NS(id=12, name="old-chan", type=NS(value=0), category_id=10, position=1),
        NS(id=13, name="old-thread", type=NS(value=11), parent_id=11),
        NS(id=14, name="private-archived", type=NS(value=12), parent_id=11),
        NS(id=15, name="gone-everywhere", type=NS(value=0), category_id=10),
    ])
    for discord_id, target in (("10", "500"), ("20", "600")):
        fluxer_state.set_category_mapping(discord_id, target)
    for discord_id, target in (("11", "501"), ("12", "502"), ("13", "503"), ("14", "504"), ("15", "505")):
        fluxer_state.set_channel_mapping(discord_id, target)
    ctx = _deleted_ctx(
        fluxer_state,
        discord_exists={20: False, 12: False, 13: False, 14: None, 15: False},   # 14: not visible, unknown
        target_channels=[{"id": "500", "name": "cat", "type": 4}, {"id": "600", "name": "old-cat", "type": 4},
                         {"id": "501", "name": "general", "type": 0, "parent_id": "500"},
                         {"id": "502", "name": "old-chan", "type": 0, "parent_id": "500"},
                         {"id": "700", "name": "fluxer-only", "type": 0}],        # no record: never listed
        target_threads=[{"id": "503", "name": "old-thread", "type": 11, "parent_id": "501"},
                        {"id": "504", "name": "private-archived", "type": 12, "parent_id": "501"}],
    )

    deleted = await find_deleted_channels(ctx, await ctx.sync_reader.get_categories(),
                                          await ctx.sync_reader.get_channels(), [])

    assert {(d["discord_id"], d["target_id"], d["kind"]) for d in deleted} == {
        ("20", "600", "category"), ("12", "502", "channel"), ("13", "503", "thread")}
    old_chan = next(d for d in deleted if d["discord_id"] == "12")
    assert (old_chan["name"], str(old_chan["category_id"])) == ("old-chan", "10")


@pytest.mark.asyncio
async def test_find_deleted_channels_nothing_to_check(fluxer_state):
    fluxer_state.set_category_mapping("10", "500")
    fluxer_state.set_channel_mapping("11", "501")
    ctx = _deleted_ctx(fluxer_state, {}, target_channels=[])

    assert await find_deleted_channels(ctx, [_category(10, "cat")], [_channel(11, "general", category_id=10)], []) == []
    ctx.sync_reader.channel_exists.assert_not_called()


@pytest.mark.asyncio
async def test_danger_delete_removed_channels_deletes_and_clears_mappings(fluxer_state):
    fluxer_state.set_category_mapping("20", "600")
    fluxer_state.set_channel_mapping("12", "502")
    fluxer_state.set_channel_mapping("13", "503")
    fluxer_state.set_channel_mapping("16", "506")
    fluxer_state.update_last_message_id("502", "999")
    order = []

    async def delete(target_id):
        order.append(target_id)
        if target_id == "506":
            raise RuntimeError("MISSING_PERMISSIONS")
        return target_id != "503"            # thread already gone on Fluxer: still cleaned up

    ctx = NS(writer=NS(delete_channel=AsyncMock(side_effect=delete)), state=fluxer_state)
    items = [{"discord_id": "20", "target_id": "600", "kind": "category", "name": "old-cat"},
             {"discord_id": "12", "target_id": "502", "kind": "channel", "name": "old-chan"},
             {"discord_id": "13", "target_id": "503", "kind": "thread", "name": "old-thread"},
             {"discord_id": "16", "target_id": "506", "kind": "channel", "name": "locked"}]

    assert await danger_delete_removed_channels(ctx, items) == 3

    assert order == ["503", "502", "506", "600"]          # threads, channels, categories
    assert fluxer_state.get_category_mapping("20") is None
    assert fluxer_state.get_target_channel_id("12") is None
    assert fluxer_state.get_target_channel_id("13") is None
    assert str(fluxer_state.get_target_channel_id("16")) == "506"   # failed delete keeps its mapping
    assert fluxer_state.get_channel_record("12")["target_channel_id"] is None
    assert fluxer_state.get_last_message_id("502") is None
