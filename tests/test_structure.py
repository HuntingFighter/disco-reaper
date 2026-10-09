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
        "  [bold yellow]📁 Voice[/bold yellow]",
        "    - # Lobby",
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
    me = NS(engine=NS(sync_reader=reader, state=state))

    preview = await OperationPane._fetch_clone_preview(me, ["sub_clone_roles", "sub_clone_channels"])

    assert preview["roles"] == [("Mods", True)]
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
    me = NS(engine=NS(sync_reader=reader, state=NS(channel_map={}, category_map={}, role_map={})))

    preview = await OperationPane._fetch_clone_preview(me, ["sub_clone_channels"])

    assert shape(preview["structure"]) == (ROOT, "root", [(CHANNEL, "general", [])])
