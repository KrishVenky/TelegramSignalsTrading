"""
processing/media_processor.py — In-memory Telegram media download.
"""

from __future__ import annotations

import io
from typing import Optional

from loguru import logger
from telethon import TelegramClient
from telethon.tl.types import (
    Document,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
)


def _is_image_document(doc: Document) -> tuple[bool, str]:
    mime = (doc.mime_type or "").lower()
    return mime.startswith("image/"), mime


def classify_message_media(msg: Message) -> tuple[str, str]:
    """
    Returns (media_class, mime_type).

    media_class: "photo" | "image_doc" | "skip" | "none"
    """
    media = msg.media

    if media is None:
        return "none", ""

    if isinstance(media, MessageMediaPhoto) and isinstance(media.photo, Photo):
        return "photo", "image/jpeg"

    if isinstance(media, MessageMediaDocument) and isinstance(media.document, Document):
        is_img, mime = _is_image_document(media.document)
        if is_img:
            return "image_doc", mime
        logger.debug("Skipping unsupported document mime={} msg_id={}", media.document.mime_type, msg.id)
        return "skip", mime

    logger.debug("Skipping unsupported media type={} msg_id={}", type(msg.media).__name__, msg.id)
    return "skip", ""


async def download_media_bytes(
    client: TelegramClient,
    msg: Message,
) -> tuple[Optional[bytes], str]:
    """Download image into memory. Returns (bytes, mime_type) or (None, "")."""
    media_class, mime_type = classify_message_media(msg)

    if media_class in ("none", "skip"):
        return None, ""

    try:
        buf = io.BytesIO()
        await client.download_media(msg.media, file=buf)
        raw = buf.getvalue()
        buf.close()

        if not raw:
            logger.warning("Empty media download for msg_id={}", msg.id)
            return None, ""

        logger.debug("Downloaded {} bytes ({}) for msg_id={}", len(raw), mime_type, msg.id)
        return raw, mime_type

    except Exception as exc:  # noqa: BLE001
        logger.error("Media download failed for msg_id={}: {}", msg.id, exc)
        return None, ""
