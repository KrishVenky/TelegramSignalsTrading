# Telegram Channel Intelligence System

A production-ready Python pipeline that monitors Indian finance/trading Telegram channels in
real-time and batch modes, extracts structured signals from both **text and chart images** using
a local or cloud LLM (Ollama / Groq), and stores results in a local SQLite database.

---

## Tech Stack

- **[Telethon](https://github.com/LonamiWebs/Telethon)** — MTProto client (reads channels as your user account)
- **[Ollama](https://ollama.com)** — local LLM inference (Meta Llama 3.2 Vision for charts, Llama 3.1 8B for text)
- **[Groq](https://groq.com)** — cloud LLM fallback (hot-swappable via config)
- **SQLite** — local signal storage (no server needed)
- **loguru** — structured logging
- **asyncio** — fully async runtime

---

## Project Structure

```
trading_intel/
├── .env                        # Your credentials (never committed)
├── .env.example                # Credential template
├── requirements.txt
├── config.py                   # Channels, LLM backend, model settings, prompts
├── main.py                     # Entry point (CLI)
├── utils.py                    # Shared helpers
├── telegram/
│   ├── client.py               # Telethon auth & session management
│   ├── batch_fetcher.py        # Historical message fetch (7-day default)
│   └── realtime_listener.py    # Live NewMessage event handler
└── processing/
    ├── database.py             # SQLite schema, insert, dedup, query
    ├── llm_processor.py        # Dual-backend LLM (Ollama + Groq)
    ├── media_processor.py      # In-memory image download & classification
    └── message_queue.py        # Async queue (text & image routing)
```

---

## Setup

### 1. Clone & create virtual environment

```bash
git clone <your-repo-url>
cd trading_intel

python -m venv .venv

# Windows
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
copy .env.example .env
```

Edit `.env`:

```
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+91xxxxxxxxxx
GROQ_API_KEY=your_groq_api_key   # only needed if LLM_BACKEND = "groq"
```

> Get Telegram credentials at [my.telegram.org](https://my.telegram.org) → API Development Tools.

### 4. Configure channels

Edit `config.py` → `CHANNELS` list:

```python
CHANNELS: list[str] = [
    "@your_channel_1",
    "@your_channel_2",
]
```

### 5. Choose LLM backend

In `config.py`:

```python
LLM_BACKEND: str = "ollama"   # local GPU — no rate limits
# LLM_BACKEND: str = "groq"   # cloud fallback
```

**For Ollama (local):** Install from [ollama.com](https://ollama.com), then:
```bash
ollama pull llama3.1:8b               # text model (~5 GB)
ollama pull llama3.2-vision:11b       # vision/chart model (~8 GB, needs 16 GB VRAM)
```

**For Groq (cloud):** Set `LLM_BACKEND = "groq"` and add `GROQ_API_KEY` to `.env`.

### 6. Run

```bash
python main.py                   # batch (7 days history) → then live monitor
python main.py --mode batch      # historical only
python main.py --mode realtime   # live only
```

**First run:** Telegram will send an OTP to your phone. Enter it in the terminal.
The session is saved to `telegram.session` — subsequent runs need no re-auth.

---

## How It Works

```
Telegram channels
       │
       ├── batch_fetcher.py    (7-day history, chronological)
       └── realtime_listener.py  (live NewMessage events)
                 │
         [Promo filter]  ← skips ads/invites/course promotions
                 │
                 ▼
         message_queue.py  (asyncio.Queue)
                 │
         ┌───────┴────────┐
    text_batch        image_items
         │                │
  LLM text call    LLM vision call
  (batch, JSON)   (1 per image, base64)
         │                │
         └───────┬────────┘
                 ▼
           database.py  →  signals.db
```

### Extracted Fields (per signal)

| Field | Description |
|---|---|
| `ticker` | Stock/crypto symbol (e.g. `RELIANCE`, `NIFTY`) |
| `action` | `BUY` / `SELL` / `HOLD` / `WATCH` |
| `entry_price` | Entry price level |
| `target_price` | Take-profit target |
| `stop_loss` | Stop-loss level |
| `sentiment` | `BULLISH` / `BEARISH` / `NEUTRAL` |
| `confidence` | `HIGH` / `MEDIUM` / `LOW` |
| `timeframe` | `intraday` / `swing` / `long-term` |
| `summary` | One-sentence plain-English signal summary |
| `message_type` | `text` or `image` |

---

## Notes

- Media is **never written to disk** — image bytes live in memory only.
- SQLite uses **WAL mode** for safe concurrent reads/writes.
- LLM calls retry up to 3× with exponential backoff on failures.
- Duplicate messages are silently skipped via `INSERT OR IGNORE` on `message_id`.
- Promotional messages (course ads, channel invites, referral links) are filtered before the LLM.
