import asyncio
import logging
from typing import Callable, Awaitable

from src.core.base import MigrationContext
from src.core.channel_matching import CATEGORY, CHANNEL, known_target, match_channels

logger = logging.getLogger(__name__)

# Fluxer channel types (same numbers as Discord)
FLUXER_ANNOUNCEMENT = 5
FLUXER_THREAD_TYPES = {10, 11, 12}
FLUXER_FORUM_TYPES = {15, 16}  # forum, media: posts are created with their first message
ARCHIVE_DURATIONS = {60, 1440, 4320, 10080}


async def _fluxer_channel_type(context: MigrationContext, fluxer_channel_id: str) -> int | None:
    for c in await context.fluxer_writer.get_channels():
        if str(c.get("id")) == str(fluxer_channel_id):
            return c.get("type")
    return None


async def resolve_target_thread(context: MigrationContext, thread, parent_message_id: str | None = None, force: bool = False) -> tuple[str | None, str | None, str | None]:
    """
    Finds or creates the Fluxer thread for a source thread.
    Returns (parent_target_id, target_thread_id, forum_post_name):
    - parent_target_id is the mapped Fluxer parent channel (owns the webhook); None if the parent isn't cloned yet.
    - target_thread_id is the Fluxer thread, created now if it wasn't mapped.
    - forum_post_name is set instead when the parent is a forum/media channel: the post is created
      together with its first message (writer.send_message(thread_name=...)).
    parent_message_id: migrated Fluxer message the thread starts from, if any (else a standalone thread).
    """
    parent_target_id = context.state.get_fluxer_channel_id(str(thread.parent_id)) if getattr(thread, "parent_id", None) else None
    if not parent_target_id:
        return None, None, None
    parent_target_id = str(parent_target_id)  # the Fluxer DB returns IDs as int

    existing = context.state.get_fluxer_channel_id(str(thread.id))
    if existing and not force:
        return parent_target_id, str(existing), None

    parent_type = await _fluxer_channel_type(context, parent_target_id)
    name = (thread.name or "thread")[:100]
    if parent_type in FLUXER_FORUM_TYPES:
        return parent_target_id, None, name

    # Announcement parents only take announcement threads; text parents take public/private
    t_type = getattr(thread, "type", 11)
    raw_type = t_type.value if hasattr(t_type, "value") else t_type
    if parent_type == FLUXER_ANNOUNCEMENT:
        thread_type = 10
    else:
        thread_type = raw_type if raw_type in (11, 12) else 11
    archive = getattr(thread, "auto_archive_duration", None)
    archive = archive if archive in ARCHIVE_DURATIONS else None
    slowmode = getattr(thread, "slowmode_delay", None) or None

    if parent_message_id:
        target_thread_id = await context.fluxer_writer.create_thread_from_message(
            parent_target_id, parent_message_id, name, auto_archive_duration=archive, slowmode_delay=slowmode
        )
    else:
        target_thread_id = await context.fluxer_writer.create_thread(
            parent_target_id, name, type=thread_type, auto_archive_duration=archive, slowmode_delay=slowmode
        )
    context.state.set_channel_mapping(str(thread.id), target_thread_id)
    logger.info(f"Created Fluxer thread '{name}' ({target_thread_id}) for Discord thread {thread.id}")
    return parent_target_id, target_thread_id, None


async def apply_thread_state(context: MigrationContext, thread, target_thread_id: str) -> None:
    """Copies archived/locked from the source thread. Run after its messages are migrated:
    webhook messages unarchive a thread and are rejected in a locked one."""
    archived = bool(getattr(thread, "archived", False))
    locked = bool(getattr(thread, "locked", False))
    if not archived and not locked:
        return
    try:
        await context.fluxer_writer.modify_thread(target_thread_id, archived=archived or None, locked=locked or None)
    except Exception as e:
        logger.warning(f"Could not archive/lock Fluxer thread {target_thread_id}: {e}")


async def _target_threads(context: MigrationContext, threads: list) -> list[dict]:
    """Fluxer threads (active + archived) under the mapped parents of the given Discord threads. Raises if a list fails."""
    parents = {p for p in (_thread_parent_target(context, t) for t in threads) if p}
    target_threads = list(await context.fluxer_writer.get_active_threads())
    channel_types = {str(c.get("id")): c.get("type") for c in await context.fluxer_writer.get_channels()}
    for parent in parents:
        target_threads += await context.fluxer_writer.get_archived_threads(parent)
        if channel_types.get(parent) == 0:
            target_threads += await context.fluxer_writer.get_archived_threads(parent, private=True)
    return target_threads


def _thread_parent_target(context: MigrationContext, thread) -> str | None:
    return known_target(context.state, str(thread.parent_id)) if getattr(thread, "parent_id", None) else None


async def _sync_thread_state(context: MigrationContext, threads: list) -> tuple[int, int]:
    """
    Thread part of sync_channel_state: channel_mappings first (drops targets that are gone), then
    unmapped threads by name under their mapped parent. Returns (updates, removals).
    Skips entirely if Fluxer's thread lists can't be read, so no valid mapping is dropped.
    """
    try:
        target_threads = await _target_threads(context, threads)
    except Exception as e:
        logger.warning(f"Fluxer thread sync skipped, could not list threads: {e}")
        return 0, 0

    return match_channels(context.state, threads, target_threads, kind=CHANNEL,
                          expected_parent=lambda t: _thread_parent_target(context, t) or "unmapped-parent")


def _raw_type(item) -> int:
    t = getattr(item, "type", 0)
    return t.value if hasattr(t, "value") else (t or 0)


def changed_channel_ids(context: MigrationContext, categories: list, channels: list, threads: list,
                        target_channels: list[dict], target_threads: list[dict] = ()) -> set[str]:
    """
    Discord IDs of mapped categories/channels/threads that were renamed or moved on Discord, i.e. whose
    Fluxer channel has a different name, parent category, or place in the order of its mapped siblings.
    Order is compared relatively (not by raw position values), voice channels after text channels as in Discord.
    Forum/media channels are left out of the order (Fluxer's reorder route can't move them). Threads: name only.
    """
    targets = {str(t.get("id")): t for t in [*target_channels, *target_threads]}
    changed: set[str] = set()
    siblings: dict[str, list[tuple]] = {}  # parent group -> [(discord sort key, target sort key, discord id)]

    def mapped(item, kind):
        target_id = known_target(context.state, str(item.id), kind)
        return targets.get(target_id) if target_id else None

    for kind, items in ((CATEGORY, categories), (CHANNEL, channels)):
        for item in items:
            target = mapped(item, kind)
            if target is None:
                continue
            discord_id = str(item.id)
            if (target.get("name") or "") != (item.name or ""):
                changed.add(discord_id)
            if kind == CATEGORY:
                group = "categories"
            else:
                category = getattr(item, "category_id", None)
                expected = known_target(context.state, str(category), CATEGORY) if category else None
                actual = str(target["parent_id"]) if target.get("parent_id") else None
                if expected != actual:
                    changed.add(discord_id)
                    continue  # different parent: its order there says nothing
                group = expected or "root"
                if target.get("type") in FLUXER_FORUM_TYPES:
                    continue
            siblings.setdefault(group, []).append((
                (_raw_type(item) == 2, getattr(item, "position", 0) or 0, int(item.id)),
                (target.get("type") == 2, target.get("position") or 0, int(target.get("id") or 0)),
                discord_id,
            ))

    for entries in siblings.values():
        discord_order = [e[2] for e in sorted(entries, key=lambda e: e[0])]
        target_order = [e[2] for e in sorted(entries, key=lambda e: e[1])]
        changed.update(d for d, t in zip(discord_order, target_order) if d != t)

    for thread in threads:
        target = mapped(thread, CHANNEL)
        if target is not None and (target.get("name") or "") != (thread.name or ""):
            changed.add(str(thread.id))
    return changed


async def find_changed_channels(context: MigrationContext, categories: list, channels: list, threads: list) -> set[str]:
    """changed_channel_ids against the live Fluxer channels and threads (threads are left out if they can't be listed)."""
    target_channels = await context.fluxer_writer.get_channels()
    try:
        target_threads = await _target_threads(context, threads) if threads else []
    except Exception as e:
        logger.warning(f"Could not list Fluxer threads to check for renames: {e}")
        target_threads = []
    return changed_channel_ids(context, categories, channels, threads, target_channels, target_threads)


def _reorder_entries(context: MigrationContext, group: str, categories: list, channels: list, skip_ids: set[str]) -> list[dict]:
    """
    Reorder route entries that put the mapped children of one target parent (a Fluxer category ID, or "root")
    in Discord's order: each entry names the sibling before it. Root holds uncategorized channels, then
    categories; inside a group text-type channels come before voice channels (Fluxer refuses the reverse).
    skip_ids: Discord IDs to leave out (forum/media channels, which the route can't move).
    """
    def target_parent(ch) -> str:
        category = getattr(ch, "category_id", None)
        return (known_target(context.state, str(category), CATEGORY) if category else None) or "root"

    def order(item):
        return (_raw_type(item) == 2, getattr(item, "position", 0) or 0, int(item.id))

    members = [(CHANNEL, ch) for ch in sorted(channels, key=order)
               if str(ch.id) not in skip_ids and target_parent(ch) == group]
    if group == "root":
        members += [(CATEGORY, c) for c in sorted(categories, key=lambda c: (getattr(c, "position", 0) or 0, int(c.id)))]

    entries, previous = [], None
    for kind, item in members:
        target_id = known_target(context.state, str(item.id), kind)
        if not target_id:
            continue
        entry = {"id": target_id, "preceding_sibling_id": previous}
        if kind == CHANNEL:
            entry["parent_id"] = None if group == "root" else group
        entries.append(entry)
        previous = target_id
    return entries


THREAD_TYPES = {10, 11, 12}


async def find_deleted_channels(context: MigrationContext, categories: list, channels: list, threads: list) -> list[dict]:
    """
    Categories/channels/threads that were migrated before (channel_mappings row with a target) but are gone
    on Discord, while their Fluxer copy still exists. Channels that only exist on Fluxer have no row and are
    never listed. An item counts as deleted only if Discord answers 404 for it: missing from the listings
    alone isn't enough (archived private threads aren't listed, archive scans can be forbidden).
    Returns dicts: discord_id, target_id, kind (category/channel/thread), name, type, category_id, parent_id, position.
    """
    source_ids = {str(i.id) for i in [*categories, *channels, *threads]}
    candidates = [r for r in context.state.get_all_channel_records()
                  if r.get("target_channel_id") is not None and str(r["discord_channel_id"]) not in source_ids]
    if not candidates:
        return []

    targets = {str(c.get("id")): c for c in await context.fluxer_writer.get_channels()}
    thread_parents = {known_target(context.state, str(r["parent_id"])) for r in candidates
                      if str(r["target_channel_id"]) not in targets and r.get("parent_id")}
    try:
        for t in await context.fluxer_writer.get_active_threads():
            targets[str(t.get("id"))] = t
        for parent in filter(None, thread_parents):
            for private in ((False, True) if (targets.get(parent) or {}).get("type") == 0 else (False,)):
                for t in await context.fluxer_writer.get_archived_threads(parent, private=private):
                    targets[str(t.get("id"))] = t
    except Exception as e:
        logger.warning(f"Could not list Fluxer threads to check for deleted threads: {e}")

    deleted = []
    for r in candidates:
        target = targets.get(str(r["target_channel_id"]))
        if target is None:
            continue  # already gone on Fluxer too (or not listable): nothing to show or delete
        if await context.sync_reader.channel_exists(int(r["discord_channel_id"])) is not False:
            continue
        t_type = r.get("type") if r.get("type") is not None else target.get("type")
        kind = CATEGORY if t_type == 4 else ("thread" if t_type in THREAD_TYPES else CHANNEL)
        deleted.append({
            "discord_id": str(r["discord_channel_id"]),
            "target_id": str(r["target_channel_id"]),
            "kind": kind,
            "name": r.get("name") or target.get("name") or "unknown",
            "type": t_type,
            "category_id": r.get("category_id"),
            "parent_id": r.get("parent_id"),
            "position": r.get("position") or 0,
        })
    return deleted


async def _rename_thread(context: MigrationContext, target_thread: dict, name: str) -> None:
    """Renames a Fluxer thread. An archived thread only accepts changes together with archived=False,
    so it is unarchived for the rename and then put back into its previous archived/locked state."""
    thread_id = str(target_thread.get("id"))
    meta = target_thread.get("thread_metadata") or {}
    if not meta.get("archived"):
        await context.fluxer_writer.modify_thread(thread_id, name=name)
        return
    await context.fluxer_writer.modify_thread(thread_id, name=name, archived=False)
    await context.fluxer_writer.modify_thread(thread_id, archived=True, locked=bool(meta.get("locked")) or None)


async def sync_channel_state(context: MigrationContext):
    """
    Scans Fluxer for channels matching Discord names and updates state file mappings.
    This prevents duplicate creation when the state file is empty but channels exist in Fluxer.
    """
    categories = await context.sync_reader.get_categories()
    channels = await context.sync_reader.get_channels()
    threads = await context.sync_reader.get_threads(True)
    fluxer_channels = await context.fluxer_writer.get_channels()

    # Keep the Discord data in channel_mappings current (target IDs are set with each mapping)
    context.state.record_source_channels([*categories, *channels, *threads])
    
    fluxer_categories = [c for c in fluxer_channels if c.get("type") == 4]
    fluxer_others = [c for c in fluxer_channels if c.get("type") != 4]

    # 1. Categories: channel_mappings first, then by name
    updates, removals = match_channels(context.state, categories, fluxer_categories, kind=CATEGORY)

    # 2. Channels: channel_mappings first, then by name within the mapped parent category
    def expected_parent(ch) -> str:
        if not ch.category_id:
            return "root"
        return known_target(context.state, str(ch.category_id), CATEGORY) or "unmapped-category"

    c_updates, c_removals = match_channels(context.state, channels, fluxer_others, kind=CHANNEL,
                                           expected_parent=expected_parent)
    updates += c_updates
    removals += c_removals

    # 3. Sync Threads (after channels, so their parents are mapped)
    t_updates, t_removals = await _sync_thread_state(context, threads)
    updates += t_updates
    removals += t_removals

    if updates > 0 or removals > 0:
        logger.info(f"Fluxer Channel sync: {updates} mapped, {removals} stale mappings removed")


async def migrate_channels(context: MigrationContext, progress_callback: Callable[[str, str, int, int], Awaitable[None]] | None = None, force: bool = False) -> dict:
    """Clones categories and text channels.
    
    Args:
        progress_callback: Optional callback receiving (item_name, status, current, total)
        force: If True, re-create channels even if they exist in state.
    """
    # Sort by position to respect Discord arrangement
    categories = sorted(await context.sync_reader.get_categories(), key=lambda c: getattr(c, 'position', 0))
    all_channels = sorted(await context.sync_reader.get_channels(), key=lambda c: getattr(c, 'position', 0))
    # Threads have no position; creation order (snowflake) keeps them in Discord's order
    all_threads = sorted(await context.sync_reader.get_threads(True), key=lambda t: int(t.id))

    cloned_info = {
        "categories_created": [],
        "channels_created": [],
        "channels_synced": [],
        "threads_created": [],
        "updated": [],  # renamed or moved on Discord, edited on Fluxer
        "structure": {} # category_name -> [channel_names]
    }
    cat_name_map = {str(cat.id): cat.name for cat in categories}
    
    # 1. Identify categories to create
    missing_categories = [cat for cat in categories if force or not context.state.get_fluxer_category_id(str(cat.id))]
    missing_category_ids = {str(cat.id) for cat in missing_categories}
    
    # 2. Identify channels to create or move
    # Fetch current Fluxer state to check parent_ids
    fluxer_channels = await context.fluxer_writer.get_channels()
    fluxer_parent_map = {str(c["id"]): (str(c.get("parent_id")) if c.get("parent_id") else None) for c in fluxer_channels}
    
    channels_to_create = []
    channels_to_move = []
    
    for ch in all_channels:
        discord_id = str(ch.id)
        fluxer_id = context.state.get_fluxer_channel_id(discord_id)
        
        if force or not fluxer_id:
            # We'll resolve the parent_id in the loop after categories are created
            channels_to_create.append(ch)
        else:
            # Always add to move/sync list to ensure properties (topic, nsfw, slowmode) are synced
            # even if the parent category is already correct.
            channels_to_move.append((ch, fluxer_id))

    # 3. Threads. Forum/media posts are skipped here: Fluxer creates a post together with its
    # first message, so message migration creates them (keeping the original author).
    forum_types = {context.sync_reader.CHANNEL_TYPE_FORUM.value, 16}
    forum_ids = {str(ch.id) for ch in all_channels
                 if (ch.type.value if hasattr(ch.type, 'value') else ch.type) in forum_types}
    threads_to_create = [t for t in all_threads
                         if str(getattr(t, 'parent_id', None)) not in forum_ids
                         and (force or not context.state.get_fluxer_channel_id(str(t.id)))]

    # 4. Renamed/moved on Discord: existing categories and threads are edited only when they changed
    # (existing channels are always synced above). Forum posts count too once they exist.
    mapped_threads = [t for t in all_threads if t not in threads_to_create and context.state.get_fluxer_channel_id(str(t.id))]
    try:
        target_threads = await _target_threads(context, mapped_threads) if mapped_threads else []
    except Exception as e:
        logger.warning(f"Could not list Fluxer threads, renamed threads are not synced this run: {e}")
        target_threads, mapped_threads = [], []
    changed = changed_channel_ids(context, categories, all_channels, mapped_threads, fluxer_channels, target_threads)
    categories_to_sync = [cat for cat in categories if str(cat.id) not in missing_category_ids and str(cat.id) in changed]
    target_threads_by_id = {str(t.get("id")): t for t in target_threads}
    threads_to_rename = [(t, target_threads_by_id[known_target(context.state, str(t.id))])
                         for t in mapped_threads
                         if str(t.id) in changed and known_target(context.state, str(t.id)) in target_threads_by_id]

    total = (len(missing_categories) + len(categories_to_sync) + len(channels_to_create) + len(channels_to_move)
             + len(threads_to_create) + len(threads_to_rename))
    current_idx = 0
    
    if total == 0:
        return cloned_info

    # Migrate Categories first
    for cat in missing_categories:
        if not context.is_running: break
        
        state_key = str(cat.id)
        pos = getattr(cat, 'position', None)
        fluxer_id = await context.fluxer_writer.create_channel(cat.name, type=4, position=pos)
        context.state.set_category_mapping(state_key, fluxer_id)
        cloned_info["categories_created"].append(cat.name)
        if cat.name not in cloned_info["structure"]:
            cloned_info["structure"][cat.name] = []
        
        current_idx += 1
        if progress_callback: await progress_callback(f"Cat: {cat.name}", "Copying", current_idx, total)

    # Rename categories that changed on Discord (their order is set by the reorder step below)
    fluxer_names = {str(c["id"]): c.get("name") for c in fluxer_channels}
    for cat in categories_to_sync:
        if not context.is_running: break
        fluxer_id = str(context.state.get_fluxer_category_id(str(cat.id)))
        try:
            if fluxer_names.get(fluxer_id) != cat.name:
                await context.fluxer_writer.modify_channel(channel_id=fluxer_id, name=cat.name)
            cloned_info["updated"].append(cat.name)
        except Exception as e:
            logger.error(f"Failed to update category '{cat.name}' ({fluxer_id}): {e}")

        current_idx += 1
        if progress_callback: await progress_callback(f"Cat: {cat.name}", "Syncing", current_idx, total)

    # Create missing channels
    for channel in channels_to_create:
        if not context.is_running: break
            
        state_key = str(channel.id)
        topic = getattr(channel, 'topic', "") or ""
        nsfw = getattr(channel, 'nsfw', False)
        slowmode = getattr(channel, 'slowmode_delay', 0)
        
        logger.debug(f"Creating channel {channel.name}: topic={topic}, nsfw={nsfw}, slowmode={slowmode}")
        
        parent_id = context.state.get_fluxer_category_id(str(channel.category_id)) if channel.category_id else None
        pos = getattr(channel, 'position', None)
        
        # Map Discord-specific types to target-supported types
        # 5 (News) -> 0 (Text), and fallback any unknown non-voice types to text
        raw_type = channel.type.value if hasattr(channel.type, 'value') else 0
        if raw_type == context.sync_reader.CHANNEL_TYPE_VOICE.value:
            ch_type = 2         # GUILD_VOICE
            is_voice = True
        elif raw_type == context.sync_reader.CHANNEL_TYPE_FORUM.value:
            ch_type = 15  # GUILD_FORUM
            is_voice = False
        elif raw_type == context.sync_reader.CHANNEL_TYPE_THREAD_ANNOUNCEMENT.value:
            ch_type = 10  # THREAD_ANNOUNCEMENT
            is_voice = False
        elif raw_type == context.sync_reader.CHANNEL_TYPE_THREAD_PUBLIC.value:
            ch_type = 11  # THREAD_PUBLIC
            is_voice = False
        elif raw_type == context.sync_reader.CHANNEL_TYPE_THREAD_PRIVATE.value:
            ch_type = 12  # THREAD_PRIVATE
            is_voice = False
        elif raw_type == context.sync_reader.CHANNEL_TYPE_NEWS.value:
            ch_type = 5         # GUILD_ANNOUNCEMENT
            is_voice = False
        elif raw_type == context.sync_reader.CHANNEL_TYPE_TEXT.value:
            ch_type = 0         # GUILD_TEXT
            is_voice = False
        else:
            # Fallback for Stage channels (13) etc. to Text for safety
            ch_type = 0
            is_voice = False
        
        fluxer_id = await context.fluxer_writer.create_channel(
            name=channel.name, 
            topic=topic if not is_voice else "", 
            type=ch_type, 
            parent_id=parent_id,
            nsfw=nsfw if not is_voice else False,
            slowmode_delay=slowmode if not is_voice else 0,
            position=pos
        )
        context.state.set_channel_mapping(state_key, fluxer_id)
        cloned_info["channels_created"].append(channel.name)
        
        parent_name = cat_name_map.get(str(channel.category_id), "No Category") if channel.category_id else "No Category"
        if parent_name not in cloned_info["structure"]:
            cloned_info["structure"][parent_name] = []
        cloned_info["structure"][parent_name].append(channel.name)
        
        # Sync again immediately (only for non-voice channels)
        if not is_voice:
            await context.fluxer_writer.modify_channel(
                channel_id=fluxer_id,
                parent_id=parent_id,
                name=channel.name,
                topic=topic,
                nsfw=nsfw,
                slowmode_delay=slowmode
            )
        
        current_idx += 1
        if progress_callback: await progress_callback(channel.name, "Copying", current_idx, total)

    # Move/Sync existing channels
    for channel, fluxer_id in channels_to_move:
        if not context.is_running: break
        
        parent_id = context.state.get_fluxer_category_id(str(channel.category_id)) if channel.category_id else None
        if parent_id is None and fluxer_parent_map.get(str(fluxer_id)):
            # Moved out of its category: fluxer.py drops parent_id=None, and Fluxer reads 0 as "top level"
            parent_id = "0"
        nsfw = getattr(channel, 'nsfw', False)
        slowmode = getattr(channel, 'slowmode_delay', 0)
        topic = getattr(channel, 'topic', "") or ""
        
        logger.debug(f"Syncing existing channel {channel.name} ({fluxer_id}): topic={topic}, nsfw={nsfw}, slowmode={slowmode}")
        
        # Order is set by the reorder step below (PATCH /channels ignores position)
        await context.fluxer_writer.modify_channel(
            channel_id=fluxer_id, 
            parent_id=parent_id,
            name=channel.name,
            topic=topic,
            nsfw=nsfw,
            slowmode_delay=slowmode
        )
        
        cloned_info["channels_synced"].append(channel.name)
        if str(channel.id) in changed:
            cloned_info["updated"].append(channel.name)
        
        current_idx += 1
        if progress_callback: await progress_callback(channel.name, "Syncing", current_idx, total)

    # Put changed and new categories/channels in Discord's order, one request per affected parent
    # (failures don't roll back, so one refused group doesn't block the others)
    def group_of(item, kind) -> str:
        category = getattr(item, "category_id", None) if kind == CHANNEL else None
        return (known_target(context.state, str(category), CATEGORY) if category else None) or "root"

    created_ids = {str(c.id) for c in [*missing_categories, *channels_to_create]}
    groups = {group_of(item, kind) for kind, items in ((CATEGORY, categories), (CHANNEL, all_channels))
              for item in items if str(item.id) in changed or str(item.id) in created_ids}
    for group in sorted(groups):
        if not context.is_running: break
        entries = _reorder_entries(context, group, categories, all_channels, skip_ids=forum_ids)
        try:
            await context.fluxer_writer.reorder_channels(entries)
        except Exception as e:
            logger.error(f"Failed to reorder channels under {group}: {e}")

    # Create missing threads under their (now mapped) parents
    for thread in threads_to_create:
        if not context.is_running: break
        try:
            _, target_thread_id, _ = await resolve_target_thread(context, thread, force=force)
            if target_thread_id:
                cloned_info["threads_created"].append(thread.name)
            else:
                logger.warning(f"Skipping thread '{thread.name}' ({thread.id}): parent channel {getattr(thread, 'parent_id', None)} is not cloned")
        except Exception as e:
            # One failing thread (permissions, active thread limit) must not stop the rest
            logger.error(f"Failed to create thread '{thread.name}' ({thread.id}): {e}")

        current_idx += 1
        if progress_callback: await progress_callback(f"Thread: {thread.name}", "Copying", current_idx, total)

    # Rename threads/forum posts that were renamed on Discord
    for thread, target_thread in threads_to_rename:
        if not context.is_running: break
        try:
            await _rename_thread(context, target_thread, (thread.name or "thread")[:100])
            cloned_info["updated"].append(thread.name)
        except Exception as e:
            logger.error(f"Failed to rename thread '{thread.name}' ({target_thread.get('id')}): {e}")

        current_idx += 1
        if progress_callback: await progress_callback(f"Thread: {thread.name}", "Syncing", current_idx, total)

    return cloned_info
