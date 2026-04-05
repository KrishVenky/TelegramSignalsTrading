# CLAUDE.md — Full Project Context for Telegram Signal Trading System

> This file is the complete briefing for any Claude instance picking up this codebase.
> Read every section before touching any code.

---

## Project Overview

This is a **production-ready Telegram channel intelligence and algo trading system** for Indian
equity markets (NSE/BSE via Zerodha Kite). It has two major stages:

1. **Stage 1 (COMPLETE)** — Monitor public Telegram trading channels, extract structured signals
   from both text messages and chart images using a local/cloud LLM, store in SQLite.

2. **Stage 2 (IN PROGRESS)** — Feed those signals into a Zerodha KiteConnect algo that
   front-runs the pump caused by channel followers, targeting a 2% intraday scalp using MIS orders.

---

## Repository Structure

```
TelegramSignalsTrading/         ← git root
└── trading_intel/              ← all code lives here (this is the working directory)
    ├── .env                    # secrets — NEVER committed
    ├── .env.example            # template
    ├── .gitignore
    ├── README.md               # public-facing, no channel names
    ├── CLAUDE.md               # this file
    ├── requirements.txt
    ├── config.py               # THE master config — all tunable knobs here
    ├── main.py                 # entry point
    ├── utils.py                # shared helpers (timestamps, text clean, promo filter)
    ├── signals.db              # SQLite database (gitignored)
    ├── telegram.session        # Telethon session (gitignored, copy between machines)
    ├── trading_intel.log       # rotating log (gitignored)
    ├── telegram/               # Telethon layer
    │   ├── __init__.py
    │   ├── client.py           # MTProto auth, session management
    │   ├── batch_fetcher.py    # historical 7-day fetch
    │   └── realtime_listener.py # live NewMessage event handler
    └── processing/             # LLM + storage layer
        ├── __init__.py
        ├── database.py         # SQLite schema, insert, dedup, query
        ├── llm_processor.py    # dual-backend LLM (Ollama + Groq), vision + text
        ├── media_processor.py  # in-memory image download from Telegram
        └── message_queue.py    # asyncio.Queue bridging Telegram events → LLM worker
```

---

## Environment Variables (.env)

```
TELEGRAM_API_ID=        # from my.telegram.org → API Development Tools
TELEGRAM_API_HASH=      # same
TELEGRAM_PHONE=         # +91XXXXXXXXXX format
GROQ_API_KEY=           # only needed if LLM_BACKEND = "groq"
                        # KITE_API_KEY and KITE_API_SECRET will go here for Stage 2
```

---

## Tech Stack

| Component | Library | Notes |
|---|---|---|
| Telegram client | `telethon` | MTProto, async-native, reads channels as user |
| Local LLM | `ollama` | Meta LLaMA models, runs on 4060 Ti 16 GB |
| Cloud LLM | `groq` | Fallback, free tier has rate limits |
| Database | `sqlite3` (stdlib) | WAL mode, no server needed |
| Logging | `loguru` | Console + rotating file |
| Async runtime | `asyncio` | Everything is async |
| Env management | `python-dotenv` | |

**NOT installed, coming in Stage 2:** `kiteconnect`

---

## config.py — The Only File You Need to Touch for Setup

```python
# Which channels to monitor (keep private — do not add to README)
CHANNELS: list[str] = ["@channel1", "@channel2", "@channel3"]

# ← FLIP THIS to switch backends
LLM_BACKEND: str = "ollama"   # "ollama" = local GPU | "groq" = cloud

# Ollama settings
OLLAMA_HOST: str = "http://localhost:11434"   # or LAN IP for remote GPU
OLLAMA_TEXT_MODEL: str = "llama3.1:8b"
OLLAMA_VISION_MODEL: str = "llama3.2-vision:11b"   # needs 16 GB VRAM

# Groq settings (fallback)
GROQ_TEXT_MODEL: str = "llama-3.3-70b-versatile"
GROQ_VISION_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
GROQ_MAX_CONCURRENT: int = 3   # semaphore to avoid 429s

# Fetch window
BATCH_DAYS_BACK: int = 7
BATCH_LIMIT: int = 500
```

**Switching backends is ONE line.** All other code adapts automatically.

---

## How the Signal Pipeline Works

```
Telegram channels
      │
      ├── telegram/batch_fetcher.py     (historical, 7-day window)
      └── telegram/realtime_listener.py (live NewMessage events)
                │
         [Promo pre-filter in utils.py]
         is_promo_message(text) → skip if True
         Catches: t.me/ links, "join our channel", "free course", <15 chars
                │
                ▼
         processing/message_queue.py
         asyncio.Queue — unified schema:
         {
           "type": "text" | "image",
           "message_id": "ChannelName:12345",
           "channel": "@channel",
           "timestamp": "2026-03-27T10:00:00Z",
           "text": str | None,
           "media_bytes": bytes | None,   ← in-memory only, never to disk
           "mime_type": str | None
         }
                │
         ┌──────┴───────┐
    text_batch      image_items
         │                │
    LLM text call    LLM vision call (one per image)
    (up to 30/batch) (base64 encoded inline)
    llama3.1:8b      llama3.2-vision:11b
         │                │
         └──────┬──────────┘
                ▼
         processing/database.py → signals.db
         INSERT OR IGNORE (dedup on message_id)
         Skips if ticker IS NULL AND action IS NULL (promo slop)
```

---

## signals.db Schema

```sql
CREATE TABLE signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT    UNIQUE NOT NULL,   -- "ChannelName:12345"
    channel      TEXT,
    timestamp    TEXT,                       -- ISO-8601 UTC
    ticker       TEXT,                       -- "RELIANCE", "GPIL", etc.
    action       TEXT,                       -- "BUY", "SELL", "HOLD", "WATCH"
    entry_price  REAL,
    target_price REAL,
    stop_loss    REAL,
    sentiment    TEXT,                       -- "BULLISH", "BEARISH", "NEUTRAL"
    confidence   TEXT,                       -- "HIGH", "MEDIUM", "LOW"
    timeframe    TEXT,                       -- "intraday", "swing", "1W", etc.
    summary      TEXT,
    raw_message  TEXT,
    message_type TEXT,                       -- "text" or "image"
    processed_at TEXT
);
```

**Signals are only written if** `ticker IS NOT NULL OR action IS NOT NULL`.
The `_is_actionable()` guard in `database.py` silently drops pure fluff.

---

## Known Bugs Fixed (do not re-introduce)

### 1. LLaMA returns lists instead of strings
LLaMA sometimes returns `ticker: ["AAPL", "RELIANCE"]` instead of `"AAPL"`.
**Fix in `database.py/_coerce_signal()`**: all `_TEXT_FIELDS` are checked — if value is a list, it's joined with `", "`.

### 2. Bulk insert killing entire batch on one bad record
Old code used a single transaction for the whole batch. One binding error rolled back everything.
**Fix**: each signal is now its own `with get_connection()` transaction in `bulk_insert_signals()`.

### 3. Groq 429 retry timing
Old code always waited `base_delay * 2^attempt`. Groq error messages include "Please try again in Xs".
**Fix in `llm_processor._parse_retry_delay()`**: regex parses Groq's suggested delay, uses `max(suggested + 0.5, fallback)`.

### 4. Unicode on Windows terminal
The `→` character crashes PowerShell's default encoding.
**Fix**: use `-X utf8` flag or write to file with `sys.stdout.reconfigure(encoding="utf-8")`.

---

## Running the System

```powershell
cd trading_intel

# First time only
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Pull Ollama models (on the desktop PC with 4060 Ti 16 GB)
ollama pull llama3.1:8b
ollama pull llama3.2-vision:11b

# Run
python main.py                      # batch (7d history) → then realtime
python main.py --mode batch         # historical only
python main.py --mode realtime      # live only
python main.py --mode both          # explicit, same as default
```

**First run**: Telegram sends OTP to phone. Enter in terminal. Session saved to `telegram.session`.
**Subsequent runs**: No auth needed — session file reused.

**Copying between laptop and desktop:**
Copy `telegram.session` manually (it's gitignored). The session works on any machine with the same API credentials.

---

## LLM Backend Details

### Ollama (primary — desktop PC with 4060 Ti 16 GB)
- Text model: `llama3.1:8b` — ~5 GB VRAM Q4
- Vision model: `llama3.2-vision:11b` — ~8 GB VRAM Q4
- Both fit on 16 GB. Ollama swaps them as needed.
- Images processed **sequentially** (not concurrent) to avoid GPU OOM.
- No rate limits. Runs 24/7. Zero cost.

### Groq (fallback — laptop or when PC is off)
- Text: `llama-3.3-70b-versatile` (30 RPM, 6000 RPD free)
- Vision: `meta-llama/llama-4-scout-17b-16e-instruct` (30 RPM, 3360 img/day free)
- Vision quota burns fast on image-heavy channels. ~100 images exhausts ~3% of daily limit.
- Semaphore `GROQ_MAX_CONCURRENT = 3` limits concurrency.
- Do NOT use Qwen models — user preference (privacy concern).

### Local LAN setup (laptop → desktop GPU)
Change in config.py:
```python
OLLAMA_HOST: str = "http://192.168.1.XX:11434"  # desktop's local IP
```
Find desktop IP: `ipconfig` → IPv4 under WiFi/Ethernet adapter.

---

## Promo Filter (utils.py)

`is_promo_message(text) -> bool` — runs BEFORE any LLM call.

Catches:
- `t.me/` or `telegram.me/` links
- "join our channel", "subscribe now", "free course", "paid group"
- "enroll now", "limited offer", "referral link", "DM me for signals"
- Messages shorter than 15 characters

Applied in both `telegram/batch_fetcher.py` and `telegram/realtime_listener.py` for text messages.
Images are NOT pre-filtered (chart images are the most valuable).

---

## Stage 2 — What Needs to Be Built Next

### The Trading Strategy

**Setup**: Indian equity channels post trading calls to tens of thousands of followers.
When they post "BUY STOCK X 🔥", followers rush in and the price rises 5-15%.
**We enter immediately on signal** (automated, seconds after post) and exit at +2-3%,
before the channel admin dumps. This is legal (public information).

**Order type**: MIS (Margin Intraday Square-off) on Zerodha.
- Must close by 3:15 PM (auto-squared at 3:20 PM by Zerodha).
- T+1 settlement is irrelevant — this is same-day cash settled.
- No overnight risk.

**Costs per round trip on ₹10,000 MIS trade**:
- Brokerage: ₹40 (₹20 each side, Zerodha flat)
- STT: ~₹2.50 (0.025% sell side only)
- Exchange/GST: ~₹5
- Net on 2% gain: ₹200 - ₹47 = **₹153 (~1.5% effective)**

**Position sizing (start small)**:
```python
CAPITAL_PER_TRADE = 10_000   # ₹10k per signal
MAX_OPEN_POSITIONS = 3        # max ₹30k at risk
MAX_DAILY_LOSS = 1_500        # kill switch — stop trading for the day
```

**Exit rules**:
- Take profit: +3% (configurable)
- Stop loss: -1.5% (hard cut, no exceptions)
- Time exit: force close at 2:45 PM regardless of P&L

### Signal Type Classification (NOT YET BUILT)

Before the Kite module, add `signal_type` to the pipeline:

**Add to DB schema**:
```sql
ALTER TABLE signals ADD COLUMN signal_type TEXT;
-- Values: DIRECT_CALL | BROKER_CALL | CHART_SETUP | RECAP | GENERAL
```

**Add to LLM prompt** (in config.py `LLM_USER_PROMPT_TEMPLATE`):
Add `signal_type` to the fields list with the rule:
- `DIRECT_CALL` — urgent buy/sell with explicit ticker + 🔥🚀 emojis or "NOW"/"URGENT"
- `BROKER_CALL` — analyst/brokerage recommendation (Jefferies, Goldman, etc.)
- `CHART_SETUP` — technical analysis setup, BO zone, pattern without urgency
- `RECAP` — past performance ("up 10% from our call")
- `GENERAL` — market commentary, no specific call

**Pre-classifier regex in utils.py** (fast path, before LLM):
```python
_DIRECT_CALL_RE = re.compile(
    r"(🔥|🚀|💥|‼️|buy now|BUY NOW|entry now|add now|accumulate now)", re.I
)
_BROKER_RE = re.compile(
    r"(jefferies|goldman|morgan|jp morgan|citi|nomura|analyst|price target|PT ₹)", re.I
)
_RECAP_RE = re.compile(
    r"(gave \d+%|up \d+%|from our call|booked profit|target achieved)", re.I
)
```

**Only `DIRECT_CALL` signals get traded.** Others stored for analysis only.

### Kite Module (NOT YET BUILT)

Files to create under `kite/`:
```
kite/
├── __init__.py
├── client.py           # KiteConnect auth, daily access token refresh
├── order_manager.py    # place/cancel MIS orders, track fills
├── position_tracker.py # WebSocket price monitoring, TP/SL/time exit
├── paper_trader.py     # simulates fills, logs to trades.db (use FIRST)
└── decision_engine.py  # reads signals.db, filters, calls order_manager
```

**Decision engine entry logic**:
```python
def should_enter(signal: dict) -> bool:
    if signal["action"] not in ("BUY", "Buy", "buy"):       return False
    if signal["signal_type"] != "DIRECT_CALL":              return False
    if signal["confidence"] == "LOW":                       return False
    if signal["ticker"] is None:                            return False

    now = datetime.now(IST)
    if not (time(9, 20) <= now.time() <= time(14, 45)):     return False
    if signal["ticker"] in open_positions:                  return False
    if daily_pnl <= -MAX_DAILY_LOSS:                        return False

    quote = kite.quote(f"NSE:{signal['ticker']}")
    if quote["volume"] < 50_000:                            return False  # too illiquid

    return True
```

**KiteConnect setup**:
```python
pip install kiteconnect

# Daily token refresh (Zerodha requires login every day)
kite = KiteConnect(api_key="KITE_API_KEY")
print(kite.login_url())   # open in browser, grab request_token from redirect URL
data = kite.generate_session(request_token, api_secret="KITE_SECRET")
kite.set_access_token(data["access_token"])
```

Add to `.env`:
```
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
```

Kite API: ₹2000/month subscription (or free 60-day trial on new account).

### Phase Sequence (do NOT skip phases)

1. **Add `signal_type` field** to DB + LLM prompts + utils.py regex classifier
2. **Build `kite/paper_trader.py`** — logs simulated trades to `trades.db`, no real orders
3. **Run paper trader for 2-3 weeks** alongside the live signal collector
4. **Analyse alpha**: query `signals.db` joined with historical prices (Kite historical API, free).
   - What % of DIRECT_CALL signals moved >3% within 4 hours of signal timestamp?
   - If >40% — there's edge. If <30% — channels have no alpha, rethink.
5. **Only then go live** with real orders via `kite/order_manager.py`

---

## What the Channels Post (Signal Quality Notes)

- **Bulk portfolio recap images**: One image listing 10-15 stocks with buy/sell ratings.
  LLaMA correctly extracts them as comma-joined lists. These hit the `_coerce_signal()` list fix.
  They are stored but typically not `DIRECT_CALL` — lower priority.

- **Individual chart images**: Candlestick charts with annotations (BO zone, entry, target).
  LLaMA 3.2 Vision 11B reads these well — extracts entry price from price axis, patterns from labels.
  These are the most valuable signals.

- **Broker recommendation images**: Text overlaid on neutral background ("Jefferies: BUY BHARAT FORGE TP 2150").
  Good for context, low urgency, classify as `BROKER_CALL`.

- **Text blasts during market hours**: Short urgent text — these are the pump triggers.
  Fastest to process (no vision call needed), highest urgency.

---

## Important Rules / Gotchas

1. **Never add channel names to README.md** — the repo is public. Channel names stay in `config.py` only.

2. **Never use Qwen models** — user explicitly rejected (privacy concern). Stick to Meta LLaMA family only.

3. **LLaVA is outdated** — Do not recommend or add `llava:*` models. Use `llama3.2-vision:11b` for vision (Meta's native multimodal, trained end-to-end, not a bolt-on adapter).

4. **Ollama processes images sequentially** — `asyncio.gather` is intentionally NOT used for Ollama vision calls. GPU can't parallelise multiple large vision inferences. Groq uses gather (semaphore-capped).

5. **T+1 is not an issue for this strategy** — MIS intraday orders settle same-day cash. T+1 only affects CNC delivery holds. Do not confuse these.

6. **Paper trade before real money** — non-negotiable. Indian small caps are heavily manipulated. The channels may themselves be pump operators. The strategy exploits that but needs confirmation of alpha first.

7. **Kite access token expires daily** — need a daily cron/script to refresh it. Store in `.env` or a local file, never in git.

8. **`telegram.session` must be copied manually** between machines — it's gitignored. The session authenticates the user so no OTP is needed on subsequent runs.

---

## Querying signals.db

```python
import sqlite3
conn = sqlite3.connect("signals.db")
conn.row_factory = sqlite3.Row

# All actionable signals
rows = conn.execute("""
    SELECT * FROM signals
    WHERE ticker IS NOT NULL OR action IS NOT NULL
    ORDER BY timestamp DESC LIMIT 50
""").fetchall()

# By ticker
rows = conn.execute(
    "SELECT * FROM signals WHERE ticker = ? ORDER BY timestamp DESC",
    ("RELIANCE",)
).fetchall()
```

Or use **DB Browser for SQLite** (free GUI) — open `signals.db` directly.

---

## Current State (as of April 2026)

- [x] Full signal pipeline working end-to-end
- [x] Ollama dual-backend (Ollama + Groq, one-line switch)
- [x] Promo filter (3 layers: regex pre-filter, LLM prompt instruction, DB actionable guard)
- [x] Image/chart extraction via Llama 3.2 Vision 11B
- [x] 76+ signals in DB, all from one channel (others were inactive during test window)
- [x] Bugs fixed: list-type coercion, per-signal DB transactions, Groq retry delay parsing
- [x] Pushed to GitHub: https://github.com/KrishVenky/TelegramSignalsTrading
- [ ] `signal_type` field (DIRECT_CALL / BROKER_CALL / CHART_SETUP / RECAP)
- [ ] `kite/` module — paper trader
- [ ] Decision engine
- [ ] Live trading (after paper validation)
