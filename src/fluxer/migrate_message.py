import asyncio
import logging
import re
import json
import io
from typing import Callable, Awaitable, Dict, Any, List
from pathlib import Path

try:
    from lottie.objects import Animation
    from lottie.exporters.gif import export_gif
    HAS_LOTTIE = True
except ImportError:
    HAS_LOTTIE = False

from src.core.base import MigrationContext
from src.core.utils import resolve_discord_links
from src.fluxer.clone_server import resolve_target_thread, apply_thread_state

logger = logging.getLogger(__name__)

def clean_mentions(content: str, guild, user_mentions=None, role_mentions=None, channel_mentions=None, emoji_map=None, channel_map=None, state=None, target_server_id=None, channel_names=None, anonymize_users=False) -> str:
    if content is None:
        return ""
    if not content or not guild:
        return content
        
    def replace_user(match):
        uid = int(match.group(1))
        if anonymize_users and state:
            alias = state.get_user_alias(str(uid))
            return f"`@{alias}`" if alias else "`@Anonymized User`"
        
        # 1. Try provided guild
        member = guild.get_member(uid)
        if member:
            return f"`@{member.display_name}`"
        
        # 2. Try provided user_mentions
        if user_mentions:
            m = next((u for u in user_mentions if u.id == uid), None)
            if m:
                return f"`@{m.display_name}`"
        
        # 3. Try global cache via guild.client
        if hasattr(guild, 'client'):
            user = guild.client.get_user(uid)
            if user:
                return f"`@{user.name}`"
        return "`@Unknown User`"
        
    def replace_role(match):
        rid = int(match.group(1))
        # 0. Try native mapping first
        if state:
            target_role_id = state.get_target_role_id(str(rid))
            if target_role_id:
                return f"<@&{target_role_id}>"

        # 1. Try provided guild cache/list
        role = guild.get_role(rid) or next((r for r in guild.roles if r.id == rid), None)
        # 2. Try message's role_mentions
        if not role and role_mentions:
            role = next((r for r in role_mentions if r.id == rid), None)
        
        # 3. Try all guilds the client is aware of (fallback for cache issues)
        if not role and hasattr(guild, 'client'):
            for g in guild.client.guilds:
                role = g.get_role(rid)
                if role: break
        
        if role and role.name:
            return f"`@{role.name}`"
            
        return f"`@Unknown Role`"
        
    def replace_channel(match):
        cid = int(match.group(1))
        
        # 1. Check if channel is mapped in state
        if channel_map and cid in channel_map:
            return f"<#{channel_map[cid]}>"
            
        # 2. Try to resolve channel name from pre-fetched names
        name = None
        if channel_names and str(cid) in channel_names:
            name = channel_names[str(cid)]
        
        # 3. Try live lookup (fallback)
        if not name:
            try:
                channel = guild.get_channel(cid) or guild.get_thread(cid)
            except Exception:
                channel = None
            if not channel and channel_mentions:
                channel = next((c for c in channel_mentions if c.id == cid), None)
            if channel:
                name = channel.name

        if name:
            return f"`#{name}`"
            
        return f"<#{cid}>"

    def replace_emoji(match):
        animated = match.group(1) == "a"
        name = match.group(2)
        eid = int(match.group(3))
        
        if emoji_map and eid in emoji_map:
            target_eid = emoji_map[eid]
            prefix = "a" if animated else ""
            return f"<{prefix}:{name}:{target_eid}>"
        
        return f":{name}:"

    content = re.sub(r'<@!?([0-9]+)>', replace_user, content)
    content = re.sub(r'<@&([0-9]+)>', replace_role, content)
    content = re.sub(r'<#([0-9]+)>', replace_channel, content)
    content = re.sub(r'<(a?):([^:]+):([0-9]+)>', replace_emoji, content)
    content = content.replace("@everyone", "`@everyone`").replace("@here", "`@here`")

    # Resolve Discord Links
    if state and target_server_id:
        content = resolve_discord_links(content, state, "fluxer", target_server_id)

    return content


def _source_thread_parent_id(channel: Any) -> str | None:
    """Parent channel ID if channel is a thread (types 10/11/12), else None."""
    c_type = getattr(channel, "type", None)
    raw_type = c_type.value if hasattr(c_type, "value") else c_type
    parent_id = getattr(channel, "parent_id", None)
    return str(parent_id) if raw_type in (10, 11, 12) and parent_id else None


async def _is_forum_channel(context: MigrationContext, channel_id: int) -> bool:
    """True for forum/media channels: they hold posts (threads) but no messages of their own."""
    try:
        channel = await context.discord_reader.get_channel(int(channel_id))
    except Exception:
        return False
    c_type = getattr(channel, "type", None)
    raw_type = c_type.value if hasattr(c_type, "value") else c_type
    return raw_type in (context.discord_reader.CHANNEL_TYPE_FORUM.value, 16)


def _skip_archived(context: MigrationContext, thread: Any) -> bool:
    """True if thread is archived and this run migrates active threads only."""
    return not getattr(context, "include_archived_threads", True) and bool(getattr(thread, "archived", False))


async def _no_messages():
    return
    yield


async def get_channel_threads(reader: Any, channel_id: int, include_archived: bool = True) -> List[Any]:
    """Helper to fetch the threads of a channel from Live or Backup: active ones, plus archived ones if include_archived."""
    threads = []
    
    # 1. From Backup (BackupReader has 'db' attribute)
    if hasattr(reader, 'db') and hasattr(reader, 'threads'):
        for t in reader.threads:
            if t.parent_id == channel_id and (include_archived or not getattr(t, 'archived', False)):
                threads.append(t)
        return threads

    # 2. From live Discord
    if hasattr(reader, 'guild') and reader.guild:
        try:
            # Guild-wide active threads
            if hasattr(reader.guild, 'active_threads'):
                for t in await reader.guild.active_threads():
                    if t.parent_id == channel_id:
                        threads.append(t)
            
            # Archived threads for this specific channel (the slow part on big servers)
            channel = await reader.get_channel(channel_id) if include_archived else None
            if hasattr(channel, 'archived_threads'):
                # discord.py method
                async for t in channel.archived_threads(limit=None):
                    threads.append(t)
        except Exception as e:
            logger.debug(f"Could not fetch live threads for {channel_id}: {e}")
            
    return threads



async def _process_and_send_message(
    context: MigrationContext,
    msg: Any,
    target_channel_id: str,
    stats: Dict[str, Any],
    thread_id: str | None = None,
    parent_target_id: str | None = None,
    thread_name: str | None = None,
    processed_threads: set | None = None,
    thread_target: Dict[str, Any] | None = None
) -> str | None:
    """
    Internal helper to process a single Discord message (mentions, attachments, stickers)
    and send it to the Fluxer platform.
    thread_target: {"id": Fluxer thread ID, "post_name": forum post name} when thread_id is migrated
        into a real Fluxer thread (target_channel_id stays its parent). Without it, thread messages
        are sent inline into the parent channel with markers.
    """
    native_thread = bool(thread_id and thread_target and (thread_target.get("id") or thread_target.get("post_name")))
    # 1. Formatting
    content = msg.content or ""
    
    # Check for forwarded flag
    is_forwarded = False
    if hasattr(msg.flags, 'forwarded'):
        is_forwarded = msg.flags.forwarded
        
    # Always ensure alias is created/retrieved to populate user_alias table
    alias = context.state.get_user_alias(str(msg.author.id))
    anonymize_users = context.config.anonymize_users if hasattr(context, 'config') else False
    
    # Process Stickers
    files = []
    if hasattr(msg, 'stickers') and msg.stickers:
        for s in msg.stickers:
            try:
                sticker_data = await context.discord_reader.download_sticker(s)
                if not sticker_data: continue
                
                format_val = getattr(s, 'format', 'png')
                if hasattr(format_val, 'name'):
                    ext = format_val.name.lower()
                elif isinstance(format_val, int):
                    format_map = {1: 'png', 2: 'apng', 3: 'lottie', 4: 'gif'}
                    ext = format_map.get(format_val, 'png')
                else:
                    ext = str(format_val).lower()
                
                # Conversion logic (Simplified for unification)
                if ext == 'lottie' and HAS_LOTTIE:
                    try:
                        lottie_data = json.loads(sticker_data)
                        def _convert_lottie(data):
                            anim = Animation.load(data)
                            buf = io.BytesIO()
                            export_gif(anim, buf)
                            buf.seek(0)
                            return buf
                        gif_buf = await asyncio.to_thread(_convert_lottie, lottie_data)
                        from PIL import Image
                        def _convert_gif_to_webp(buf):
                            img = Image.open(buf)
                            w_buf = io.BytesIO()
                            if getattr(img, 'n_frames', 1) > 1:
                                img.save(w_buf, format='WEBP', save_all=True, loop=0, quality=80)
                            else:
                                img.save(w_buf, format='WEBP', quality=80)
                            return w_buf.getvalue()
                        sticker_data = await asyncio.to_thread(_convert_gif_to_webp, gif_buf)
                        ext = 'webp'
                    except Exception: ext = 'json'
                elif ext in ('apng', 'gif'):
                    try:
                        from PIL import Image
                        def _process_animated_sticker(data):
                            img = Image.open(io.BytesIO(data))
                            webp_buf = io.BytesIO()
                            if getattr(img, 'n_frames', 1) > 1:
                                img.save(webp_buf, format='WEBP', save_all=True, loop=0, quality=80)
                            else:
                                img.save(webp_buf, format='WEBP', quality=80)
                            return webp_buf.getvalue()
                        sticker_data = await asyncio.to_thread(_process_animated_sticker, sticker_data)
                        ext = 'webp'
                    except Exception: pass
                
                files.append({"filename": f"sticker_{s.name}_{s.id}.{ext}", "data": sticker_data})
                stats["attachments"] += 1
            except Exception as e:
                logger.error(f"Failed to download sticker {getattr(s, 'name', 'unknown')}: {e}")

    # Process Attachments
    attachments_to_process = list(msg.attachments)
    if is_forwarded and hasattr(msg, 'message_snapshots') and msg.message_snapshots:
        snapshot = msg.message_snapshots[0]
        if not content:
            content = snapshot.content
        attachments_to_process.extend(snapshot.attachments)

    for att in attachments_to_process:
        try:
            att_data = await context.discord_reader.download_attachment(att)
            files.append({"filename": att.filename, "data": att_data})
            stats["attachments"] += 1
        except Exception as e:
            logger.error(f"Failed to download attachment {att.filename}: {e}")

    # Clean Mentions
    content = clean_mentions(
        content=content,
        guild=context.discord_reader.guild,
        user_mentions=msg.mentions,
        role_mentions=msg.role_mentions,
        channel_mentions=msg.channel_mentions,
        emoji_map=context.state.emoji_map,
        channel_map=context.state.channel_map,
        state=context.state,
        target_server_id=context.fluxer_writer.community_id,
        channel_names=context.channel_names if hasattr(context, 'channel_names') else None,
        anonymize_users=anonymize_users
    )

    if not content and not files:
        return None

    # Reply Resolution
    reply_to_fluxer_id = None
    if msg.reference and msg.reference.message_id:
        if native_thread:
            # A reply must point into the same thread on Fluxer
            reply_to_fluxer_id = context.state.get_thread_message_id(target_channel_id, thread_id, str(msg.reference.message_id))
        else:
            reply_to_fluxer_id = context.state.get_fluxer_message_id(target_channel_id, str(msg.reference.message_id))
        
        # Fallback author tagging for replies if mapping not found
        if not reply_to_fluxer_id:
            try:
                source_ref_msg = await context.discord_reader.get_message(msg.channel.id, msg.reference.message_id)
                if source_ref_msg and source_ref_msg.author:
                    ref_name = context.state.get_user_alias(str(source_ref_msg.author.id)) if anonymize_users else source_ref_msg.author.display_name
                    content = f"`@{ref_name}`\n{content}"
                else:
                    tgt_reply = context.state.get_target_message_id(target_channel_id, msg.reference.message_id)
                    if tgt_reply: content = f"[Reply to {tgt_reply}]\n{content}"
            except Exception: pass

    # Thread logic (inline fallback when there is no Fluxer thread)
    if not native_thread:
        if not reply_to_fluxer_id and parent_target_id and stats["messages"] == 0:
            reply_to_fluxer_id = parent_target_id
        if thread_name and stats["messages"] == 0:
            content = f"> <<< THREAD: **{thread_name}** >>>\n{content}"

    # Send Message
    if anonymize_users:
        author_name = alias or "Anonymized User"
        author_avatar_url = None
    else:
        author_name = msg.author.display_name
        author_avatar_url = msg.author.avatar.url if hasattr(msg.author, 'avatar') and msg.author.avatar else None

    fluxer_msg_id = await context.fluxer_writer.send_message(
        channel_id=target_channel_id,
        author_name=author_name,
        author_avatar_url=author_avatar_url,
        content=content,
        timestamp=int(msg.created_at.timestamp()),
        files=files if files else None,
        reply_to_message_id=reply_to_fluxer_id,
        is_forwarded=is_forwarded,
        embeds=msg.embeds,
        thread_id=thread_target.get("id") if native_thread else None,
        thread_name=thread_target.get("post_name") if native_thread and not thread_target.get("id") else None
    )

    if fluxer_msg_id and native_thread and not thread_target.get("id"):
        # This message created the forum post; the post ID equals its first message's ID
        thread_target["id"], thread_target["post_name"] = fluxer_msg_id, None
        context.state.set_channel_mapping(thread_id, fluxer_msg_id)

    if fluxer_msg_id:
        if thread_id:
            context.state.set_thread_message_mapping(target_channel_id, thread_id, str(msg.id), fluxer_msg_id)
            context.state.update_thread_last_message_timestamp(target_channel_id, thread_id, str(msg.created_at))
            context.state.update_thread_last_message_id(target_channel_id, thread_id, str(msg.id))
            context.state.increment_thread_stats(target_channel_id, thread_id, messages=1, files=len(files) if files else 0)
        else:
            context.state.set_message_mapping(target_channel_id, str(msg.id), fluxer_msg_id)
            context.state.update_last_message_timestamp(target_channel_id, str(msg.created_at))
            context.state.update_last_message_id(target_channel_id, str(msg.id))
            context.state.increment_stats(target_channel_id, messages=1, files=len(files) if files else 0)

        stats["messages"] += 1
        stats["last_message_content"] = content
        stats["last_message_author"] = msg.author.display_name
        
    return fluxer_msg_id

async def analyze_migration(context: MigrationContext, source_channel_id: int, after_message_id: int | None = None, inclusive: bool = False, progress_callback: Callable[[Dict[str, Any]], Awaitable[None]] | None = None, processed_threads: set | None = None) -> Dict[str, int]:

    """
    Scans channel history to count messages, threads, and attachments.
    """
    stats = {"messages": 0, "threads": 0, "attachments": 0}
    
    # Forum channels have no messages of their own; count their posts (threads) below
    is_forum = processed_threads is None and await _is_forum_channel(context, source_channel_id)
    if processed_threads is None:
        processed_threads = set()

    history = _no_messages() if is_forum else context.discord_reader.fetch_message_history(source_channel_id, after_id=after_message_id, inclusive=inclusive)
    async for msg in history:
        if not context.is_running:
            break
        
        # Count thread messages and markers even if parent is skipped
        if hasattr(msg, 'thread') and msg.thread and not _skip_archived(context, msg.thread):
            thread = msg.thread
            if thread.id not in processed_threads:
                processed_threads.add(thread.id)
                stats["threads"] += 1
                
                # Fetch last migrated message ID for this thread
                target_channel_id = context.state.get_target_channel_id(str(source_channel_id))
                thread_after_id = None
                if target_channel_id:
                    thread_after_id = context.state.get_thread_last_message_id(target_channel_id, str(thread.id))
                
                # Recursively count thread content
                thread_stats = await analyze_migration(context, thread.id, after_message_id=int(thread_after_id) if thread_after_id else None, processed_threads=processed_threads)
                stats["messages"] += thread_stats["messages"]
                stats["attachments"] += thread_stats["attachments"]
                stats["threads"] += thread_stats["threads"] # Nested threads (rare in Discord but possible in forum channels)

        # Consistent filtering with migrate_messages
        if msg.type not in [
            context.discord_reader.MESSAGE_TYPE_DEFAULT,
            context.discord_reader.MESSAGE_TYPE_REPLY,
            context.discord_reader.MESSAGE_TYPE_THREAD_STARTER,
            context.discord_reader.MESSAGE_TYPE_FORWARD,
            context.discord_reader.MESSAGE_TYPE_CHAT_INPUT_COMMAND,
            context.discord_reader.MESSAGE_TYPE_CONTEXT_MENU_COMMAND,
            context.discord_reader.MESSAGE_TYPE_POLL_RESULT,
            context.discord_reader.MESSAGE_TYPE_AUTO_MODERATION_ACTION
        ]:
            logger.debug(f"Skipping message {msg.id} in analyze: type={msg.type} (not an allowed type)")
            continue

        stats["messages"] += 1
        stats["attachments"] += len(msg.attachments)
        logger.debug(f"Analyze msg {msg.id}: type={msg.type}, content={msg.content[:50]!r}...")

        if progress_callback and stats["messages"] % 10 == 0:
            await progress_callback(stats)

    # After scanning messages, explicitly check for any missed threads (e.g. archived or skipped in scan)
    # Only do this at the top level (not in recursive thread calls)
    if is_forum or after_message_id is not None or inclusive: # Usually top level calls have some start point
        # Optimization: We check all threads for the channel
        all_threads = await get_channel_threads(context.discord_reader, source_channel_id,
                                                include_archived=getattr(context, "include_archived_threads", True))
        for t in all_threads:
            if t.id not in processed_threads:
                processed_threads.add(t.id)
                stats["threads"] += 1
                
                # Fetch last migrated message ID for this thread
                target_channel_id = context.state.get_target_channel_id(str(source_channel_id))
                thread_after_id = None
                if target_channel_id:
                    thread_after_id = context.state.get_thread_last_message_id(target_channel_id, str(t.id))

                thread_stats = await analyze_migration(context, t.id, after_message_id=int(thread_after_id) if thread_after_id else None, processed_threads=processed_threads)
                stats["messages"] += thread_stats["messages"]
                stats["attachments"] += thread_stats["attachments"]
                stats["threads"] += thread_stats["threads"]

    return stats


async def migrate_messages(
    context: MigrationContext, 
    source_channel_id: int, 
    target_channel_id: str, 
    after_message_id: int | None = None, 
    inclusive: bool = False,
    progress_callback: Callable[[Dict[str, Any]], Awaitable[None]] | None = None,
    thread_id: str | None = None,
    parent_target_id: str | None = None,
    thread_name: str | None = None,
    processed_threads: set | None = None,
    thread: Any = None
) -> Dict[str, Any]:
    """Migrate messages for a specific channel and returns detailed statistics.
    thread: the source thread object when migrating a thread (source_channel_id == thread.id).
        Its messages go into the matching Fluxer thread, created if needed; parent_target_id is then
        the Fluxer message the thread starts from.
    """
    stats = {
        "messages": 0, 
        "threads": 0, 
        "attachments": 0,
        "first_message_url": "",
        "last_message_url": "",
        "last_message_content": "",
        "last_message_author": ""
    }
    
    logger.info(f"Starting message migration: Discord #{source_channel_id} -> Fluxer #{target_channel_id}")

    # Real Fluxer thread for this source thread; falls back to inline markers if it can't be created
    thread_target = None
    if thread_id and thread is not None:
        try:
            mapped_parent = context.state.get_fluxer_channel_id(str(getattr(thread, "parent_id", "")))
            if mapped_parent and str(mapped_parent) != str(target_channel_id):
                # The webhook belongs to target_channel_id; Fluxer only accepts threads of that channel
                logger.warning(f"Thread '{thread_name}' belongs to Fluxer channel {mapped_parent}, not the chosen target "
                               f"{target_channel_id}; sending it inline")
            else:
                _, target_thread_id, post_name = await resolve_target_thread(context, thread, parent_message_id=parent_target_id)
                if target_thread_id or post_name:
                    thread_target = {"id": target_thread_id, "post_name": post_name}
        except Exception as e:
            logger.error(f"Could not create Fluxer thread for '{thread_name}' ({thread_id}), sending inline: {e}")
    stats["native_thread"] = thread_target is not None
    if after_message_id:
        logger.info(f"Starting migration of {source_channel_id} (inclusive={inclusive})...")
    
    # Pre-fetch channel and thread names for better mention resolution
    if not hasattr(context, 'channel_names'):
        context.channel_names = {}
        try:
            logger.debug(f"Pre-fetching channel and thread names for guild {context.discord_reader.guild.id}...")
            # fetch_channels usually includes all non-thread channels
            all_channels = await context.discord_reader.fetch_channels()
            for c in all_channels:
                context.channel_names[str(c.id)] = c.name
            
            # get_active_threads helps find threads that might be mentioned
            threads = await context.discord_reader.get_active_threads()
            for t in threads:
                context.channel_names[str(t.id)] = t.name
            
            logger.debug(f"Pre-fetched {len(context.channel_names)} names.")
        except Exception as e:
            logger.debug(f"Failed to pre-fetch channel names: {e}")

    # Process missed threads first if resuming
    if processed_threads is None:
        processed_threads = set()

    async def _process_missed_threads():
        """Helper to scan for threads not yet processed in the current scan."""
        if not context.is_running:
            return
        logger.info(f"Checking for missed or pending threads in channel {source_channel_id}...")
        all_threads = sorted(await get_channel_threads(context.discord_reader, source_channel_id,
                                                       include_archived=getattr(context, "include_archived_threads", True)),
                             key=lambda t: int(t.id))
        for t in all_threads:
            if not context.is_running:
                break
            if t.id not in processed_threads:
                processed_threads.add(t.id)
                
                # Skip if thread was already fully migrated in a previous run
                if context.state.is_thread_completed(target_channel_id, str(t.id)):
                    logger.debug(f"Skipping already completed thread '{t.name}' (ID: {t.id})")
                    continue

                logger.info(f"Checking missed thread '{t.name}' (ID: {t.id})")
                
                # Fetch last migrated message ID for this thread
                thread_after_id = context.state.get_thread_last_message_id(target_channel_id, str(t.id))
                if thread_after_id:
                    logger.info(f"Resuming missed/pending thread '{t.name}' from after message ID: {thread_after_id}")

                stats["threads"] += 1
                thread_stats = await migrate_messages(
                    context=context,
                    source_channel_id=t.id,
                    target_channel_id=target_channel_id,
                    after_message_id=int(thread_after_id) if thread_after_id else None,
                    thread_id=str(t.id),
                    parent_target_id=None,
                    thread_name=t.name,
                    processed_threads=processed_threads,
                    thread=t
                )
                stats["messages"] += thread_stats["messages"]
                stats["attachments"] += thread_stats["attachments"]
                stats["threads"] += thread_stats["threads"]
                if progress_callback:
                    await progress_callback(stats)
                
                if context.is_running and not thread_stats.get("native_thread"):
                    await context.fluxer_writer.send_marker(
                        channel_id=target_channel_id,
                        content=f"> <<< END OF THREAD >>>"
                    )

    try:
        # If resuming (after_message_id is set) and at top level, check for pending threads FIRST
        # to preserve chronological order (finish old unfinished business first)
        # Forum channels have no messages of their own: every post is migrated as a thread
        is_forum = not thread_id and await _is_forum_channel(context, source_channel_id)
        if not thread_id and (is_forum or after_message_id is not None):
            await _process_missed_threads()

        history = _no_messages() if is_forum else context.discord_reader.fetch_message_history(source_channel_id, after_id=after_message_id, inclusive=inclusive)
        async for msg in history:
            if not context.is_running:
                logger.warning("Migration interrupted by user (is_running=False)")
                break
                


            # Skip system messages like "pinned a message", etc.
            logger.debug(f"Analyzing message {msg.id}: type={msg.type}, content_len={len(msg.content) if msg.content else 0}, attachments={len(msg.attachments)}, embeds={len(msg.embeds)}")
            if msg.type not in [
                context.discord_reader.MESSAGE_TYPE_DEFAULT,
                context.discord_reader.MESSAGE_TYPE_REPLY,
                context.discord_reader.MESSAGE_TYPE_THREAD_STARTER,
                context.discord_reader.MESSAGE_TYPE_FORWARD,
                context.discord_reader.MESSAGE_TYPE_CHAT_INPUT_COMMAND,
                context.discord_reader.MESSAGE_TYPE_CONTEXT_MENU_COMMAND,
                context.discord_reader.MESSAGE_TYPE_POLL_RESULT,
                context.discord_reader.MESSAGE_TYPE_AUTO_MODERATION_ACTION
            ]:
                # If we are skipping the parent, we STILL need to check for a thread!
                if hasattr(msg, 'thread') and msg.thread and not _skip_archived(context, msg.thread):
                    thread = msg.thread
                    if thread.id not in processed_threads:
                        processed_threads.add(thread.id)
                        # Track thread entry
                        stats["threads"] += 1
                        
                        # Fetch last migrated message ID for this thread
                        thread_after_id = context.state.get_thread_last_message_id(target_channel_id, str(thread.id))
                        if thread_after_id:
                            logger.info(f"Resuming thread '{thread.name}' from after message ID: {thread_after_id}")

                        # Migrate thread messages recursively
                        thread_stats = await migrate_messages(
                            context=context,
                            source_channel_id=thread.id,
                            target_channel_id=target_channel_id,
                            after_message_id=int(thread_after_id) if thread_after_id else None,
                            thread_id=str(thread.id),
                            parent_target_id=None,
                            thread_name=thread.name,
                            processed_threads=processed_threads,
                            thread=thread
                        )
                        stats["messages"] += thread_stats["messages"]
                        stats["attachments"] += thread_stats["attachments"]
                        stats["threads"] += thread_stats["threads"]
    
                        # Send End Marker
                        if context.is_running and not thread_stats.get("native_thread"):
                            await context.fluxer_writer.send_marker(
                                channel_id=target_channel_id,
                                content=f"> <<< END OF THREAD >>>"
                            )
                    
                if progress_callback:
                    await progress_callback(stats)
                continue
            else:
                # Use custom clean_mentions with msg mentions for accuracy
                content = clean_mentions(
                    msg.content, 
                    context.discord_reader.guild, 
                    msg.mentions, 
                    msg.role_mentions, 
                    msg.channel_mentions,
                    context.state.emoji_map,
                    context.state.channel_map,
                    state=context.state,
                    target_server_id=context.fluxer_writer.community_id,
                    channel_names=context.channel_names if hasattr(context, 'channel_names') else None,
                    anonymize_users=context.config.anonymize_users
                )
                logger.debug(f"Message {msg.id} cleaned content length: {len(content) if content else 0}")
                
            # Process attachments
            files = []
            attachments_to_process = list(msg.attachments)
            
            # Check if this message is forwarded
            # Discord flags: forwarded (is bit 28 / 0x10000000)
            is_forwarded = False
            if hasattr(msg.flags, 'forwarded'):
                is_forwarded = msg.flags.forwarded
            
            # If forwarded, the content and attachments might be in message_snapshots (discord.py 2.5+)
            # Note: If content was set by thread_starter_message, we don't overwrite it.
            if is_forwarded:
                logger.debug(f"Detected forwarded message: ID={msg.id}, Flags={msg.flags.value}")
                if hasattr(msg, 'message_snapshots') and msg.message_snapshots:
                    # For now we handle the first snapshot
                    snapshot = msg.message_snapshots[0]
                    if not content: # Only update content if it wasn't already set (e.g., by thread_starter_message)
                        content = snapshot.content
                        if context.discord_reader.guild:
                            content = clean_mentions(
                                content, 
                                context.discord_reader.guild, 
                                snapshot.mentions if hasattr(snapshot, 'mentions') else None,
                                snapshot.role_mentions if hasattr(snapshot, 'role_mentions') else None,
                                snapshot.channel_mentions if hasattr(snapshot, 'channel_mentions') else None, # Changed this line
                                context.state.emoji_map,
                                context.state.channel_map,
                                state=context.state,
                                target_server_id=context.fluxer_writer.community_id,
                                channel_names=context.channel_names if hasattr(context, 'channel_names') else None,
                                anonymize_users=context.config.anonymize_users
                            )
                    # Add snapshot attachments to the list to process
                    attachments_to_process.extend(snapshot.attachments)
                    logger.debug(f"Found forwarded snapshot content: {content[:50]}... and {len(snapshot.attachments)} attachments")

            for att in attachments_to_process:
                try:
                    att_data = await context.discord_reader.download_attachment(att)
                    files.append({"filename": att.filename, "data": att_data})
                    stats["attachments"] += 1
                except Exception as e:
                    logger.error(f"Failed to download attachment {att.filename}: {e}")
            
            # Process stickers as attachments
            if hasattr(msg, 'stickers') and msg.stickers:
                for s in msg.stickers:
                    try:
                        sticker_data = await context.discord_reader.download_sticker(s)
                        if sticker_data:
                            # Use format to determine extension
                            format_val = getattr(s, 'format', 'png')
                            logger.debug(f"Sticker {getattr(s, 'name', 'unknown')} format_val type: {type(format_val)}, value: {format_val}")
                            
                            if hasattr(format_val, 'name'): # discord.py StickerFormat enum
                                ext = format_val.name.lower()
                            elif isinstance(format_val, int):
                                format_map = {1: 'png', 2: 'apng', 3: 'lottie', 4: 'gif'}
                                ext = format_map.get(format_val, 'png')
                            else:
                                ext = str(format_val).lower()
                            
                            logger.debug(f"Determined sticker extension: {ext}")
                            
                            # Fluxer: Convert animated stickers to WebP
                            # Lottie (json) → GIF (via lottie lib) → WebP (via Pillow)
                            if ext == 'lottie':
                                if HAS_LOTTIE:
                                    try:
                                        logger.debug(f"Converting Lottie sticker {s.name} (ID: {s.id}) to WebP...")
                                        lottie_data = json.loads(sticker_data)
                                        
                                        def _convert_lottie(data):
                                            anim = Animation.load(data)
                                            buf = io.BytesIO()
                                            export_gif(anim, buf)
                                            buf.seek(0)
                                            return buf

                                        gif_buf = await asyncio.to_thread(_convert_lottie, lottie_data)
                                        
                                        # GIF → WebP via Pillow
                                        from PIL import Image
                                        
                                        def _convert_gif_to_webp(buf):
                                            img = Image.open(buf)
                                            w_buf = io.BytesIO()
                                            if getattr(img, 'n_frames', 1) > 1:
                                                img.save(w_buf, format='WEBP', save_all=True, loop=0, quality=80)
                                            else:
                                                img.save(w_buf, format='WEBP', quality=80)
                                            return w_buf.getvalue()

                                        sticker_data = await asyncio.to_thread(_convert_gif_to_webp, gif_buf)
                                        ext = 'webp'
                                        logger.debug(f"Successfully converted Lottie sticker {s.name} to WebP")
                                    except Exception as conv_err:
                                        logger.error(f"Failed to convert Lottie sticker {s.name} to WebP: {conv_err}")
                                        ext = 'json'
                                else:
                                    logger.warning(f"Lottie library not available, sending sticker {s.name} as raw JSON")
                                    ext = 'json'
                            
                            elif ext in ('apng', 'gif'):
                                try:
                                    logger.debug(f"Converting {ext.upper()} sticker {s.name} (ID: {s.id}) to WebP...")
                                    from PIL import Image
                                    
                                    def _process_animated_sticker(data):
                                        img = Image.open(io.BytesIO(data))
                                        webp_buf = io.BytesIO()
                                        if getattr(img, 'n_frames', 1) > 1:
                                            img.save(webp_buf, format='WEBP', save_all=True, loop=0, quality=80)
                                        else:
                                            img.save(webp_buf, format='WEBP', quality=80)
                                        return webp_buf.getvalue()

                                    sticker_data = await asyncio.to_thread(_process_animated_sticker, sticker_data)
                                    ext = 'webp'
                                    logger.debug(f"Successfully converted sticker {s.name} to WebP")
                                except Exception as conv_err:
                                    logger.error(f"Failed to convert {ext.upper()} sticker {s.name} to WebP: {conv_err}")
                                    # Keep original format as fallback
                            
                            filename = f"sticker_{s.name}_{s.id}.{ext}"
                            sticker_size = len(sticker_data)
                            files.append({"filename": filename, "data": sticker_data})
                            stats["attachments"] += 1
                            logger.debug(f"Added sticker {s.name} as attachment (extension: {ext}, size: {sticker_size} bytes)")
                    except Exception as e:
                        logger.error(f"Failed to download sticker {getattr(s, 'name', 'unknown')}: {e}")
                       # Check for existing mapping to avoid duplicates when resuming
            if context.state.get_target_message_id(target_channel_id, str(msg.id)):
                continue
                
            try:
                fluxer_msg_id = await _process_and_send_message(
                    context=context,
                    msg=msg,
                    target_channel_id=target_channel_id,
                    stats=stats,
                    thread_id=thread_id,
                    parent_target_id=parent_target_id,
                    thread_name=thread_name,
                    processed_threads=processed_threads,
                    thread_target=thread_target
                )

                # Check for associated thread (Individual mode recursion)
                if hasattr(msg, 'thread') and msg.thread and not _skip_archived(context, msg.thread):
                    thread = msg.thread
                    if thread.id not in processed_threads:
                        processed_threads.add(thread.id)
                        stats["threads"] += 1
                        
                        thread_after_id = context.state.get_thread_last_message_id(target_channel_id, str(thread.id))
                        thread_stats = await migrate_messages(
                            context=context,
                            source_channel_id=thread.id,
                            target_channel_id=target_channel_id,
                            after_message_id=int(thread_after_id) if thread_after_id else None,
                            thread_id=str(thread.id),
                            parent_target_id=fluxer_msg_id,
                            thread_name=thread.name,
                            processed_threads=processed_threads,
                            thread=thread
                        )
                        stats["messages"] += thread_stats["messages"]
                        stats["attachments"] += thread_stats["attachments"]
                        stats["threads"] += thread_stats["threads"]
                        
                        if context.is_running and not thread_stats.get("native_thread"):
                            await context.fluxer_writer.send_marker(
                                channel_id=target_channel_id,
                                content=f"> <<< END OF THREAD >>>"
                            )
                
                # Update Link Tracking (Parent pointer updates)
                if not stats["first_message_url"]:
                    stats["first_message_url"] = msg.jump_url
                stats["last_message_url"] = msg.jump_url
                
                if progress_callback:
                    await progress_callback(stats)
            except Exception as e:
                logger.error(f"Failed to process message {msg.id}: {e}")
                import traceback
                logger.error(traceback.format_exc())
        
        # Mark thread as completed if we finished the loop without being interrupted
        if thread_id and context.is_running:
            context.state.update_thread_completed(target_channel_id, thread_id, completed=True)
            logger.info(f"Thread '{thread_name}' (ID: {thread_id}) marked as completed.")
            if thread_target and thread_target.get("id"):
                await apply_thread_state(context, thread, thread_target["id"])
        

    except (KeyboardInterrupt, asyncio.CancelledError):
        context.is_running = False
        pass
    
    return stats


async def analyze_global_migration(context: MigrationContext, after_message_id: int | None = None, inclusive: bool = False, progress_callback: Callable[[Dict[str, Any]], Awaitable[None]] | None = None) -> Dict[str, int]:
    """
    Scans the entire server history to count messages, threads, and attachments globally.
    """
    stats = {"messages": 0, "threads": 0, "attachments": 0}
    
    # In global mode, thread messages are returned natively in timestamp order by global fetch if they're in the DB
    # However we just count them if the fetcher yields them.
    # Fetch global progress map to skip migrated messages efficiently
    progress_map = context.state.get_all_last_message_ids()
    
    async for msg in context.discord_reader.fetch_global_message_history(after_id=after_message_id):
        if not context.is_running:
            break
            
        # Determine target channel to check for existing mapping
        if not msg.channel:
            continue
        if _source_thread_parent_id(msg.channel) and _skip_archived(context, msg.channel):
            continue
            
        target_channel_id = context.state.get_target_channel_id(str(msg.channel.id))
        if not target_channel_id:
            continue

        # Efficient skip: if message ID is <= last migrated ID for this channel/thread
        # This is the primary resume mechanism: wait until we pass the last migrated ID for this channel
        last_id = progress_map.get(str(msg.channel.id))
        if last_id and msg.id <= int(last_id):
            continue
            
        if msg.type not in [
            context.discord_reader.MESSAGE_TYPE_DEFAULT,
            context.discord_reader.MESSAGE_TYPE_REPLY,
            context.discord_reader.MESSAGE_TYPE_THREAD_STARTER,
            context.discord_reader.MESSAGE_TYPE_FORWARD,
            context.discord_reader.MESSAGE_TYPE_CHAT_INPUT_COMMAND,
            context.discord_reader.MESSAGE_TYPE_CONTEXT_MENU_COMMAND,
            context.discord_reader.MESSAGE_TYPE_POLL_RESULT,
            context.discord_reader.MESSAGE_TYPE_AUTO_MODERATION_ACTION
        ]:
            continue
            
        stats["messages"] += 1
        stats["attachments"] += len(msg.attachments)
        if hasattr(msg, 'thread') and msg.thread:
            # We don't recursively traverse here, we just count the fact there is a thread
            # The actual thread messages are also fetched by the global fetcher because they have their own timestamp/id
            stats["threads"] += 1
            
        if progress_callback and stats["messages"] % 100 == 0:
            await progress_callback(stats)
            
    if progress_callback:
        await progress_callback(stats)
        
    return stats


async def migrate_global_messages(
    context: MigrationContext,
    after_message_id: int | None = None,
    inclusive: bool = False,
    progress_callback: Callable[[Dict[str, Any]], Awaitable[None]] | None = None
) -> Dict[str, Any]:
    """
    Migrates messages across all channels chronologically to Fluxer.
    """
    stats = {
        "messages": 0,
        "threads": 0,
        "attachments": 0,
        "last_message_content": "",
        "last_message_author": "",
        "first_message_url": None,
        "last_message_url": None
    }
    
    processed_threads = set()
    thread_targets: Dict[str, Dict[str, Any] | None] = {}  # source thread ID -> Fluxer thread target
    logger.info("Starting Global Waterfall Migration for Fluxer...")
    
    # Fetch global progress map to skip migrated messages efficiently
    progress_map = context.state.get_all_last_message_ids()

    try:
        async for msg in context.discord_reader.fetch_global_message_history(after_id=after_message_id):
            if not context.is_running:
                logger.warning("Global migration interrupted by user")
                break
                
            if msg.type not in [
                context.discord_reader.MESSAGE_TYPE_DEFAULT,
                context.discord_reader.MESSAGE_TYPE_REPLY,
                context.discord_reader.MESSAGE_TYPE_THREAD_STARTER,
                context.discord_reader.MESSAGE_TYPE_FORWARD,
                context.discord_reader.MESSAGE_TYPE_CHAT_INPUT_COMMAND,
                context.discord_reader.MESSAGE_TYPE_CONTEXT_MENU_COMMAND,
                context.discord_reader.MESSAGE_TYPE_POLL_RESULT,
                context.discord_reader.MESSAGE_TYPE_AUTO_MODERATION_ACTION
            ]:
                continue
                
            # Determine target channel
            if not msg.channel:
                continue

            # Thread messages go into the Fluxer thread via its parent channel (which owns the webhook)
            parent_src_id = _source_thread_parent_id(msg.channel)
            if parent_src_id:
                if _skip_archived(context, msg.channel):
                    continue
                target_channel_id = context.state.get_target_channel_id(parent_src_id)
                if not target_channel_id:
                    continue
                src_thread_id = str(msg.channel.id)
                last_id = context.state.get_thread_last_message_id(target_channel_id, src_thread_id)
                if last_id and msg.id <= int(last_id):
                    continue
                if src_thread_id not in thread_targets:
                    thread_targets[src_thread_id] = None
                    try:
                        _, target_thread_id, post_name = await resolve_target_thread(context, msg.channel)
                        if target_thread_id or post_name:
                            thread_targets[src_thread_id] = {"id": target_thread_id, "post_name": post_name, "thread": msg.channel}
                    except Exception as e:
                        logger.error(f"Could not create Fluxer thread for '{msg.channel.name}' ({src_thread_id}): {e}")
                if thread_targets[src_thread_id] is None:
                    continue
                if src_thread_id not in processed_threads:
                    processed_threads.add(src_thread_id)
                    stats["threads"] += 1
                try:
                    await _process_and_send_message(
                        context=context,
                        msg=msg,
                        target_channel_id=target_channel_id,
                        stats=stats,
                        thread_id=src_thread_id,
                        thread_name=msg.channel.name,
                        processed_threads=processed_threads,
                        thread_target=thread_targets[src_thread_id]
                    )
                    if not stats["first_message_url"]:
                        stats["first_message_url"] = msg.jump_url
                    stats["last_message_url"] = msg.jump_url
                    if progress_callback:
                        await progress_callback(stats)
                except Exception as e:
                    logger.error(f"Failed to process global thread message {msg.id}: {e}")
                continue

            target_channel_id = context.state.get_target_channel_id(str(msg.channel.id))
            if not target_channel_id:
                continue

            # Efficient skip: if message ID is <= last migrated ID for this channel/thread
            # This ensures we only resume a channel once we reach its last known progress point
            last_id = progress_map.get(str(target_channel_id))
            if last_id and msg.id <= int(last_id):
                continue
                
            # If it's a thread message, we need to handle it based on if it's the thread starter or a reply
            if hasattr(msg, 'thread') and msg.thread and msg.id == msg.thread.id:
                processed_threads.add(msg.thread.id)
                stats["threads"] += 1

            try:
                await _process_and_send_message(
                    context=context,
                    msg=msg,
                    target_channel_id=target_channel_id,
                    stats=stats,
                    processed_threads=processed_threads
                )

                if not stats["first_message_url"]:
                    stats["first_message_url"] = msg.jump_url
                stats["last_message_url"] = msg.jump_url
                
                if progress_callback:
                    await progress_callback(stats)
                    
            except Exception as e:
                logger.error(f"Failed to process global message {msg.id}: {e}")

        # Archive/lock threads like on Discord once all their messages are in
        if context.is_running:
            for target in thread_targets.values():
                if target and target.get("id"):
                    await apply_thread_state(context, target["thread"], target["id"])
                
    except (KeyboardInterrupt, asyncio.CancelledError):
        context.is_running = False
        pass
        
    return stats
