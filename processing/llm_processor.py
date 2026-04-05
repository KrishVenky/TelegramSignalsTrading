"""
processing/llm_processor.py — Dual-backend LLM signal extraction.

Set LLM_BACKEND in config.py:
  "ollama"  → local Ollama server (no rate limits, uses your GPU)
  "groq"    → Groq cloud API (fast, but subject to free-tier limits)

Text:  batched → text model
Image: individual vision calls → vision model
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any, Optional

from dotenv import load_dotenv
from loguru import logger

from config import (
    GROQ_MAX_CONCURRENT,
    GROQ_TEXT_MODEL,
    GROQ_VISION_MODEL,
    LLM_BACKEND,
    LLM_MAX_RETRIES,
    LLM_RETRY_BASE_DELAY,
    LLM_SYSTEM_PROMPT,
    LLM_USER_PROMPT_TEMPLATE,
    OLLAMA_HOST,
    OLLAMA_TEXT_MODEL,
    OLLAMA_VISION_MODEL,
)
from utils import classify_signal_type, extract_json_from_response, now_iso8601, strip_json_fences

load_dotenv()

# ---------------------------------------------------------------------------
# Backend initialisation
# ---------------------------------------------------------------------------

if LLM_BACKEND == "groq":
    from groq import AsyncGroq
    _groq: AsyncGroq = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
    _sem: asyncio.Semaphore = asyncio.Semaphore(GROQ_MAX_CONCURRENT)
    logger.info("LLM backend: Groq (text={}, vision={})", GROQ_TEXT_MODEL, GROQ_VISION_MODEL)

elif LLM_BACKEND == "ollama":
    from ollama import AsyncClient as _OllamaClient
    _ollama: _OllamaClient = _OllamaClient(host=OLLAMA_HOST)
    logger.info("LLM backend: Ollama @ {} (text={}, vision={})",
                OLLAMA_HOST, OLLAMA_TEXT_MODEL, OLLAMA_VISION_MODEL)

else:
    raise ValueError(f"Unknown LLM_BACKEND: {LLM_BACKEND!r}. Must be 'groq' or 'ollama'.")


# ---------------------------------------------------------------------------
# Image extraction prompt
# ---------------------------------------------------------------------------

_IMAGE_EXTRACTION_PROMPT = (
    "You are a financial signal extraction engine analyzing a Telegram channel image. "
    "This image is from an Indian finance/trading signal channel. "
    "Extract any trading intelligence visible in the image.\n\n"
    "Look for: stock/crypto tickers (NSE/BSE symbols), buy/sell calls, "
    "price targets, stop loss levels, chart patterns (cup & handle, "
    "breakout, support/resistance), candlestick patterns, annotations.\n\n"
    "Return ONLY a raw JSON object with these exact fields:\n"
    "message_id, channel, timestamp, ticker, action, entry_price, target_price, "
    "stop_loss, sentiment, confidence, timeframe, summary, raw_message\n\n"
    "For raw_message: describe what you see in the image in plain text.\n"
    "Never fabricate prices not visible in the image.\n"
    "For fields you cannot determine: use null.\n"
    "No markdown. No explanation. JSON only."
)


# ---------------------------------------------------------------------------
# Groq backend
# ---------------------------------------------------------------------------

def _parse_retry_delay(exc: Exception, fallback: float) -> float:
    msg = str(exc)
    m = re.search(r"Please try again in (\d+(?:\.\d+)?)(ms|s)", msg)
    if m:
        value, unit = float(m.group(1)), m.group(2)
        return (value / 1000) if unit == "ms" else value
    return fallback


async def _groq_chat(messages: list[dict], model: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, LLM_MAX_RETRIES + 1):
        try:
            async with _sem:
                response = await _groq.chat.completions.create(
                    model=model, messages=messages,
                    temperature=0.1, max_tokens=4096,
                )
            return response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            fallback = LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            delay = max(_parse_retry_delay(exc, fallback) + 0.5, fallback)
            logger.warning("Groq attempt {}/{} failed ({}): {}. Retrying in {:.1f}s…",
                           attempt, LLM_MAX_RETRIES, model, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError(f"Groq failed after {LLM_MAX_RETRIES} attempts") from last_exc


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

async def _ollama_chat(messages: list[dict], model: str) -> str:
    """Call local Ollama server. No rate limits — runs on your GPU."""
    try:
        response = await _ollama.chat(
            model=model,
            messages=messages,
            options={"temperature": 0.1},
        )
        return response.message.content or ""
    except Exception as exc:
        raise RuntimeError(f"Ollama call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Unified dispatch
# ---------------------------------------------------------------------------

async def _llm_chat(messages: list[dict], model: str) -> str:
    if LLM_BACKEND == "groq":
        return await _groq_chat(messages, model)
    return await _ollama_chat(messages, model)


def _text_model() -> str:
    return GROQ_TEXT_MODEL if LLM_BACKEND == "groq" else OLLAMA_TEXT_MODEL


def _vision_model() -> str:
    return GROQ_VISION_MODEL if LLM_BACKEND == "groq" else OLLAMA_VISION_MODEL


def _build_vision_messages(prompt: str, b64_data: str, mime_type: str) -> list[dict]:
    """Build the message list for a vision call — format differs per backend."""
    if LLM_BACKEND == "groq":
        image_url = f"data:{mime_type};base64,{b64_data}"
        return [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        }]
    else:  # ollama
        return [{
            "role": "user",
            "content": prompt,
            "images": [b64_data],   # Ollama accepts base64 strings
        }]


# ---------------------------------------------------------------------------
# JSON parsing helper
# ---------------------------------------------------------------------------

def _parse_json(raw: str, expect_array: bool = True) -> Any:
    cleaned = extract_json_from_response(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Last-ditch: try replacing all remaining invalid control chars
        try:
            sanitised = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", cleaned)
            parsed = json.loads(sanitised)
        except json.JSONDecodeError:
            logger.error("JSON parse failed (array={}): {}. Raw: {!r}",
                         expect_array, exc, cleaned[:300])
            return None
    # If we expected an array but got a single dict, wrap it
    if expect_array and isinstance(parsed, dict):
        return [parsed]
    return parsed


# ---------------------------------------------------------------------------
# Text batch extraction
# ---------------------------------------------------------------------------

async def extract_text_signals(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch text messages → single LLM call → list of signal dicts."""
    if not messages:
        return []

    safe = [{k: v for k, v in m.items() if k != "media_bytes"} for m in messages]
    user_content = LLM_USER_PROMPT_TEMPLATE.format(
        messages_json=json.dumps(safe, ensure_ascii=False, indent=2)
    )

    llm_messages = [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        raw = await _llm_chat(llm_messages, model=_text_model())
    except Exception as exc:
        logger.error("Text batch LLM call failed: {}", exc)
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
        # Regex override: if LLM left signal_type vague, sharpen with regex
        raw_text = item.get("raw_message") or ""
        llm_type = (item.get("signal_type") or "").upper()
        if llm_type not in ("DIRECT_CALL", "BROKER_CALL", "CHART_SETUP", "RECAP"):
            item["signal_type"] = classify_signal_type(raw_text)
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
    """Process one image via vision model. Never raises — falls back to minimal record."""
    media_bytes: Optional[bytes] = item.get("media_bytes")
    mime_type: str = item.get("mime_type") or "image/jpeg"

    if not media_bytes:
        logger.warning("No bytes for image message_id={}", item["message_id"])
        return _minimal_image_record(item)

    b64_data = base64.b64encode(media_bytes).decode("utf-8")
    caption_note = f"\n\nCaption from the post: {item['text']}" if item.get("text") else ""
    prompt_text = (
        _IMAGE_EXTRACTION_PROMPT
        + caption_note
        + f"\n\nmessage_id: {item['message_id']}"
        + f"\nchannel: {item['channel']}"
        + f"\ntimestamp: {item['timestamp']}"
    )

    vision_messages = _build_vision_messages(prompt_text, b64_data, mime_type)

    try:
        raw = await _llm_chat(vision_messages, model=_vision_model())
    except Exception as exc:
        logger.error("Vision call failed for message_id={}: {}", item["message_id"], exc)
        return _minimal_image_record(item)

    parsed = _parse_json(raw, expect_array=False)
    if not isinstance(parsed, dict):
        logger.warning("Non-dict vision response for message_id={}.", item["message_id"])
        return _minimal_image_record(item)

    if "message_id" in parsed:
        parsed["message_id"] = str(parsed["message_id"])
    parsed.setdefault("message_id", item["message_id"])
    parsed.setdefault("channel", item["channel"])
    parsed.setdefault("timestamp", item["timestamp"])
    parsed["message_type"] = "image"
    # Regex override on image: use caption text if available
    caption = item.get("text") or parsed.get("raw_message") or ""
    llm_type = (parsed.get("signal_type") or "").upper()
    if llm_type not in ("DIRECT_CALL", "BROKER_CALL", "CHART_SETUP", "RECAP"):
        parsed["signal_type"] = classify_signal_type(caption)

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
    """Drain queue → LLM (text batch + vision calls) → SQLite."""
    from processing.database import bulk_insert_signals

    logger.info("LLM worker started (backend={}).", LLM_BACKEND)

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
            # For Ollama: process sequentially to avoid overwhelming the GPU
            # For Groq: semaphore already caps concurrency
            if LLM_BACKEND == "ollama":
                for img_item in image_items:
                    result = await extract_image_signal(img_item)
                    all_signals.append(result)
            else:
                image_results = await asyncio.gather(
                    *[extract_image_signal(item) for item in image_items],
                    return_exceptions=False,
                )
                all_signals.extend(image_results)

        if all_signals:
            inserted, skipped = bulk_insert_signals(all_signals, db_path=db_path)
            logger.info("Batch done: {} signals | {} inserted | {} skipped.",
                        len(all_signals), inserted, skipped)

    logger.info("LLM worker stopped.")
