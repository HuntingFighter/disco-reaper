"""
Channel matching: channel_mappings first, name matching only for channels without a known target,
and never onto a target channel another Discord channel already claimed.
"""
import pytest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from src.core.channel_matching import CATEGORY, CHANNEL, known_target, match_channels
from src.core.state import MigrationState


@pytest.fixture
def state(tmp_path):
    s = MigrationState()
    s.set_folder("999", "MatchTest", platform="fluxer", base_dir=tmp_path)
    yield s
    s.db.close()


def _ch(id, name, category_id=None):
    return NS(id=id, name=name, type=NS(value=0), category_id=category_id)


def _target(id, name, parent_id=None, type=0):
    return {"id": id, "name": name, "parent_id": parent_id, "type": type}


def _target_of(state, discord_id):
    return known_target(state, discord_id)


def test_recorded_target_wins_over_name(state):
    """A renamed target channel stays matched; name matching would have picked the other one."""
    state.set_channel_mapping("11", "501")
    targets = [_target("501", "chat"), _target("502", "general")]

    assert match_channels(state, [_ch(11, "general")], targets) == (0, 0)
    assert _target_of(state, "11") == "501"


def test_gone_target_is_dropped_then_matched_by_name(state):
    state.set_channel_mapping("11", "999")

    assert match_channels(state, [_ch(11, "general")], [_target("502", "General")]) == (1, 1)
    assert _target_of(state, "11") == "502"            # name match is case-insensitive
    assert str(state.get_channel_record("11")["target_channel_id"]) == "502"


def test_name_matching_skips_claimed_targets(state):
    """Two Discord channels with the same name don't end up on the same target channel."""
    state.set_channel_mapping("11", "501")
    sources = [_ch(11, "general"), _ch(12, "general"), _ch(13, "general")]

    match_channels(state, sources, [_target("501", "general"), _target("502", "general")])

    assert (_target_of(state, "11"), _target_of(state, "12"), _target_of(state, "13")) == ("501", "502", None)


def test_legacy_mapping_without_record_is_used_and_recorded(state):
    state.db.set_server_mapping("channel", "11", "501")   # written before channel_mappings existed
    state.db.set_channel_record_target("11", None)

    assert match_channels(state, [_ch(11, "general")], [_target("501", "other-name")]) == (0, 0)
    assert str(state.get_channel_record("11")["target_channel_id"]) == "501"


def test_expected_parent_limits_name_matching(state):
    targets = [_target("501", "general", parent_id="600"), _target("502", "general", parent_id="700")]

    match_channels(state, [_ch(11, "general", category_id=10)], targets, expected_parent=lambda ch: "700")

    assert _target_of(state, "11") == "502"


def test_categories_use_category_mappings(state):
    match_channels(state, [NS(id=10, name="Text")], [_target("500", "text", type=4)], kind=CATEGORY)

    assert str(state.get_category_mapping("10")) == "500"
    assert state.get_target_channel_id("10") is None


def test_thread_mappings_are_not_touched_by_channel_matching(state):
    state.set_channel_mapping("12", "8000")   # a thread: not in the target channel list
    state.set_channel_mapping("11", "501")

    match_channels(state, [_ch(11, "general")], [_target("501", "general")])

    assert _target_of(state, "12") == "8000"


@pytest.mark.asyncio
async def test_ui_auto_matching_keeps_thread_mappings(state):
    """Regression: auto-matching used to drop every channel mapping missing from the target channel list,
    which includes all thread mappings, so resumed migrations created duplicate threads."""
    from src.ui.shuttle_ops import OperationPane
    state.set_channel_mapping("11", "501")
    state.set_channel_mapping("12", "8000")   # thread under #general
    state.set_channel_mapping("13", "999")    # channel deleted on the target

    reader = NS(
        get_roles=AsyncMock(return_value=[]), get_emojis=AsyncMock(return_value=[]),
        get_stickers=AsyncMock(return_value=[]), get_categories=AsyncMock(return_value=[]),
        get_channels=AsyncMock(return_value=[_ch(11, "general"), _ch(13, "old"), _ch(14, "new")]),
    )
    client = NS(get_guild_roles=AsyncMock(return_value=[]), get_guild_emojis=AsyncMock(return_value=[]),
                get_guild_stickers=AsyncMock(return_value=[]), get_guild=AsyncMock(return_value=None))
    writer = NS(client=client, get_channels=AsyncMock(return_value=[_target("501", "general"), _target("502", "new")]))
    engine = NS(sync_reader=reader, writer=writer, state=state, config=NS(fluxer_server_id="999"),
                ensure_state_initialized=MagicMock())
    pane = NS(engine=engine, target_platform="fluxer")

    await OperationPane._perform_auto_matching(pane)

    assert _target_of(state, "12") == "8000"   # thread mapping survives
    assert _target_of(state, "11") == "501"
    assert _target_of(state, "13") is None     # deleted target dropped
    assert _target_of(state, "14") == "502"    # matched by name
    assert state.get_channel_record("14")["name"] == "new"
