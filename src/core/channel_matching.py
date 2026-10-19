"""
Matching Discord categories/channels to existing target channels.

channel_mappings (see database.py) is the primary source: a Discord channel whose recorded target
still exists keeps it. Only channels without a known target are matched by name, and only against
target channels that no other Discord channel has claimed yet.
"""
import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

CATEGORY = "category"
CHANNEL = "channel"


def known_target(state, discord_id, kind: str = CHANNEL) -> str | None:
    """Target ID recorded for a Discord category/channel/thread: channel_mappings first, then the legacy mapping."""
    record = state.get_channel_record(discord_id)
    if record and record.get("target_channel_id") is not None:
        return str(record["target_channel_id"])
    legacy = state.get_category_mapping(discord_id) if kind == CATEGORY else state.get_target_channel_id(discord_id)
    return str(legacy) if legacy is not None else None


def claimed_targets(state) -> dict[str, str]:
    """target channel ID -> Discord channel ID for every recorded mapping."""
    return {str(r["target_channel_id"]): str(r["discord_channel_id"])
            for r in state.get_all_channel_records() if r.get("target_channel_id") is not None}


def match_channels(
    state,
    sources: Iterable[Any],
    targets: Iterable[dict],
    kind: str = CHANNEL,
    expected_parent: Callable[[Any], str | None] | None = None,
) -> tuple[int, int]:
    """
    Maps Discord categories or channels (sources) to target channels (targets: dicts with id/name/parent_id).
    expected_parent(source) -> target parent ID the match must have ("root" for none); None means any parent.
    Returns (new mappings, removed stale mappings).
    """
    set_mapping = state.set_category_mapping if kind == CATEGORY else state.set_channel_mapping
    remove_mapping = state.remove_category_mapping if kind == CATEGORY else state.remove_channel_mapping
    targets = list(targets)
    target_ids = {str(t.get("id")) for t in targets}
    claimed = claimed_targets(state)
    updates = removals = 0

    sources = list(sources)
    unmatched = []
    for src in sources:
        discord_id = str(src.id)
        target = known_target(state, discord_id, kind)
        if target is None:
            unmatched.append(src)
        elif target in target_ids:
            if claimed.get(target) != discord_id:
                set_mapping(discord_id, target)  # legacy-only mapping: record it in channel_mappings
                claimed[target] = discord_id
        else:
            logger.info(f"Channel matching: target {target} of Discord {kind} {src.name} ({discord_id}) is gone")
            remove_mapping(discord_id)
            claimed.pop(target, None)
            removals += 1
            unmatched.append(src)

    # Name fallback, only against targets nobody has claimed
    for src in unmatched:
        parent = expected_parent(src) if expected_parent else None
        name = (src.name or "").casefold()
        match = next((t for t in targets
                      if str(t.get("id")) not in claimed
                      and (t.get("name") or "").casefold() == name
                      and (parent is None or (str(t.get("parent_id")) if t.get("parent_id") else "root") == parent)), None)
        if match:
            target = str(match.get("id"))
            logger.info(f"Channel matching: {kind} {src.name} ({src.id}) matched by name -> {target}")
            set_mapping(str(src.id), target)
            claimed[target] = str(src.id)
            updates += 1
    return updates, removals
