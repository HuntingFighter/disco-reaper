"""Channels of type 20 are skipped by both readers (they share an interface, see AGENTS.md §4)."""
import discord
import pytest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from src.core.backup_database import BackupDatabase
from src.core.backup_reader import BackupReader
from src.core.discord_reader import DiscordReader


def _discord_reader():
    reader = DiscordReader.__new__(DiscordReader)
    reader.guild = MagicMock()
    reader.guild.id = 1
    return reader


@pytest.mark.asyncio
async def test_discord_reader_skips_type_20_when_discord_py_raises_invalid_data():
    reader = _discord_reader()
    reader.guild.fetch_channels = AsyncMock(side_effect=discord.InvalidData("Unknown channel type 20"))
    reader.guild._state.http.get_all_guild_channels = AsyncMock(return_value=[
        {"id": "10", "name": "general", "type": 0, "position": 0},
        {"id": "11", "name": "weird", "type": 20, "position": 1},
        {"id": "12", "name": "Cat", "type": 4, "position": 2},
    ])

    channels = await reader.fetch_channels()
    assert [c.id for c in channels] == [10, 12]
    assert [c.id for c in await reader.get_channels()] == [10]
    assert [c.id for c in await reader.get_categories()] == [12]


@pytest.mark.asyncio
async def test_discord_reader_skips_type_20_from_parsed_channels():
    reader = _discord_reader()
    reader.guild.fetch_channels = AsyncMock(return_value=[NS(id=10, type=discord.ChannelType.text), NS(id=11, type=20)])

    assert [c.id for c in await reader.fetch_channels()] == [10]


@pytest.mark.asyncio
async def test_discord_reader_still_raises_for_other_unknown_types():
    reader = _discord_reader()
    reader.guild.fetch_channels = AsyncMock(side_effect=discord.InvalidData("Unknown channel type 99"))
    reader.guild._state.http.get_all_guild_channels = AsyncMock(return_value=[{"id": "13", "name": "x", "type": 99}])

    with pytest.raises(discord.InvalidData):
        await reader.fetch_channels()


@pytest.mark.asyncio
async def test_backup_reader_skips_type_20(tmp_path):
    db = BackupDatabase(tmp_path / "backup.db")
    db._conn.execute("INSERT INTO guild_profile (id, name) VALUES (?, ?)", (1, "Guild"))
    db._conn.executemany("INSERT INTO channels (id, name, type) VALUES (?, ?, ?)",
                         [(10, "general", 0), (11, "weird", 20), (12, "Cat", 4)])
    db._conn.commit()
    reader = BackupReader(tmp_path)
    reader.db = db

    assert sorted(c.id for c in await reader.fetch_channels()) == [10, 12]
    assert [c.id for c in await reader.get_channels()] == [10]
    assert await reader.get_channel(11) is None
