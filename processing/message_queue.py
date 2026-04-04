"""
processing/message_queue.py — Async queue (unified text + image schema).
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from config import LLM_BATCH_SIZE, QUEUE_CONSUMER_TIMEOUT


class MessageQueue:
    """
    Async queue with typed producer/consumer helpers.

    Unified item schema:
        {
            "type":        "text" | "image",
            "message_id":  str,
            "channel":     str,
            "timestamp":   str  (ISO-8601),
            "text":        str | None,
            "media_bytes": bytes | None,
            "mime_type":   str | None,
        }
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)

    async def put(self, message: dict[str, Any]) -> None:
        await self._queue.put(message)

    def put_nowait(self, message: dict[str, Any]) -> None:
        self._queue.put_nowait(message)

    async def get_batch(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Drain the queue into (text_batch, image_items).

        Waits up to QUEUE_CONSUMER_TIMEOUT for the first item, then greedily
        collects up to LLM_BATCH_SIZE text items without blocking.
        Returns ([], []) on timeout with empty queue.
        """
        text_batch: list[dict[str, Any]] = []
        image_items: list[dict[str, Any]] = []

        try:
            first = await asyncio.wait_for(
                self._queue.get(), timeout=QUEUE_CONSUMER_TIMEOUT
            )
            self._queue.task_done()
            _route(first, text_batch, image_items)
        except asyncio.TimeoutError:
            return [], []

        while len(text_batch) < LLM_BATCH_SIZE:
            try:
                item = self._queue.get_nowait()
                self._queue.task_done()
                _route(item, text_batch, image_items)
            except asyncio.QueueEmpty:
                break

        logger.debug("Queue drained: {} text, {} image item(s)", len(text_batch), len(image_items))
        return text_batch, image_items

    def qsize(self) -> int:
        return self._queue.qsize()

    async def join(self) -> None:
        await self._queue.join()

    def empty(self) -> bool:
        return self._queue.empty()


def _route(
    item: dict[str, Any],
    text_batch: list[dict[str, Any]],
    image_items: list[dict[str, Any]],
) -> None:
    if item.get("type") == "image":
        image_items.append(item)
    else:
        text_batch.append(item)
