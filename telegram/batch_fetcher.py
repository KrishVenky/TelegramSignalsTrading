"""
telegram/batch_fetcher.py — Historical message fetching with media classification.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

from config import BATCH_DAYS_BACK, BATCH_LIMIT
from processing.media_processor import classify_message_media, download_media_bytes
from processing.message_queue import MessageQueue
from utils import clean_text, is_promo_message, make_message_id, to_iso8601


async def _build_queue_item(
    client: TelegramClient,
    msg: Message,
    channel_name: str,
) -> Optional[dict]:
    """Classify message and build unified queue item. Returns None for skipped types."""
    text = (msg.message or "").strip()
    media_class, mime_type = classify_message_media(msg)
    msg_id = make_message_id(channel_name, msg.id)
    ts = to_iso8601(msg.date)

    if media_class in ("photo", "image_doc"):
        media_bytes, actual_mime = await download_media_bytes(client, msg)
        if media_bytes is not None:
            return {
                "type": "image",
                "message_id": msg_id,
                "channel": f"@{channel_name}",
                "timestamp": ts,
                "text": clean_text(text) if text else None,
                "media_bytes": media_bytes,
                "mime_type": actual_mime or mime_type,
            }
        # Download failed — fall back to text if available
        if text:
            return {
                "type": "text",
                "message_id": msg_id,
                "channel": f"@{channel_name}",
                "timestamp": ts,
                "text": clean_text(text),
                "media_bytes": None,
                "mime_type": None,
            }
        return None

    if media_class == "skip":
        return None

    # Pure text
    if text:
        if is_promo_message(text):
            logger.debug("Promo/slop skipped (batch) msg_id={}", msg_id)
            return None
        return {
            "type": "text",
            "message_id": msg_id,
            "channel": f"@{channel_name}",
            "timestamp": ts,
            "text": clean_text(text),
            "media_bytes": None,
            "mime_type": None,
        }
    return None


async def fetch_history(
    client: TelegramClient,
    channel: str,
    queue: MessageQueue,
    limit: int = BATCH_LIMIT,
    days_back: int = BATCH_DAYS_BACK,
) -> int:
    """Fetch historical messages, classify them, and push to queue. Returns enqueued count."""
    from telegram.client import resolve_channel  # local import

    oldest_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    logger.info("Batch fetch: channel={} limit={} days_back={} (since {})",
                channel, limit, days_back, oldest_dt.strftime("%Y-%m-%d"))

    try:
        entity = await resolve_channel(client, channel)
    except ValueError as exc:
        logger.error("Cannot resolve channel {!r}: {}", channel, exc)
        return 0

    channel_name = getattr(entity, "username", None) or channel.lstrip("@")
    enqueued = skipped = fetched = 0
    raw_messages: list[Message] = []

    try:
        async for msg in client.iter_messages(entity, limit=limit):
            if msg.date and msg.date.replace(tzinfo=timezone.utc) < oldest_dt:
                break
            raw_messages.append(msg)
            fetched += 1
            if fetched % 100 == 0:
                logger.info("  … {} raw messages fetched from {}", fetched, channel)
    except FloodWaitError as exc:
        logger.warning("FloodWaitError: sleeping {} seconds…", exc.seconds)
        await asyncio.sleep(exc.seconds + 1)

    logger.info("Fetched {} raw messages from {}. Classifying…", fetched, channel)

    for msg in reversed(raw_messages):  # chronological order
        item = await _build_queue_item(client, msg, channel_name)
        if item is None:
            skipped += 1
            continue
        await queue.put(item)
        enqueued += 1

    logger.info("Batch complete: channel={} enqueued={} skipped={}", channel, enqueued, skipped)
    return enqueued


async def run_batch_for_all_channels(
    client: TelegramClient,
    channels: list[str],
    queue: MessageQueue,
    limit: int = BATCH_LIMIT,
    days_back: int = BATCH_DAYS_BACK,
) -> None:
    total = 0
    for ch in channels:
        n = await fetch_history(client, ch, queue, limit=limit, days_back=days_back)
        total += n
    logger.info("All channels batched. Total enqueued: {}", total)
