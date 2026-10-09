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
from src.fluxer.clone_server import sync_channel_state, migrate_channels
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


def _channel_context(state, categories, channels, threads, target_channels):
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
    )
    return NS(discord_reader=reader, fluxer_writer=writer, state=state, is_running=True)


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
    return NS(discord_reader=reader, fluxer_writer=NS(client=client), state=state,
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
    )
    await sync_channel_state(ctx)
    result = await migrate_channels(ctx, force=False)
    ctx.fluxer_writer.create_channel.assert_not_called()
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
