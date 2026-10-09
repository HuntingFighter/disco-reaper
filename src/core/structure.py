"""
Source server hierarchy as a recursive tree: categories > channels > threads.

Building and reading are both recursive, so a node can hold children of any kind at any depth
(e.g. threads under channels). Works with discord.py and backup objects alike.
"""
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, List, Optional

ROOT = "root"
CATEGORY = "category"
CHANNEL = "channel"
THREAD = "thread"

# Siblings are listed channels first, then categories (uncategorized channels sit at the top),
# each group by position, then id (threads have no position, so they end up in creation order).
_KIND_ORDER = {CHANNEL: 0, THREAD: 0, CATEGORY: 1}
_ICONS = {CATEGORY: "📁", CHANNEL: "#", THREAD: "🧵"}


@dataclass
class StructureNode:
    kind: str
    name: str
    id: Optional[int] = None
    exists: bool = False
    position: int = 0
    children: List["StructureNode"] = field(default_factory=list)

    def walk(self) -> Iterator["StructureNode"]:
        """Yields this node and all descendants, depth first."""
        yield self
        for child in self.children:
            yield from child.walk()

    def count(self, kind: str) -> int:
        return sum(1 for node in self.walk() if node.kind == kind)


def _parent_of(kind: str, item) -> Optional[int]:
    if kind == CATEGORY:
        return None
    if kind == THREAD:
        # Not category_id: on discord.Thread that is the parent channel's category.
        return getattr(item, "parent_id", None)
    return getattr(item, "category_id", None)


def build_structure_tree(
    categories: Iterable = (),
    channels: Iterable = (),
    threads: Iterable = (),
    is_mapped: Callable[[str, int], bool] = lambda kind, item_id: False,
) -> StructureNode:
    """
    Builds the tree recursively: every item becomes a child of the item whose id is its parent id.
    Items without a parent, or whose parent is not in the input (e.g. a thread of a skipped channel),
    are attached to the root. is_mapped(kind, id) marks nodes that already exist on the target.
    """
    items = [(CATEGORY, c) for c in categories] + [(CHANNEL, c) for c in channels] + [(THREAD, t) for t in threads]
    known_ids = {item.id for _, item in items}

    by_parent: dict = {}
    for kind, item in items:
        parent = _parent_of(kind, item)
        if parent not in known_ids or parent == item.id:
            parent = None
        by_parent.setdefault(parent, []).append((kind, item))

    def sort_key(entry):
        kind, item = entry
        return (_KIND_ORDER.get(kind, 0), getattr(item, "position", 0) or 0, item.id or 0)

    def make_children(parent_id: Optional[int], seen: frozenset) -> List[StructureNode]:
        nodes = []
        for kind, item in sorted(by_parent.get(parent_id, []), key=sort_key):
            if item.id in seen:  # guards against malformed parent cycles
                continue
            nodes.append(StructureNode(
                kind=kind,
                name=item.name,
                id=item.id,
                exists=is_mapped(kind, item.id),
                position=getattr(item, "position", 0) or 0,
                children=make_children(item.id, seen | {item.id}),
            ))
        return nodes

    return StructureNode(ROOT, "root", children=make_children(None, frozenset()))


def render_structure_lines(node: StructureNode, depth: int = 1, max_threads: int = 10) -> List[str]:
    """
    Renders the tree recursively as Rich-markup lines for the clone preview. Existing items are green,
    new categories bold yellow. At most max_threads threads are listed per parent.
    """
    lines: List[str] = []
    indent = "  " * depth
    shown_threads = 0
    hidden_threads = 0
    for child in node.children:
        if child.kind == THREAD:
            if shown_threads >= max_threads:
                hidden_threads += 1
                continue
            shown_threads += 1

        icon = _ICONS.get(child.kind, "-")
        if child.kind == CATEGORY:
            color = "green" if child.exists else "bold yellow"
            lines.append(f"{indent}[{color}]{icon} {child.name}[/{color}]")
        elif child.exists:
            lines.append(f"{indent}- [green]{icon} {child.name}[/green]")
        else:
            lines.append(f"{indent}- {icon} {child.name}")
        lines.extend(render_structure_lines(child, depth + 1, max_threads))

    if hidden_threads:
        lines.append(f"{indent}[dim]... and {hidden_threads} more threads[/dim]")
    return lines
