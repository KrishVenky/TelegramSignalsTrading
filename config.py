"""
config.py — Central configuration for the Telegram Intelligence System.
Edit CHANNELS to add the channels you want to monitor.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Telegram channels to monitor.
# Add usernames (e.g. "@wallstreetbets") or invite links.
# ---------------------------------------------------------------------------
CHANNELS: list[str] = [
    "@FinanceWithSunil",   # Finance With Sunil — SEBI RA, price action trading
    "@ChartWallah00",      # Chart Wallah — micro-caps & multibaggers
    "@johntradingwick",    # Trading Wick — market insights & forecasting
]

# ---------------------------------------------------------------------------
# Batch fetcher settings
# ---------------------------------------------------------------------------
# Maximum number of historical messages to fetch per channel.
BATCH_LIMIT: int = 500

# How many calendar days of history to retrieve.
BATCH_DAYS_BACK: int = 7

# ---------------------------------------------------------------------------
# LLM processing settings
# ---------------------------------------------------------------------------
# Number of messages sent to the LLM in a single API call.
LLM_BATCH_SIZE: int = 30

# ---------------------------------------------------------------------------
# Groq model names
# ---------------------------------------------------------------------------
# Text extraction — LLaMA 3.3 70B (30 RPM / 6 000 RPD on free tier)
GROQ_TEXT_MODEL: str = "llama-3.3-70b-versatile"

# Vision / image extraction — LLaMA 4 Scout (supports vision, generous free tier)
GROQ_VISION_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"

# Max concurrent Groq calls (keeps us well under the per-minute rate limit)
GROQ_MAX_CONCURRENT: int = 3


# ---------------------------------------------------------------------------
# Extraction field definitions (used to build the LLM prompt).
# These map exactly to the database columns.
# ---------------------------------------------------------------------------
EXTRACTION_FIELDS: list[str] = [
    "message_id",    # Telegram message ID (str)
    "channel",       # Channel username (str)
    "timestamp",     # ISO-8601 datetime string (str)
    "ticker",        # Stock/crypto symbol e.g. AAPL, BTC  (str | null)
    "action",        # BUY, SELL, HOLD, WATCH              (str | null)
    "entry_price",   # Entry price                          (float | null)
    "target_price",  # Target / take-profit price           (float | null)
    "stop_loss",     # Stop-loss price                      (float | null)
    "sentiment",     # BULLISH, BEARISH, NEUTRAL            (str)
    "confidence",    # HIGH, MEDIUM, LOW                    (str)
    "timeframe",     # e.g. "intraday", "swing", "long-term" (str | null)
    "summary",       # One-sentence plain-English summary   (str)
    "raw_message",   # Original unmodified message text     (str)
]

# ---------------------------------------------------------------------------
# LLM prompt templates
# ---------------------------------------------------------------------------
LLM_SYSTEM_PROMPT: str = (
    "You are a financial signal extraction engine for Indian stock/crypto markets. "
    "Extract structured trading intelligence from Telegram messages. "
    "Return ONLY a raw JSON array. No markdown. No explanation. "
    "IMPORTANT: If a message is promotional (course ads, channel invites, referral links, "
    "subscribe requests, discount codes, or general motivational content with no specific trade), "
    "still include it in the array but set ticker, action, entry_price, target_price, "
    "stop_loss, timeframe to null and sentiment to NEUTRAL. "
    "Never fabricate ticker symbols or prices that are not explicitly mentioned. "
    "If a field cannot be determined from the message text, use null."
)

LLM_USER_PROMPT_TEMPLATE: str = (
    "Extract trading signals from these Telegram messages: {messages_json}\n\n"
    "Return a JSON array where each object has these exact fields:\n"
    "message_id, channel, timestamp, ticker, action, entry_price, "
    "target_price, stop_loss, sentiment, confidence, timeframe, "
    "summary, raw_message\n\n"
    "Rules:\n"
    "- ticker: stock/crypto symbol only if EXPLICITLY mentioned (e.g. RELIANCE, NIFTY, BTC). null otherwise.\n"
    "- action: BUY/SELL/HOLD/WATCH only if a clear call is made. null otherwise.\n"
    "- Do NOT guess tickers from context. If not stated, use null.\n"
    "- Promotional messages: set all signal fields to null."
)

# ---------------------------------------------------------------------------
# Retry / resilience settings
# ---------------------------------------------------------------------------
LLM_MAX_RETRIES: int = 3
LLM_RETRY_BASE_DELAY: float = 2.0   # seconds; doubled each retry

# Maximum seconds the queue consumer waits before flushing a partial batch.
QUEUE_CONSUMER_TIMEOUT: float = 5.0
