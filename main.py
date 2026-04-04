"""
main.py — Entry point for the Telegram Channel Intelligence System.

Usage:
    python main.py                  # Default: batch → then realtime
    python main.py --mode batch     # Historical fetch only
    python main.py --mode realtime  # Live listener only
    python main.py --mode both      # Same as default
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger

from config import BATCH_DAYS_BACK, BATCH_LIMIT, CHANNELS
from processing.database import init_db
from processing.message_queue import MessageQueue


def _configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> — "
            "<level>{message}</level>"
        ),
        colorize=True,
    )
    logger.add(
        "trading_intel.log",
        level="DEBUG",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
    )


async def run_batch(queue: MessageQueue, client) -> None:
    from telegram.batch_fetcher import run_batch_for_all_channels
    await run_batch_for_all_channels(client, CHANNELS, queue,
                                     limit=BATCH_LIMIT, days_back=BATCH_DAYS_BACK)


async def run_realtime(queue: MessageQueue, client, stop_event: asyncio.Event) -> None:
    from telegram.realtime_listener import start_realtime_listener
    await start_realtime_listener(client, CHANNELS, queue, stop_event)


async def _main(mode: str) -> None:
    _configure_logging()
    logger.info("=== Telegram Intelligence System starting — mode: {} ===", mode)

    init_db()
    queue = MessageQueue()
    llm_stop_event = asyncio.Event()
    realtime_stop_event = asyncio.Event()

    from telegram.client import disconnect_client, get_client
    client = await get_client()

    from processing.llm_processor import llm_worker

    if mode == "batch":
        llm_task = asyncio.create_task(llm_worker(queue, stop_event=llm_stop_event))
        await run_batch(queue, client)
        llm_stop_event.set()
        await llm_task
        logger.info("Batch mode complete.")

    elif mode == "realtime":
        llm_task = asyncio.create_task(llm_worker(queue, stop_event=llm_stop_event))
        try:
            await run_realtime(queue, client, realtime_stop_event)
        finally:
            llm_stop_event.set()
            await llm_task

    elif mode == "both":
        llm_task = asyncio.create_task(llm_worker(queue, stop_event=llm_stop_event))

        logger.info("--- Phase 1: Batch fetch ---")
        await run_batch(queue, client)
        logger.info("Waiting for LLM worker to drain queue…")
        await queue.join()
        logger.info("Queue drained — switching to real-time mode.")

        logger.info("--- Phase 2: Real-time listener ---")
        try:
            await run_realtime(queue, client, realtime_stop_event)
        finally:
            llm_stop_event.set()
            await llm_task

    else:
        logger.error("Unknown mode: {!r}", mode)
        sys.exit(1)

    await disconnect_client()
    logger.info("=== Telegram Intelligence System stopped cleanly. ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Channel Intelligence System")
    parser.add_argument("--mode", choices=["realtime", "batch", "both"],
                        default="both", help="Operating mode (default: both)")
    args = parser.parse_args()

    try:
        asyncio.run(_main(args.mode))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down gracefully.")
        sys.exit(0)


if __name__ == "__main__":
    main()
