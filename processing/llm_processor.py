"""
processing/llm_processor.py — Groq (LLaMA) signal extraction.

Rate-limit retries respect Groq's suggested retry delay from the 429 message.

Text messages  → batched → llama-3.3-70b-versatile
Image messages → individual vision calls → meta-llama/llama-4-scout-17b-16e-instruct

Concurrency is capped by GROQ_MAX_CONCURRENT (asyncio.Semaphore) to stay
within Groq's free-tier rate limits.
"""

from __future__ import annotations

import asyncio
import base64
import re
import json
import os
from typing import Any, Optional

from dotenv import load_dotenv
from groq import AsyncGroq
from loguru import logger

from config import (
    GROQ_MAX_CONCURRENT,
    GROQ_TEXT_MODEL,
    GROQ_VISION_MODEL,
    LLM_MAX_RETRIES,
    LLM_RETRY_BASE_DELAY,
    LLM_SYSTEM_PROMPT,
    LLM_USER_PROMPT_TEMPLATE,
)
from utils import now_iso8601, strip_json_fences

load_dotenv()

# Shared async client (one instance for the whole process)
_groq: AsyncGroq = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])

# Semaphore: limits concurrent Groq API calls to avoid rate limiting
_sem: asyncio.Semaphore = asyncio.Semaphore(GROQ_MAX_CONCURRENT)

_IMAGE_EXTRACTION_PROMPT = (
    "You are a financial signal extraction engine analyzing a Telegram channel image. "
    "This image is from a finance/trading signal channel. "
    "Extract any trading intelligence visible in the image.\n\n"
    "Look for: stock/crypto tickers, buy/sell calls, price targets, stop loss levels, "
    "chart patterns, sentiment indicators, any text overlaid on the image.\n\n"
    "Return ONLY a raw JSON object with these exact fields:\n"
    "message_id, channel, timestamp, ticker, action, entry_price, target_price, "
    "stop_loss, sentiment, confidence, timeframe, summary, raw_message\n\n"
    "For raw_message: describe what you see in the image in plain text.\n"
    "For fields you cannot determine: use null.\n"
    "No markdown. No explanation. JSON only."
)


# ---------------------------------------------------------------------------
# Core Groq call with retry + semaphore
# ---------------------------------------------------------------------------

def _parse_retry_delay(exc: Exception, fallback: float) -> float:
    """
    Extract Groq's suggested retry delay from the 429 error message.
    Handles formats like "Please try again in 2s" or "Please try again in 576ms".
    Falls back to *fallback* if parsing fails.
    """
    msg = str(exc)
    m = re.search(r"Please try again in (\d+(?:\.\d+)?)(ms|s)", msg)
    if m:
        value, unit = float(m.group(1)), m.group(2)
        return (value / 1000) if unit == "ms" else value
    return fallback


async def _groq_chat(
    messages: list[dict],
    model: str,
) -> str:
    """
    Call Groq chat completions with retry.
    On 429, sleeps for Groq's suggested delay (parsed from error message).
    Concurrency is bounded by _sem.
    """
    last_exc: Exception | None = None

    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            async with _sem:
                response = await _groq.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=4096,
                )
            return response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            fallback = LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            delay = _parse_retry_delay(exc, fallback)
            # Add a small buffer on top of Groq's suggested delay.
            delay = max(delay + 0.5, fallback)
            logger.warning(
                "Groq attempt {}/{} failed ({}): {}. Retrying in {:.1f}s…",
                attempt, LLM_MAX_RETRIES, model, exc, delay,
            )
            await asyncio.sleep(delay)

    raise RuntimeError(f"Groq failed after {LLM_MAX_RETRIES} attempts") from last_exc


def _parse_json(raw: str, expect_array: bool = True) -> Any:
    cleaned = strip_json_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("JSON parse failed (array={}): {}. Raw: {!r}", expect_array, exc, cleaned[:500])
        return None


# ---------------------------------------------------------------------------
# Text batch extraction
# ---------------------------------------------------------------------------

async def extract_text_signals(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch text messages → single Groq call → list of signal dicts."""
    if not messages:
        return []

    safe = [{k: v for k, v in m.items() if k != "media_bytes"} for m in messages]
    user_content = LLM_USER_PROMPT_TEMPLATE.format(
        messages_json=json.dumps(safe, ensure_ascii=False, indent=2)
    )

    groq_messages = [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        raw = await _groq_chat(groq_messages, model=GROQ_TEXT_MODEL)
    except Exception as exc:
        logger.error("Text batch Groq call failed: {}", exc)
        return []

    parsed = _parse_json(raw, expect_array=True)
    if not isinstance(parsed, list):
        logger.error("Expected JSON array for text batch, got {}", type(parsed).__name__)
        return []

    signals = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if "message_id" in item:
            item["message_id"] = str(item["message_id"])
        item["message_type"] = "text"
        signals.append(item)

    logger.info("Text batch: {} signal(s) from {} message(s)", len(signals), len(messages))
    return signals


# ---------------------------------------------------------------------------
# Image (vision) extraction
# ---------------------------------------------------------------------------

def _minimal_image_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": item["message_id"],
        "channel": item["channel"],
        "timestamp": item["timestamp"],
        "ticker": None, "action": None,
        "entry_price": None, "target_price": None, "stop_loss": None,
        "sentiment": "NEUTRAL", "confidence": "LOW", "timeframe": None,
        "summary": "unprocessable image",
        "raw_message": item.get("text") or "(image)",
        "message_type": "image",
        "processed_at": now_iso8601(),
    }


async def extract_image_signal(item: dict[str, Any]) -> dict[str, Any]:
    """Process one image queue item via Groq vision. Never raises — falls back to minimal record."""
    media_bytes: Optional[bytes] = item.get("media_bytes")
    mime_type: str = item.get("mime_type") or "image/jpeg"

    if not media_bytes:
        logger.warning("No bytes for image message_id={}", item["message_id"])
        return _minimal_image_record(item)

    b64_data = base64.b64encode(media_bytes).decode("utf-8")
    image_url = f"data:{mime_type};base64,{b64_data}"

    caption_note = f"\n\nCaption from the post: {item['text']}" if item.get("text") else ""
    prompt_text = (
        _IMAGE_EXTRACTION_PROMPT
        + caption_note
        + f"\n\nmessage_id: {item['message_id']}"
        + f"\nchannel: {item['channel']}"
        + f"\ntimestamp: {item['timestamp']}"
    )

    groq_messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt_text},
            ],
        }
    ]

    try:
        raw = await _groq_chat(groq_messages, model=GROQ_VISION_MODEL)
    except Exception as exc:
        logger.error("Vision Groq call failed for message_id={}: {}", item["message_id"], exc)
        return _minimal_image_record(item)

    parsed = _parse_json(raw, expect_array=False)
    if not isinstance(parsed, dict):
        logger.warning(
            "Non-dict vision response for message_id={}. Using minimal record.", item["message_id"]
        )
        return _minimal_image_record(item)

    if "message_id" in parsed:
        parsed["message_id"] = str(parsed["message_id"])
    parsed.setdefault("message_id", item["message_id"])
    parsed.setdefault("channel", item["channel"])
    parsed.setdefault("timestamp", item["timestamp"])
    parsed["message_type"] = "image"

    logger.info("Image signal extracted for message_id={}", item["message_id"])
    return parsed


# ---------------------------------------------------------------------------
# Queue consumer worker
# ---------------------------------------------------------------------------

async def llm_worker(
    queue: "MessageQueue",  # noqa: F821
    db_path: str = "signals.db",
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Drain queue → Groq (text batch + rate-limited vision calls) → SQLite."""
    from processing.database import bulk_insert_signals

    logger.info("LLM worker started (Groq backend).")

    while True:
        if stop_event is not None and stop_event.is_set() and queue.empty():
            logger.info("LLM worker: stop signal and queue empty. Exiting.")
            break

        text_batch, image_items = await queue.get_batch()

        if not text_batch and not image_items:
            if stop_event is not None and stop_event.is_set():
                logger.info("LLM worker: queue empty after timeout, stopping.")
                break
            continue

        all_signals: list[dict[str, Any]] = []

        if text_batch:
            all_signals.extend(await extract_text_signals(text_batch))

        if image_items:
            # asyncio.gather respects the semaphore, so concurrency is capped
            image_results = await asyncio.gather(
                *[extract_image_signal(item) for item in image_items],
                return_exceptions=False,
            )
            all_signals.extend(image_results)

        if all_signals:
            inserted, skipped = bulk_insert_signals(all_signals, db_path=db_path)
            logger.info(
                "Batch done: {} signals | {} inserted | {} skipped.",
                len(all_signals), inserted, skipped,
            )

    logger.info("LLM worker stopped.")
