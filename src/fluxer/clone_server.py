import asyncio
import logging
from typing import Callable, Awaitable

from src.core.base import MigrationContext

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


async def _sync_thread_state(context: MigrationContext, threads: list) -> tuple[int, int]:
    """
    Thread part of sync_channel_state: drops mappings whose Fluxer thread is gone and maps
    unmapped threads by name under their mapped parent. Returns (updates, removals).
    Skips entirely if Fluxer's thread lists can't be read, so no valid mapping is dropped.
    """
    parents = {str(context.state.get_fluxer_channel_id(str(t.parent_id))) for t in threads
               if getattr(t, "parent_id", None) and context.state.get_fluxer_channel_id(str(t.parent_id))}
    try:
        target_threads = list(await context.fluxer_writer.get_active_threads())
        channel_types = {str(c.get("id")): c.get("type") for c in await context.fluxer_writer.get_channels()}
        for parent in parents:
            target_threads += await context.fluxer_writer.get_archived_threads(parent)
            if channel_types.get(parent) == 0:
                target_threads += await context.fluxer_writer.get_archived_threads(parent, private=True)
    except Exception as e:
        logger.warning(f"Fluxer thread sync skipped, could not list threads: {e}")
        return 0, 0

    target_ids = {str(t.get("id")) for t in target_threads}
    by_parent_name = {(str(t.get("parent_id")), t.get("name")): str(t.get("id")) for t in target_threads}
    updates = removals = 0
    for t in threads:
        fluxer_id = context.state.get_fluxer_channel_id(str(t.id))
        if fluxer_id:
            if str(fluxer_id) not in target_ids:
                context.state.remove_channel_mapping(str(t.id))
                removals += 1
            continue
        parent = context.state.get_fluxer_channel_id(str(t.parent_id)) if getattr(t, "parent_id", None) else None
        match = by_parent_name.get((str(parent), t.name)) if parent else None
        if match:
            context.state.set_channel_mapping(str(t.id), match)
            updates += 1
    return updates, removals

async def sync_channel_state(context: MigrationContext):
    """
    Scans Fluxer for channels matching Discord names and updates state file mappings.
    This prevents duplicate creation when the state file is empty but channels exist in Fluxer.
    """
    categories = await context.sync_reader.get_categories()
    channels = await context.sync_reader.get_channels()
    fluxer_channels = await context.fluxer_writer.get_channels()
    
    # Build maps for Fluxer lookup
    # {name: id} for categories
    fluxer_cats = {c.get("name"): str(c.get("id")) for c in fluxer_channels if c.get("type") == 4}
    # {parent_id: {name: id}} for channels
    fluxer_structure = {}
    for c in fluxer_channels:
        if c.get("type") == 4: continue
        p_id = str(c.get("parent_id")) if c.get("parent_id") else "root"
        if p_id not in fluxer_structure: fluxer_structure[p_id] = {}
        fluxer_structure[p_id][c.get("name")] = str(c.get("id"))
    
    fluxer_id_set = {str(c.get("id")) for c in fluxer_channels}
    updates = 0
    removals = 0
    
    # 1. Sync Categories
    for cat in categories:
        discord_id = str(cat.id)
        fluxer_id = context.state.get_fluxer_category_id(discord_id)
        if fluxer_id:
            if str(fluxer_id) not in fluxer_id_set:
                context.state.remove_category_mapping(discord_id)
                removals += 1
        elif cat.name in fluxer_cats:
            context.state.set_category_mapping(discord_id, fluxer_cats[cat.name])
            updates += 1
                
    # 2. Sync Channels (parent-aware)
    for ch in channels:
        discord_id = str(ch.id)
        fluxer_id = context.state.get_fluxer_channel_id(discord_id)
        if fluxer_id:
            if str(fluxer_id) not in fluxer_id_set:
                context.state.remove_channel_mapping(discord_id)
                removals += 1
        else:
            # Try to match by name within the mapped parent category
            p_discord_id = str(ch.category_id) if ch.category_id else "root"
            p_fluxer_id = str(context.state.get_fluxer_category_id(p_discord_id)) if p_discord_id != "root" else "root"
            
            if p_fluxer_id in fluxer_structure and ch.name in fluxer_structure[p_fluxer_id]:
                context.state.set_channel_mapping(discord_id, fluxer_structure[p_fluxer_id][ch.name])
                updates += 1

    # 3. Sync Threads (after channels, so their parents are mapped)
    t_updates, t_removals = await _sync_thread_state(context, await context.sync_reader.get_threads(True))
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

    total = len(missing_categories) + len(channels_to_create) + len(channels_to_move) + len(threads_to_create)
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
                slowmode_delay=slowmode,
                position=pos
            )
        
        current_idx += 1
        if progress_callback: await progress_callback(channel.name, "Copying", current_idx, total)

    # Move/Sync existing channels
    for channel, fluxer_id in channels_to_move:
        if not context.is_running: break
        
        parent_id = context.state.get_fluxer_category_id(str(channel.category_id)) if channel.category_id else None
        nsfw = getattr(channel, 'nsfw', False)
        slowmode = getattr(channel, 'slowmode_delay', 0)
        topic = getattr(channel, 'topic', "") or ""
        
        logger.debug(f"Syncing existing channel {channel.name} ({fluxer_id}): topic={topic}, nsfw={nsfw}, slowmode={slowmode}")
        
        pos = getattr(channel, 'position', None)
        
        await context.fluxer_writer.modify_channel(
            channel_id=fluxer_id, 
            parent_id=parent_id,
            name=channel.name,
            topic=topic,
            nsfw=nsfw,
            slowmode_delay=slowmode,
            position=pos
        )
        
        cloned_info["channels_synced"].append(channel.name)
        
        current_idx += 1
        if progress_callback: await progress_callback(channel.name, "Syncing", current_idx, total)

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

    return cloned_info
