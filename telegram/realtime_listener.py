"""
telegram/realtime_listener.py — Live Telethon event handler (text + image).
"""

from __future__ import annotations

import asyncio

from loguru import logger
from telethon import TelegramClient, events
from telethon.tl.types import User

from processing.media_processor import classify_message_media, download_media_bytes
from processing.message_queue import MessageQueue
from utils import clean_text, is_promo_message, make_message_id, to_iso8601


async def start_realtime_listener(
    client: TelegramClient,
    channels: list[str],
    queue: MessageQueue,
    stop_event: asyncio.Event,
) -> None:
    """Register NewMessage handler for all channels and run until stop_event is set."""
    from telegram.client import resolve_channel  # local import

    resolved_entities: dict[int, str] = {}

    for ch in channels:
        try:
            entity = await resolve_channel(client, ch)
            pid = entity.id
            name = getattr(entity, "username", None) or ch.lstrip("@")
            resolved_entities[pid] = name
            logger.info("Realtime listener registered for @{} (id={})", name, pid)
        except ValueError as exc:
            logger.error("Cannot resolve channel {!r} — skipping: {}", ch, exc)

    if not resolved_entities:
        logger.warning("No valid channels resolved for realtime listening.")
        return

    chats = list(resolved_entities.keys())

    @client.on(events.NewMessage(chats=chats))
    async def _handler(event: events.NewMessage.Event) -> None:
        sender = await event.get_sender()
        if isinstance(sender, User) and sender.bot:
            return

        msg = event.message
        text = (msg.message or "").strip()
        media_class, mime_type = classify_message_media(msg)

        if media_class == "skip":
            return
        if media_class == "none" and not text:
            return

        chat = await event.get_chat()
        channel_name = resolved_entities.get(chat.id, getattr(chat, "username", str(chat.id)))
        msg_id = make_message_id(channel_name, msg.id)
        ts = to_iso8601(msg.date)

        if media_class in ("photo", "image_doc"):
            media_bytes, actual_mime = await download_media_bytes(client, msg)
            if media_bytes is not None:
                payload: dict = {
                    "type": "image",
                    "message_id": msg_id,
                    "channel": f"@{channel_name}",
                    "timestamp": ts,
                    "text": clean_text(text) if text else None,
                    "media_bytes": media_bytes,
                    "mime_type": actual_mime or mime_type,
                }
            elif text:
                payload = {
                    "type": "text",
                    "message_id": msg_id,
                    "channel": f"@{channel_name}",
                    "timestamp": ts,
                    "text": clean_text(text),
                    "media_bytes": None,
                    "mime_type": None,
                }
            else:
                return
        else:
            # Pure text — apply promo pre-filter
            if is_promo_message(text):
                logger.debug("Promo/slop skipped (realtime) msg_id={}", msg_id)
                return
            payload = {
                "type": "text",
                "message_id": msg_id,
                "channel": f"@{channel_name}",
                "timestamp": ts,
                "text": clean_text(text),
                "media_bytes": None,
                "mime_type": None,
            }

        await queue.put(payload)
        logger.debug("Realtime: queued {} msg_id={} from @{}", payload["type"], msg_id, channel_name)

    logger.info("Realtime listener active on {} channel(s).", len(resolved_entities))
    await stop_event.wait()
    client.remove_event_handler(_handler)
    logger.info("Realtime listener stopped.")
