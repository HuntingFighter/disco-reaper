"""Recursive server structure tree used by the clone preview (src/core/structure.py)."""
import pytest
from types import SimpleNamespace as NS

from src.core.structure import (
    CATEGORY, CHANNEL, THREAD, ROOT, StructureNode, build_structure_tree, render_structure_lines,
)


def cat(id, name, position=0):
    return NS(id=id, name=name, position=position)


def chan(id, name, category_id=None, position=0):
    return NS(id=id, name=name, category_id=category_id, position=position)


def thread(id, name, parent_id, category_id=None):
    # discord.Thread.category_id is the parent channel's category; the tree must use parent_id
    return NS(id=id, name=name, parent_id=parent_id, category_id=category_id)


def shape(node: StructureNode):
    return (node.kind, node.name, [shape(c) for c in node.children])


@pytest.fixture
def tree():
    return build_structure_tree(
        categories=[cat(2, "Voice", position=1), cat(1, "Text", position=0)],
        channels=[
            chan(11, "general", category_id=1, position=0),
            chan(12, "memes", category_id=1, position=1),
            chan(21, "Lobby", category_id=2),
            chan(30, "rules"),                          # uncategorized
        ],
        threads=[
            thread(102, "second", parent_id=11, category_id=1),
            thread(101, "first", parent_id=11, category_id=1),
            thread(999, "orphan", parent_id=4242),      # parent channel not in the input
        ],
        is_mapped=lambda kind, item_id: item_id in {1, 11},
    )


def test_tree_nests_threads_under_their_channel(tree):
    assert shape(tree) == (ROOT, "root", [
        (CHANNEL, "rules", []),
        (THREAD, "orphan", []),
        (CATEGORY, "Text", [
            (CHANNEL, "general", [(THREAD, "first", []), (THREAD, "second", [])]),
            (CHANNEL, "memes", []),
        ]),
        (CATEGORY, "Voice", [(CHANNEL, "Lobby", [])]),
    ])


def test_counts_and_exists_flags(tree):
    assert (tree.count(CATEGORY), tree.count(CHANNEL), tree.count(THREAD)) == (2, 4, 3)
    assert {n.name for n in tree.walk() if n.exists} == {"Text", "general"}


def test_tree_supports_arbitrary_depth():
    # e.g. a thread under a thread: building and walking are recursive, not fixed to 3 levels
    deep = build_structure_tree(
        channels=[chan(1, "a")],
        threads=[thread(2, "b", parent_id=1), thread(3, "c", parent_id=2)],
    )
    assert shape(deep) == (ROOT, "root", [(CHANNEL, "a", [(THREAD, "b", [(THREAD, "c", [])])])])


def test_parent_cycle_does_not_recurse_forever():
    looped = build_structure_tree(threads=[thread(1, "x", parent_id=2), thread(2, "y", parent_id=1)])
    assert looped.count(THREAD) == 0  # neither is reachable from the root; no infinite recursion


def test_render_lines(tree):
    assert render_structure_lines(tree) == [
        "  - # rules",
        "  - 🧵 orphan",
        "  [green]📁 Text[/green]",
        "    - [green]# general[/green]",
        "      - 🧵 first",
        "      - 🧵 second",
        "    - # memes",
        "  [bold]📁 Voice[/bold]",
        "    - # Lobby",
    ]


def test_render_marks_changed_items_orange():
    changed = build_structure_tree(
        categories=[cat(1, "Text")],
        channels=[chan(11, "general", category_id=1), chan(12, "memes", category_id=1)],
        is_mapped=lambda kind, item_id: True,
        is_changed=lambda kind, item_id: item_id in {1, 12},
    )
    assert render_structure_lines(changed) == [
        "  [orange1]📁 Text[/orange1]",
        "    - [green]# general[/green]",
        "    - [orange1]# memes[/orange1]",
    ]


def test_render_limits_threads_per_parent():
    many = build_structure_tree(
        channels=[chan(1, "busy")],
        threads=[thread(100 + i, f"t{i}", parent_id=1) for i in range(5)],
    )
    lines = render_structure_lines(many, max_threads=2)
    assert lines == [
        "  - # busy",
        "    - 🧵 t0",
        "    - 🧵 t1",
        "    [dim]... and 3 more threads[/dim]",
    ]


@pytest.mark.asyncio
async def test_clone_preview_builds_tree_and_matches_int_mapping_keys():
    from unittest.mock import AsyncMock
    from src.ui.shuttle_ops import OperationPane

    reader = NS(
        get_roles=AsyncMock(return_value=[NS(id=7, name="Mods")]),
        get_categories=AsyncMock(return_value=[cat(1, "Text")]),
        get_channels=AsyncMock(return_value=[chan(11, "general", category_id=1)]),
        get_threads=AsyncMock(return_value=[thread(101, "first", parent_id=11)]),
    )
    # Fluxer's migration DB returns int keys
    state = NS(channel_map={11: 500}, category_map={1: 400}, role_map={7: 600})
    me = NS(engine=NS(sync_reader=reader, state=state), target_platform="stoat")

    preview = await OperationPane._fetch_clone_preview(me, ["sub_clone_roles", "sub_clone_channels"])

    assert preview["roles"] == [("Mods", True)]
    reader.get_threads.assert_awaited_once_with(True)  # archived threads/forum posts included
    root = preview["structure"]
    assert shape(root) == (ROOT, "root", [
        (CATEGORY, "Text", [(CHANNEL, "general", [(THREAD, "first", [])])]),
    ])
    assert [n.exists for n in root.walk()] == [False, True, True, False]


@pytest.mark.asyncio
async def test_clone_preview_without_threads_when_fetch_fails():
    from unittest.mock import AsyncMock
    from src.ui.shuttle_ops import OperationPane

    reader = NS(
        get_categories=AsyncMock(return_value=[]),
        get_channels=AsyncMock(return_value=[chan(11, "general")]),
        get_threads=AsyncMock(side_effect=RuntimeError("boom")),
    )
    me = NS(engine=NS(sync_reader=reader, state=NS(channel_map={}, category_map={}, role_map={})), target_platform="stoat")

    preview = await OperationPane._fetch_clone_preview(me, ["sub_clone_channels"])

    assert shape(preview["structure"]) == (ROOT, "root", [(CHANNEL, "general", [])])


@pytest.mark.asyncio
async def test_clone_preview_marks_renamed_channels_on_fluxer(monkeypatch):
    from unittest.mock import AsyncMock
    from src.ui.shuttle_ops import OperationPane
    import src.fluxer.clone_server as clone_server

    reader = NS(
        get_categories=AsyncMock(return_value=[cat(1, "Text")]),
        get_channels=AsyncMock(return_value=[chan(11, "general", category_id=1), chan(12, "memes", category_id=1)]),
        get_threads=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(clone_server, "find_changed_channels", AsyncMock(return_value={"12"}))
    state = NS(channel_map={11: 500, 12: 501}, category_map={1: 400}, role_map={})
    me = NS(engine=NS(sync_reader=reader, state=state), target_platform="fluxer")

    preview = await OperationPane._fetch_clone_preview(me, ["sub_clone_channels"])

    assert {n.name for n in preview["structure"].walk() if n.changed} == {"memes"}


def test_forum_posts_are_hidden_with_a_note():
    forum = NS(id=1, name="ideas", category_id=None, position=0, type=NS(value=15))
    media = NS(id=2, name="gallery", category_id=None, position=1, type=16)
    tree = build_structure_tree(
        channels=[forum, media, chan(3, "general", position=2)],
        threads=[thread(10, "post a", parent_id=1), thread(11, "post b", parent_id=1), thread(12, "chat", parent_id=3)],
    )
    assert tree.count(THREAD) == 1
    assert render_structure_lines(tree) == [
        "  - # ideas",
        "    [dim]2 forum posts, only created when migrating messages (renamed posts are updated here)[/dim]",
        "  - # gallery",
        "    [dim]Forum posts are only created when migrating messages (renamed posts are updated here)[/dim]",
        "  - # general",
        "    - 🧵 chat",
    ]


def test_deleted_items_are_red_always_listed_and_counted_apart():
    forum = NS(id=1, name="ideas", category_id=None, position=0, type=15)
    tree = build_structure_tree(
        categories=[cat(5, "Old", position=1)],
        channels=[forum, chan(6, "gone", category_id=5)],
        threads=[thread(10, "live post", parent_id=1), thread(11, "dead post", parent_id=1)],
        is_mapped=lambda kind, item_id: True,
        is_deleted=lambda kind, item_id: item_id in {5, 6, 11},
    )
    assert render_structure_lines(tree, max_threads=0) == [
        "  - [green]# ideas[/green]",
        "    [dim]1 forum posts, only created when migrating messages (renamed posts are updated here)[/dim]",
        "    - [red]🧵 dead post[/red]",
        "  [red]📁 Old[/red]",
        "    - [red]# gone[/red]",
    ]
    assert (tree.count(CATEGORY), tree.count(CHANNEL), tree.count(THREAD)) == (0, 1, 0)
    assert (tree.count(CATEGORY, deleted=True), tree.count(CHANNEL, deleted=True), tree.count(THREAD, deleted=True)) == (1, 1, 1)


@pytest.mark.asyncio
async def test_clone_preview_places_deleted_channels_under_their_old_parent(monkeypatch):
    from unittest.mock import AsyncMock
    from src.ui.shuttle_ops import OperationPane
    import src.fluxer.clone_server as clone_server

    reader = NS(
        get_categories=AsyncMock(return_value=[cat(1, "Text")]),
        get_channels=AsyncMock(return_value=[chan(11, "general", category_id=1)]),
        get_threads=AsyncMock(return_value=[]),
    )
    gone = [{"discord_id": "12", "target_id": "502", "kind": "channel", "name": "old", "type": 0,
             "category_id": "1", "parent_id": None, "position": 1},
            {"discord_id": "13", "target_id": "503", "kind": "thread", "name": "old thread", "type": 11,
             "category_id": None, "parent_id": "11", "position": 0}]
    monkeypatch.setattr(clone_server, "find_changed_channels", AsyncMock(return_value=set()))
    monkeypatch.setattr(clone_server, "find_deleted_channels", AsyncMock(return_value=gone))
    state = NS(channel_map={11: 500, 12: 502, 13: 503}, category_map={1: 400}, role_map={})
    me = NS(engine=NS(sync_reader=reader, state=state), target_platform="fluxer")

    preview = await OperationPane._fetch_clone_preview(me, ["sub_clone_channels"])

    assert preview["deleted"] == gone
    assert shape(preview["structure"]) == (ROOT, "root", [
        (CATEGORY, "Text", [(CHANNEL, "general", [(THREAD, "old thread", [])]), (CHANNEL, "old", [])]),
    ])
    assert {n.name for n in preview["structure"].walk() if n.deleted} == {"old", "old thread"}
