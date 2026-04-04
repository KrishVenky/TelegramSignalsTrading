# Telegram Channel Intelligence System

A production-ready Python pipeline that monitors Indian finance/trading Telegram channels in real-time and batch modes, extracts structured signals from both **text and chart images** using Gemini 2.0 Flash (multimodal), and stores results in a local SQLite database.

## Monitored Channels

| Channel | Focus |
|---|---|
| [@FinanceWithSunil](https://t.me/FinanceWithSunil) | SEBI-registered analyst — price action & breakout trading |
| [@ChartWallah00](https://t.me/ChartWallah00) | Micro-caps & multibagger research |
| [@johntradingwick](https://t.me/johntradingwick) | Market insights, forecasting, TradingView analysis |

---

## Tech Stack

- **[Telethon](https://github.com/LonamiWebs/Telethon)** — MTProto client (reads channels as your user account)
- **[Gemini 2.0 Flash](https://ai.google.dev/)** — multimodal LLM for signal extraction
- **SQLite** — local signal storage (no server needed)
- **loguru** — structured logging
- **asyncio** — fully async runtime

---

## Project Structure

```
TelegramBot/
├── .gitignore
├── README.md
└── trading_intel/
    ├── .env                  # Your credentials (never committed)
    ├── .env.example          # Credential template
    ├── requirements.txt
    ├── config.py             # Channels, LLM settings, prompts
    ├── main.py               # Entry point (CLI)
    ├── telegram_client.py    # Telethon auth & session management
    ├── batch_fetcher.py      # Historical message fetch
    ├── realtime_listener.py  # Live message handler
    ├── media_processor.py    # In-memory image download & classification
    ├── llm_processor.py      # Gemini text + multimodal image extraction
    ├── message_queue.py      # Async queue (text & image routing)
    ├── database.py           # SQLite schema, insert, dedup, query
    └── utils.py              # Timestamp helpers, text/JSON cleaning
```

---

## Setup

### 1. Clone & create virtual environment

```bash
git clone <your-repo-url>
cd TelegramBot/trading_intel

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # macOS/Linux
```

Edit `.env`:

```
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_PHONE=+91xxxxxxxxxx
GEMINI_API_KEY=your_gemini_api_key
```

> Get Telegram credentials at [my.telegram.org](https://my.telegram.org) → API Development Tools.  
> Get Gemini API key at [aistudio.google.com](https://aistudio.google.com).

### 4. Run

```bash
python main.py              # batch (7 days history) → then live monitor
python main.py --mode batch     # historical only
python main.py --mode realtime  # live only
```

**First run:** Telegram will send an OTP to your phone. Enter it in the terminal. The session is saved to `telegram.session` — subsequent runs need no re-auth.

---

## How It Works

```
Telegram channels
       │
       ├── batch_fetcher.py  (7-day history, chronological)
       └── realtime_listener.py  (live NewMessage events)
                 │
                 ▼
         message_queue.py  (asyncio.Queue)
                 │
         ┌───────┴────────┐
    text_batch        image_items
         │                │
  Gemini text API   Gemini multimodal
  (batch, JSON arr) (1 per image, base64)
         │                │
         └───────┬────────┘
                 ▼
           database.py  →  signals.db
```

### Extracted Fields (per signal)

| Field | Description |
|---|---|
| `ticker` | Stock/crypto symbol (e.g. `RELIANCE`, `NIFTY`, `BTC`) |
| `action` | `BUY` / `SELL` / `HOLD` / `WATCH` |
| `entry_price` | Entry price |
| `target_price` | Take-profit target |
| `stop_loss` | Stop-loss level |
| `sentiment` | `BULLISH` / `BEARISH` / `NEUTRAL` |
| `confidence` | `HIGH` / `MEDIUM` / `LOW` |
| `timeframe` | `intraday` / `swing` / `long-term` |
| `summary` | One-sentence plain-English signal summary |
| `message_type` | `text` or `image` |

---

## Querying Results

```python
# Quick query from Python
from database import query_signals
for row in query_signals(ticker="RELIANCE", limit=10):
    print(dict(row))
```

Or open `signals.db` with [DB Browser for SQLite](https://sqlitebrowser.org/) (free GUI).

---

## Notes

- Media is **never written to disk** — image bytes live in memory only and are discarded after Gemini processes them.
- SQLite uses **WAL mode** for safe concurrent reads/writes during live monitoring.
- Gemini calls retry up to 3× with exponential backoff on failures.
- Telethon **FloodWait** errors are respected automatically.
- Duplicate messages are silently skipped via `INSERT OR IGNORE` on `message_id`.
