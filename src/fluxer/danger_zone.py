import logging
from typing import Callable, Awaitable

from src.core.base import MigrationContext

logger = logging.getLogger(__name__)

async def danger_remove_logo_and_banner(context: MigrationContext) -> dict:
    """Removes the target community's logo and banner image. Returns per-field status."""
    return await context.writer.remove_community_logo_and_banner()

async def danger_delete_all_channels(context: MigrationContext, progress_callback=None) -> int:
    """Deletes every channel and category in the target community."""
    count = await context.writer.delete_all_channels(progress_callback=progress_callback)
    context.state.clear_channel_mappings()
    context.state.clear_message_history()
    return count

async def danger_reset_channel_permissions(context: MigrationContext, progress_callback=None) -> int:
    """Resets all permission overwrites on every channel and category in the target community."""
    return await context.writer.reset_channel_permissions(progress_callback=progress_callback)

async def danger_delete_all_roles(context: MigrationContext, progress_callback=None) -> int:
    """Deletes all deletable roles in the target community."""
    count = await context.writer.delete_all_roles(progress_callback=progress_callback)
    context.state.clear_role_mappings()
    return count

async def danger_delete_all_emojis_and_stickers(context: MigrationContext, progress_callback=None) -> dict:
    """Deletes all custom emojis and stickers from the target community. Returns {"emojis": int, "stickers": int}."""
    counts = await context.writer.delete_all_emojis_and_stickers(progress_callback=progress_callback)
    context.state.clear_asset_mappings()
    return counts

async def danger_delete_removed_channels(context: MigrationContext, items: list[dict], progress_callback=None) -> int:
    """
    Deletes the Fluxer copies of categories/channels/threads that were deleted on Discord
    (items from clone_server.find_deleted_channels) and clears their mappings and message tracking.
    Threads go first, categories last. Returns the number of items removed.
    """
    order = {"thread": 0, "channel": 1, "category": 2}
    items = sorted(items, key=lambda i: order.get(i["kind"], 1))
    removed = 0
    for idx, item in enumerate(items, start=1):
        try:
            # False = already gone on Fluxer (e.g. a thread deleted together with its channel): clean up all the same
            await context.writer.delete_channel(item["target_id"])
        except Exception as e:
            logger.error(f"Failed to delete Fluxer {item['kind']} '{item['name']}' ({item['target_id']}): {e}")
            continue
        if item["kind"] == "category":
            context.state.remove_category_mapping(item["discord_id"])
        else:
            context.state.remove_channel_mapping(item["discord_id"])
            if item["kind"] == "channel":
                context.state.clear_channel_data(item["target_id"])
        removed += 1
        if progress_callback:
            await progress_callback(item["name"], idx, len(items))
    return removed
