# Telegram Channel Intelligence System

A production-ready Python pipeline that monitors Indian finance/trading Telegram channels,
extracts structured signals from **text and chart images** using a local or cloud LLM
(Ollama / Groq), stores them in SQLite, and exposes them via a local dashboard with
**Kite Connect** order execution.

---

## Tech Stack

| Layer | Library | Purpose |
|---|---|---|
| Telegram | `telethon` | MTProto client — reads channels as your user account |
| LLM (local) | `ollama` | Local inference — Llama 3.1 8B (text) + Llama 3.2 Vision 11B (charts) |
| LLM (cloud) | `groq` | Cloud fallback — hot-swappable via `config.py` |
| Database | `sqlite3` | WAL-mode local signal + trade storage |
| Dashboard | `fastapi` + `uvicorn` | Local web UI — signals, recommended buys, Kite auth |
| Broker | `kiteconnect` | Zerodha order placement (paper or live MIS) |
| Logging | `loguru` | Structured log to file + stderr |

---

## Project Structure

```
trading_intel/
├── .env                        # Credentials (never committed)
├── .env.example                # Credential template
├── config.py                   # All settings: channels, LLM, risk params
├── main.py                     # CLI entry point
├── utils.py                    # Shared helpers + promo filter
│
├── telegram/
│   ├── client.py               # Telethon auth & session management
│   ├── batch_fetcher.py        # Historical fetch (7-day default)
│   └── realtime_listener.py    # Live NewMessage handler
│
├── processing/
│   ├── database.py             # SQLite schema, insert, dedup, query
│   ├── llm_processor.py        # Dual-backend LLM worker (Ollama + Groq)
│   ├── media_processor.py      # In-memory image download & classification
│   └── message_queue.py        # Async queue (text vs image routing)
│
├── kite/
│   ├── client.py               # KiteConnect auth, token storage, LTP fetch
│   └── order_manager.py        # Paper/live MIS order placement + trades.db
│
└── dashboard/
    └── app.py                  # FastAPI dashboard (signals, Kite auth, confirm buy)
```

---

## Quick Start (Daily Morning Workflow)

```powershell
# From trading_intel/
.venv\Scripts\Activate.ps1

# 1. Fetch last 7 days of signals from all channels
python main.py --mode batch

# 2. Open the dashboard (in a separate terminal)
python -m uvicorn dashboard.app:app --port 8000

# 3. Open http://localhost:8000 in browser
# 4. Click "Kite" chip in header → log in to Zerodha → token saved automatically
# 5. Enable Trading toggle (OFF by default) before placing any orders
```

Or use the included startup script:
```powershell
.\start.ps1          # runs batch + dashboard automatically
```

---

## Full Setup (First Time)

### 1. Clone & virtual environment
```bash
git clone <your-repo-url>
cd trading_intel
python -m venv .venv
.venv\Scripts\activate    # Windows
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
GROQ_API_KEY=gsk_...            # only if LLM_BACKEND = "groq"
KITE_API_KEY=your_kite_api_key
KITE_API_SECRET=your_kite_secret
```

Get Telegram credentials at [my.telegram.org](https://my.telegram.org) → API Development Tools.  
Get Kite credentials at [kite.trade/apps](https://kite.trade/apps) → create a Personal app.

> **Kite redirect URL:** Set to `http://127.0.0.1:8000/kite/callback` in your Kite app settings.

### 4. Configure channels
Edit `config.py` → `CHANNELS`:
```python
CHANNELS = ["@your_channel_1", "@your_channel_2"]
```

### 5. Choose LLM backend
In `config.py`:
```python
LLM_BACKEND = "groq"    # laptop/no GPU: cloud, rate-limited
LLM_BACKEND = "ollama"  # desktop with GPU: local, unlimited
```

**For Ollama (local GPU, 4060 Ti 16 GB recommended):**
```bash
ollama pull llama3.1:8b           # text model (~5 GB)
ollama pull llama3.2-vision:11b   # vision/chart model (~8 GB VRAM Q4)
```

**For Groq:** just set the key in `.env` — no install needed.

### 6. Run the pipeline
```bash
python main.py --mode batch      # 7-day history fetch (run this every morning)
python main.py --mode realtime   # live monitor (runs until Ctrl+C)
python main.py                   # batch → then realtime (default)
```

---

## Risk & Trading Parameters

All editable in **`config.py`** — no code changes needed:

```python
# ─── Exit thresholds ───────────────────────────────────────────────
TRADE_TAKE_PROFIT_PCT = 0.02   # +2% → take profit   ← edit here
TRADE_STOP_LOSS_PCT   = 0.01   # −1% → stop loss     ← edit here

# ─── Capital ───────────────────────────────────────────────────────
CAPITAL_PER_TRADE  = 10_000    # ₹ per trade
MAX_OPEN_POSITIONS = 3         # max 3 concurrent positions → ₹30k max
MAX_DAILY_LOSS     = 1_500     # ₹ kill-switch for the day

# ─── Safety ────────────────────────────────────────────────────────
PAPER_TRADE = True             # ← flip to False for live orders (careful!)
```

**Strategy:** MIS (intraday) scalp. Target 2% gain on momentum before channel admins dump.
Stop loss at 1% protects capital. All positions auto-square at 3:15 PM by Zerodha.

---

## Dashboard

```bash
python -m uvicorn dashboard.app:app --port 8000 --reload
```

Open **http://localhost:8000**

| Section | What you see |
|---|---|
| Stats bar | Total signals, Buy/Sell counts, High confidence, Unique tickers |
| Recommended Buys | Top HIGH/MEDIUM confidence BUY signals with SL/TP/qty calculator |
| Signal feed | Full filterable list (action, confidence, ticker search) |
| Risk Config | Live view of your SL/TP/Capital/Mode from config.py |
| Top Tickers | Most mentioned tickers with buy count bar chart |
| Recent Trades | Paper/live trades logged to trades.db |

**Kite login flow (every morning):**
1. Click the **Kite ○** chip in the top-right header
2. Log in to Zerodha (username + password + TOTP)
3. Automatically redirected back — token saved to `kite_token.json`
4. Chip turns green: **Kite ✓**
5. Enable the **Trading OFF** toggle to allow order execution

**Trading safety:** The trading toggle resets to OFF every time the dashboard restarts.
You must explicitly enable it each session. Paper mode is the default — no real orders until
`PAPER_TRADE = False` in `config.py`.

---

## How It Works

```
Telegram channels
       │
       ├── batch_fetcher.py    (7-day history, chronological)
       └── realtime_listener.py  (live NewMessage events)
                 │
         [Promo filter]  ← drops ads / course invites / referral links
                 │
                 ▼
         message_queue.py  (asyncio.Queue — text vs image routing)
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
                 │
           dashboard/app.py  →  http://localhost:8000
                 │
           kite/order_manager.py  →  Zerodha MIS order
```

### Extracted fields per signal

| Field | Description |
|---|---|
| `ticker` | NSE symbol e.g. `RELIANCE`, `NIFTY` |
| `action` | `BUY` / `SELL` / `HOLD` / `WATCH` |
| `entry_price` | Entry price level |
| `target_price` | Take-profit target |
| `stop_loss` | Stop-loss level |
| `sentiment` | `BULLISH` / `BEARISH` / `NEUTRAL` |
| `confidence` | `HIGH` / `MEDIUM` / `LOW` |
| `timeframe` | `intraday` / `swing` / `long-term` |
| `summary` | One-sentence signal summary |
| `message_type` | `text` or `image` |

---

## Notes

- Media is **never written to disk** — image bytes live in memory only.
- SQLite uses **WAL mode** for safe concurrent reads/writes.
- LLM calls retry up to 3× with exponential backoff.
- Duplicate messages silently skipped via `INSERT OR IGNORE` on `message_id`.
- Promotional messages (course ads, channel invites, referral links) filtered before LLM.
- `telegram.session` and `kite_token.json` are gitignored — copy manually between machines.
- On a new device: first `python main.py` run will prompt for Telegram OTP.
