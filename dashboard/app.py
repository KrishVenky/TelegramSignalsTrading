"""
dashboard/app.py — Signal dashboard with Kite Connect integration.

Run: uvicorn dashboard.app:app --reload --port 8000
Then: http://localhost:8000

Features:
  - Live signal feed from signals.db (auto-refreshes)
  - Recommended Buys: top HIGH/MEDIUM confidence BUY signals
  - Kite Connect auth (morning login button → /kite/login → /kite/callback)
  - Paper / Live mode toggle (Paper = safe default)
  - Trading master switch (OFF by default — must enable per session)
  - Confirm Buy modal with calculated qty, SL, TP, total cost
  - Recent trades sidebar (from trades.db)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

# ── Paths ─────────────────────────────────────────────────────────────────
_ROOT     = Path(__file__).parent.parent
_SIGNALS  = str(_ROOT / "signals.db")

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Signal Dashboard", docs_url=None, redoc_url=None)

# ── In-memory state (resets on restart — intentional) ─────────────────────
_trading_enabled: bool = False   # master kill-switch; OFF by default


# ── DB helpers ─────────────────────────────────────────────────────────────

def _sconn() -> sqlite3.Connection:
    conn = sqlite3.connect(_SIGNALS, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ── Kite helpers (graceful degradation if not set up) ─────────────────────

def _kite_connected() -> bool:
    try:
        from kite.client import is_connected
        return is_connected()
    except Exception:
        return False


def _get_ltp(ticker: str) -> Optional[float]:
    try:
        from kite.client import get_ltp
        return get_ltp(ticker)
    except Exception:
        return None


# ── Pydantic models ────────────────────────────────────────────────────────

class TradingModeReq(BaseModel):
    enabled: bool

class ExecuteTradeReq(BaseModel):
    signal_id: str


# ══════════════════════════════════════════════════════════════════════════
# Kite auth routes
# ══════════════════════════════════════════════════════════════════════════

@app.get("/kite/login")
def kite_login():
    """Redirect browser to Zerodha OAuth login page."""
    try:
        from kite.client import get_login_url
        return RedirectResponse(get_login_url())
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/kite/callback")
def kite_callback(request: Request):
    """
    Zerodha redirects here after login with ?request_token=XXXX&status=success.
    Exchange for access_token and redirect back to dashboard.
    """
    params   = dict(request.query_params)
    rt       = params.get("request_token")
    status   = params.get("status", "")

    if status != "success" or not rt:
        return RedirectResponse("/?kite=error")

    try:
        from kite.client import complete_login
        complete_login(rt)
        return RedirectResponse("/?kite=connected")
    except Exception as exc:
        return RedirectResponse(f"/?kite=error&msg={str(exc)[:80]}")


# ══════════════════════════════════════════════════════════════════════════
# Status / control API
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/kite/status")
def kite_status():
    from config import PAPER_TRADE, TRADE_STOP_LOSS_PCT, TRADE_TAKE_PROFIT_PCT, CAPITAL_PER_TRADE
    return {
        "connected":       _kite_connected(),
        "paper_mode":      PAPER_TRADE,
        "trading_enabled": _trading_enabled,
        "sl_pct":          TRADE_STOP_LOSS_PCT * 100,
        "tp_pct":          TRADE_TAKE_PROFIT_PCT * 100,
        "capital":         CAPITAL_PER_TRADE,
    }


@app.post("/api/trading-mode")
def set_trading_mode(body: TradingModeReq):
    global _trading_enabled
    _trading_enabled = body.enabled
    return {"trading_enabled": _trading_enabled}


# ══════════════════════════════════════════════════════════════════════════
# Signal data API
# ══════════════════════════════════════════════════════════════════════════

@app.get("/api/signals")
def get_signals(
    action:     Optional[str] = Query(None),
    confidence: Optional[str] = Query(None),
    ticker:     Optional[str] = Query(None),
    limit:      int           = Query(60, le=200),
):
    clauses = ["(ticker IS NOT NULL OR action IS NOT NULL)"]
    params: list = []
    if action:
        clauses.append("UPPER(action) = ?"); params.append(action.upper())
    if confidence:
        clauses.append("UPPER(confidence) = ?"); params.append(confidence.upper())
    if ticker:
        clauses.append("UPPER(ticker) LIKE ?"); params.append(f"%{ticker.upper()}%")

    where = "WHERE " + " AND ".join(clauses)
    params.append(limit)
    conn = _sconn()
    try:
        return _rows(conn.execute(
            f"SELECT * FROM signals {where} ORDER BY timestamp DESC LIMIT ?", params
        ).fetchall())
    finally:
        conn.close()


@app.get("/api/stats")
def get_stats():
    conn = _sconn()
    try:
        def q(sql): return conn.execute(sql).fetchone()[0]
        return {
            "total":           q("SELECT COUNT(*) FROM signals WHERE ticker IS NOT NULL OR action IS NOT NULL"),
            "buys":            q("SELECT COUNT(*) FROM signals WHERE UPPER(action)='BUY'"),
            "sells":           q("SELECT COUNT(*) FROM signals WHERE UPPER(action)='SELL'"),
            "high_confidence": q("SELECT COUNT(*) FROM signals WHERE UPPER(confidence)='HIGH' AND (ticker IS NOT NULL OR action IS NOT NULL)"),
            "unique_tickers":  q("SELECT COUNT(DISTINCT ticker) FROM signals WHERE ticker IS NOT NULL"),
            "latest_signal":   (conn.execute("SELECT timestamp FROM signals WHERE ticker IS NOT NULL ORDER BY timestamp DESC LIMIT 1").fetchone() or [None])[0],
        }
    finally:
        conn.close()


@app.get("/api/top_tickers")
def get_top_tickers():
    conn = _sconn()
    try:
        return _rows(conn.execute("""
            SELECT ticker, COUNT(*) as count,
                   SUM(CASE WHEN UPPER(action)='BUY' THEN 1 ELSE 0 END) as buys
            FROM signals
            WHERE ticker IS NOT NULL AND ticker != ''
            GROUP BY ticker ORDER BY count DESC LIMIT 10
        """).fetchall())
    finally:
        conn.close()


@app.get("/api/recommended")
def get_recommended():
    """Top BUY signals (HIGH/MEDIUM confidence, last 72h) with calculated trade details."""
    from config import CAPITAL_PER_TRADE, TRADE_STOP_LOSS_PCT, TRADE_TAKE_PROFIT_PCT

    conn = _sconn()
    try:
        rows = _rows(conn.execute("""
            SELECT * FROM signals
            WHERE UPPER(action) = 'BUY'
              AND UPPER(confidence) IN ('HIGH', 'MEDIUM')
              AND ticker IS NOT NULL AND ticker != ''
            ORDER BY
              CASE UPPER(confidence) WHEN 'HIGH' THEN 1 ELSE 2 END,
              timestamp DESC
            LIMIT 8
        """).fetchall())
    finally:
        conn.close()

    result = []
    for s in rows:
        ltp   = _get_ltp(s["ticker"])
        entry = ltp or s.get("entry_price") or 0
        if entry and entry > 0:
            qty = max(1, int(CAPITAL_PER_TRADE / entry))
            s["calc"] = {
                "ltp":      ltp,
                "entry":    entry,
                "quantity": qty,
                "total":    round(qty * entry, 2),
                "sl":       round(entry * (1 - TRADE_STOP_LOSS_PCT), 2),
                "tp":       round(entry * (1 + TRADE_TAKE_PROFIT_PCT), 2),
                "sl_pct":   round(TRADE_STOP_LOSS_PCT * 100, 1),
                "tp_pct":   round(TRADE_TAKE_PROFIT_PCT * 100, 1),
                "max_loss": round(qty * entry * TRADE_STOP_LOSS_PCT, 2),
                "max_gain": round(qty * entry * TRADE_TAKE_PROFIT_PCT, 2),
            }
        else:
            s["calc"] = None
        result.append(s)
    return result


@app.post("/api/execute-trade")
def execute_trade(body: ExecuteTradeReq):
    """Place a trade (paper or live) for a given signal_id."""
    if not _trading_enabled:
        return JSONResponse({"error": "Trading is OFF. Enable it in the dashboard first."}, 422)

    conn = _sconn()
    row = conn.execute(
        "SELECT * FROM signals WHERE message_id = ?", (body.signal_id,)
    ).fetchone()
    conn.close()

    if not row:
        return JSONResponse({"error": "Signal not found."}, 404)

    signal = dict(row)
    ltp    = _get_ltp(signal["ticker"])
    if not ltp:
        ltp = signal.get("entry_price")
    if not ltp:
        return JSONResponse({"error": f"Cannot determine LTP for {signal['ticker']}. Kite not connected?"}, 422)

    from kite.order_manager import place_buy
    trade = place_buy(signal, float(ltp))
    return trade


@app.get("/api/trades")
def get_trades(limit: int = Query(20, le=100)):
    try:
        from kite.order_manager import get_all_trades
        return get_all_trades(limit)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════════
# HTML
# ══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(_HTML)


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Signal Intelligence Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#08080d;--surface:#0f0f17;--card:#13131e;--border:#1c1c2e;
  --accent:#6366f1;--accent2:#818cf8;--accent3:#a5b4fc;
  --green:#22c55e;--green-d:rgba(34,197,94,.1);--green-b:rgba(34,197,94,.25);
  --red:#ef4444;--red-d:rgba(239,68,68,.1);--red-b:rgba(239,68,68,.25);
  --amber:#f59e0b;--amber-d:rgba(245,158,11,.1);
  --text:#e2e8f0;--muted:#64748b;--subtle:#1e1e30;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;min-height:100vh}

/* ── HEADER ── */
header{
  background:rgba(15,15,23,.95);border-bottom:1px solid var(--border);
  padding:0 20px;height:58px;display:flex;align-items:center;gap:16px;
  position:sticky;top:0;z-index:200;backdrop-filter:blur(16px);
}
.logo{font-weight:700;font-size:15px;letter-spacing:-.3px;display:flex;align-items:center;gap:8px;white-space:nowrap}
.pulse{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.hspacer{flex:1}
.chip{
  display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;
  font-size:12px;font-weight:600;cursor:pointer;border:1px solid var(--border);
  background:var(--card);transition:all .15s;white-space:nowrap
}
.chip:hover{border-color:var(--accent)}
.chip.kite-on{border-color:var(--green);color:var(--green)}
.chip.kite-off{border-color:var(--red);color:var(--muted)}
.chip.kite-off:hover{border-color:var(--accent2);color:var(--accent2)}
#tradingToggle{cursor:pointer;user-select:none}
#tradingToggle.on{background:rgba(239,68,68,.15);border-color:var(--red);color:var(--red)}
#tradingToggle.off{background:var(--card);border-color:var(--border);color:var(--muted)}
.last-upd{font-size:11px;color:var(--muted)}

/* ── ALERT BANNER ── */
.alert-bar{
  display:none;padding:10px 20px;font-size:13px;font-weight:500;
  border-bottom:1px solid;text-align:center;
}
.alert-bar.trading-live{display:block;background:rgba(239,68,68,.08);border-color:var(--red-b);color:var(--red)}
.alert-bar.market-warn{display:block;background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.3);color:var(--amber)}

/* ── MAIN LAYOUT ── */
main{max-width:1440px;margin:0 auto;padding:20px}

/* ── STATS ── */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px;transition:border-color .2s}
.stat:hover{border-color:var(--accent)}
.stat-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.stat-val{font-size:26px;font-weight:700;font-family:'JetBrains Mono',monospace}
.c-green{color:var(--green)}.c-red{color:var(--red)}.c-acc{color:var(--accent2)}.c-amb{color:var(--amber)}

/* ── RECOMMENDED ── */
.rec-section{margin-bottom:20px}
.section-title{
  font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;
  color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:8px
}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.rec-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.rec-card{
  background:var(--card);border:1px solid var(--green-b);border-radius:14px;
  padding:16px;position:relative;overflow:hidden;transition:all .15s
}
.rec-card::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(34,197,94,.04),transparent);pointer-events:none}
.rec-card:hover{border-color:var(--green);transform:translateY(-1px)}
.rec-header{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.rec-ticker{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:18px}
.ltp-badge{font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace}
.rec-prices{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0;padding:10px;background:var(--bg);border-radius:8px}
.prc-itm{text-align:center}
.prc-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.prc-val{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:600}
.prc-val.e{color:var(--text)}.prc-val.t{color:var(--green)}.prc-val.s{color:var(--red)}
.rec-meta{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-bottom:10px}
.rec-summary{font-size:12px;color:#94a3b8;line-height:1.5;margin-bottom:12px}
.rec-actions{display:flex;gap:8px}
.btn-confirm{
  flex:1;background:var(--green);color:#000;border:none;padding:9px 16px;
  border-radius:8px;font-family:'Inter',sans-serif;font-size:13px;font-weight:700;
  cursor:pointer;transition:opacity .15s;
}
.btn-confirm:hover{opacity:.85}
.btn-dismiss{
  background:var(--subtle);color:var(--muted);border:1px solid var(--border);
  padding:9px 12px;border-radius:8px;font-size:13px;cursor:pointer;font-family:'Inter',sans-serif;
  transition:all .15s
}
.btn-dismiss:hover{color:var(--text)}
.rec-empty{color:var(--muted);font-size:13px;padding:20px 0}

/* ── CONTROLS ── */
.controls{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center}
.fg{display:flex;gap:4px;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:3px}
.fb{background:none;border:none;color:var(--muted);padding:5px 12px;border-radius:5px;cursor:pointer;font-family:'Inter',sans-serif;font-size:12px;font-weight:500;transition:all .12s}
.fb:hover{color:var(--text);background:var(--subtle)}
.fb.active{background:var(--accent);color:#fff}
.fb.buy.active{background:var(--green)}.fb.sell.active{background:var(--red)}
.search{
  background:var(--card);border:1px solid var(--border);color:var(--text);
  padding:7px 12px;border-radius:8px;font-size:13px;outline:none;width:160px;
  font-family:'Inter',sans-serif;transition:border-color .2s
}
.search:focus{border-color:var(--accent)}
.search::placeholder{color:var(--muted)}
.btn-refresh{
  background:var(--accent);border:none;color:#fff;padding:7px 14px;border-radius:8px;
  cursor:pointer;font-size:12px;font-family:'Inter',sans-serif;font-weight:500;transition:opacity .15s
}
.btn-refresh:hover{opacity:.85}

/* ── MAIN GRID ── */
.layout{display:grid;grid-template-columns:1fr 270px;gap:16px}
@media(max-width:900px){.layout{grid-template-columns:1fr}}

/* ── SIGNAL CARDS ── */
.feed{display:flex;flex-direction:column;gap:8px}
.sig-card{
  background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:14px;transition:all .15s;position:relative;overflow:hidden
}
.sig-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--border)}
.sig-card.buy::before{background:var(--green)}.sig-card.sell::before{background:var(--red)}
.sig-card:hover{border-color:var(--accent2);transform:translateX(2px)}
.sh{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}
.sticker{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px}
.abadge{padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase}
.abadge.buy{background:var(--green-d);color:var(--green);border:1px solid var(--green-b)}
.abadge.sell{background:var(--red-d);color:var(--red);border:1px solid var(--red-b)}
.abadge.hold{background:var(--amber-d);color:var(--amber);border:1px solid rgba(245,158,11,.3)}
.abadge.watch{background:rgba(99,102,241,.1);color:var(--accent2);border:1px solid rgba(99,102,241,.25)}
.cbadge{padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.cbadge.high{background:rgba(34,197,94,.12);color:var(--green)}
.cbadge.medium{background:rgba(245,158,11,.12);color:var(--amber)}
.cbadge.low{background:rgba(100,116,139,.12);color:var(--muted)}
.tbadge{margin-left:auto;padding:2px 7px;border-radius:4px;font-size:10px;color:var(--muted);background:var(--subtle);text-transform:uppercase}
.prices{display:flex;gap:16px;margin:8px 0;flex-wrap:wrap}
.pi{display:flex;flex-direction:column;gap:1px}
.pl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.pv{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:600}
.pv.e{color:var(--text)}.pv.t{color:var(--green)}.pv.s{color:var(--red)}
.rr{font-size:11px;background:rgba(99,102,241,.1);color:var(--accent2);border:1px solid rgba(99,102,241,.2);padding:2px 7px;border-radius:4px;font-family:'JetBrains Mono',monospace;align-self:flex-end}
.summ{font-size:12px;color:#94a3b8;line-height:1.5;margin:6px 0}
.sf{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)}
.chan{font-size:11px;color:var(--muted)}
.tftag{font-size:11px;color:var(--accent2);background:rgba(99,102,241,.08);padding:1px 7px;border-radius:4px}
.ts{font-size:11px;color:var(--muted);margin-left:auto;font-family:'JetBrains Mono',monospace}

/* ── SIDEBAR ── */
.sidebar{display:flex;flex-direction:column;gap:14px}
.side-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px}
.side-ttl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px;font-weight:600}
.tkrow{display:flex;align-items:center;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)}
.tkrow:last-child{border-bottom:none}
.tkname{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:12px}
.tkcnt{font-size:11px;color:var(--muted)}
.tkbar-w{width:50px;height:3px;background:var(--border);border-radius:2px}
.tkbar{height:3px;background:var(--accent);border-radius:2px}
.tr-row{padding:7px 0;border-bottom:1px solid var(--border)}
.tr-row:last-child{border-bottom:none}
.tr-ticker{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:12px;margin-bottom:3px}
.tr-detail{font-size:11px;color:var(--muted)}
.tr-badge{font-size:10px;padding:1px 6px;border-radius:3px;margin-left:auto}
.tr-badge.paper{background:rgba(99,102,241,.1);color:var(--accent2)}
.tr-badge.live{background:var(--green-d);color:var(--green)}

/* ── EMPTY & LOADING ── */
.empty{text-align:center;padding:40px 20px;color:var(--muted);font-size:13px}
.loading{text-align:center;padding:40px}
.spin{width:24px;height:24px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 10px}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── CONFIG PANEL ── */
.cfg-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px}
.cfg-row:last-child{border-bottom:none}
.cfg-key{color:var(--muted)}
.cfg-val{font-family:'JetBrains Mono',monospace;font-weight:600;color:var(--accent2)}

/* ── MODAL ── */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;width:420px;max-width:95vw}
.modal-title{font-size:16px;font-weight:700;margin-bottom:20px;display:flex;align-items:center;gap:8px}
.modal-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.modal-row:last-of-type{border-bottom:none}
.modal-key{font-size:12px;color:var(--muted)}
.modal-val{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:600}
.modal-val.green{color:var(--green)}.modal-val.red{color:var(--red)}.modal-val.amb{color:var(--amber)}
.modal-mode{
  margin:14px 0;padding:10px 14px;border-radius:8px;font-size:12px;font-weight:500;
  display:flex;align-items:center;gap:6px
}
.modal-mode.paper{background:rgba(99,102,241,.08);color:var(--accent2);border:1px solid rgba(99,102,241,.2)}
.modal-mode.live{background:var(--red-d);color:var(--red);border:1px solid var(--red-b)}
.modal-btns{display:flex;gap:10px;margin-top:16px}
.btn-cancel{flex:1;background:var(--subtle);border:1px solid var(--border);color:var(--muted);padding:10px;border-radius:8px;cursor:pointer;font-family:'Inter',sans-serif;font-size:13px;transition:all .15s}
.btn-cancel:hover{color:var(--text)}
.btn-exec{flex:2;border:none;padding:10px;border-radius:8px;cursor:pointer;font-family:'Inter',sans-serif;font-size:13px;font-weight:700;transition:opacity .15s}
.btn-exec.paper{background:var(--accent);color:#fff}
.btn-exec.live{background:var(--red);color:#fff}
.btn-exec:hover{opacity:.85}
</style>
</head>
<body>

<!-- ── HEADER ── -->
<header>
  <div class="logo"><div class="pulse"></div>Signal Intel</div>
  <div class="hspacer"></div>
  <div id="kiteChip" class="chip kite-off" onclick="handleKiteClick()" title="Click to connect Kite">
    <span id="kiteIcon">○</span> Kite
  </div>
  <div id="tradingToggle" class="chip off" onclick="toggleTrading()" title="Enable/disable trade execution">
    <span id="tradingIcon">⬤</span> <span id="tradingLabel">Trading OFF</span>
  </div>
  <div class="last-upd" id="lastUpd">—</div>
</header>

<!-- ── ALERT BANNERS ── -->
<div id="tradingBanner" class="alert-bar">
  ⚠️ TRADING ACTIVE — Orders will be placed on Zerodha. Disable before leaving.
</div>
<div id="paperBanner" class="alert-bar market-warn" style="display:none">
  📄 PAPER MODE — Trades are simulated only. Edit PAPER_TRADE in config.py to go live.
</div>

<main>
  <!-- Stats -->
  <div class="stats">
    <div class="stat"><div class="stat-lbl">Total Signals</div><div class="stat-val c-acc" id="s-total">—</div></div>
    <div class="stat"><div class="stat-lbl">Buy Signals</div><div class="stat-val c-green" id="s-buys">—</div></div>
    <div class="stat"><div class="stat-lbl">Sell Signals</div><div class="stat-val c-red" id="s-sells">—</div></div>
    <div class="stat"><div class="stat-lbl">High Confidence</div><div class="stat-val c-amb" id="s-high">—</div></div>
    <div class="stat"><div class="stat-lbl">Unique Tickers</div><div class="stat-val c-acc" id="s-tickers">—</div></div>
    <div class="stat"><div class="stat-lbl">Latest Signal</div><div class="stat-val" style="font-size:12px;margin-top:4px" id="s-latest">—</div></div>
  </div>

  <!-- Recommended Buys -->
  <div class="rec-section">
    <div class="section-title">🎯 Recommended Buys</div>
    <div id="recGrid" class="rec-grid">
      <div class="loading"><div class="spin"></div></div>
    </div>
  </div>

  <!-- Controls -->
  <div class="controls">
    <div class="fg">
      <button class="fb active" data-action="">All</button>
      <button class="fb buy"  data-action="BUY">Buy</button>
      <button class="fb sell" data-action="SELL">Sell</button>
    </div>
    <div class="fg">
      <button class="fb active" data-conf="">All Conf</button>
      <button class="fb" data-conf="HIGH">High</button>
      <button class="fb" data-conf="MEDIUM">Med</button>
    </div>
    <input class="search" id="tickerQ" placeholder="Search ticker…">
    <div style="flex:1"></div>
    <button class="btn-refresh" onclick="loadAll()">↻ Refresh</button>
  </div>

  <!-- Layout -->
  <div class="layout">
    <div id="sigFeed" class="feed">
      <div class="loading"><div class="spin"></div>Loading…</div>
    </div>
    <div class="sidebar">
      <!-- Config panel -->
      <div class="side-card">
        <div class="side-ttl">Risk Config</div>
        <div class="cfg-row"><span class="cfg-key">Stop Loss</span><span class="cfg-val" id="cfg-sl">—</span></div>
        <div class="cfg-row"><span class="cfg-key">Take Profit</span><span class="cfg-val" id="cfg-tp">—</span></div>
        <div class="cfg-row"><span class="cfg-key">Capital/Trade</span><span class="cfg-val" id="cfg-cap">—</span></div>
        <div class="cfg-row"><span class="cfg-key">Mode</span><span class="cfg-val" id="cfg-mode">—</span></div>
        <div style="font-size:10px;color:var(--muted);margin-top:10px">Edit config.py to change SL/TP/Capital</div>
      </div>
      <!-- Top Tickers -->
      <div class="side-card">
        <div class="side-ttl">Top Tickers</div>
        <div id="topTickers">—</div>
      </div>
      <!-- Recent Trades -->
      <div class="side-card">
        <div class="side-ttl">Recent Trades</div>
        <div id="recentTrades"><span style="color:var(--muted);font-size:12px">No trades yet</span></div>
      </div>
    </div>
  </div>
</main>

<!-- ── CONFIRM TRADE MODAL ── -->
<div class="overlay" id="tradeModal">
  <div class="modal">
    <div class="modal-title">
      <span class="abadge buy" style="font-size:13px">BUY</span>
      <span id="m-ticker" style="font-family:'JetBrains Mono',monospace;font-size:19px"></span>
    </div>
    <div class="modal-row"><span class="modal-key">Entry Price (LTP)</span><span class="modal-val" id="m-entry">—</span></div>
    <div class="modal-row"><span class="modal-key">Quantity</span><span class="modal-val" id="m-qty">—</span></div>
    <div class="modal-row"><span class="modal-key">Total Cost</span><span class="modal-val" id="m-total">—</span></div>
    <div class="modal-row"><span class="modal-key">Stop Loss (−<span id="m-sl-pct">1</span>%)</span><span class="modal-val red" id="m-sl">—</span></div>
    <div class="modal-row"><span class="modal-key">→ Max Loss</span><span class="modal-val red" id="m-loss">—</span></div>
    <div class="modal-row"><span class="modal-key">Take Profit (+<span id="m-tp-pct">2</span>%)</span><span class="modal-val green" id="m-tp">—</span></div>
    <div class="modal-row"><span class="modal-key">→ Max Gain</span><span class="modal-val green" id="m-gain">—</span></div>
    <div id="m-mode-badge" class="modal-mode paper">📄 Paper Trade — no real order will be placed</div>
    <div class="modal-btns">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-exec paper" id="m-exec-btn" onclick="executeConfirmed()">Execute Paper Trade</button>
    </div>
  </div>
</div>

<script>
// ── State ────────────────────────────────────────────────────────────────
let _state   = { action:'', conf:'', ticker:'' };
let _kite    = { connected:false, paper:true, enabled:false, sl:1, tp:2, capital:10000 };
let _pending = null;   // signal waiting for modal confirm

// ── Filters ──────────────────────────────────────────────────────────────
document.querySelectorAll('[data-action]').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('[data-action]').forEach(x=>x.classList.remove('active','buy','sell'));
    b.classList.add('active');
    if(b.dataset.action==='BUY')b.classList.add('buy');
    if(b.dataset.action==='SELL')b.classList.add('sell');
    _state.action=b.dataset.action; loadSignals();
  };
});
document.querySelectorAll('[data-conf]').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('[data-conf]').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); _state.conf=b.dataset.conf; loadSignals();
  };
});
let _st;
document.getElementById('tickerQ').oninput=e=>{
  clearTimeout(_st); _st=setTimeout(()=>{ _state.ticker=e.target.value; loadSignals(); },350);
};

// ── Kite status ──────────────────────────────────────────────────────────
async function loadKiteStatus(){
  try{
    const r=await fetch('/api/kite/status'); const d=await r.json();
    _kite={connected:d.connected,paper:d.paper_mode,enabled:d.trading_enabled,
           sl:d.sl_pct,tp:d.tp_pct,capital:d.capital};

    const chip=document.getElementById('kiteChip');
    if(d.connected){
      chip.className='chip kite-on';
      document.getElementById('kiteIcon').textContent='✓';
    }else{
      chip.className='chip kite-off';
      document.getElementById('kiteIcon').textContent='○';
    }

    // Trading toggle
    const tog=document.getElementById('tradingToggle');
    tog.className='chip '+(d.trading_enabled?'on':'off');
    document.getElementById('tradingIcon').textContent=d.trading_enabled?'⬤':'○';
    document.getElementById('tradingLabel').textContent=d.trading_enabled?'Trading ON':'Trading OFF';

    // Alert banners
    document.getElementById('tradingBanner').className='alert-bar'+(d.trading_enabled&&!d.paper_mode?' trading-live':'');
    document.getElementById('paperBanner').style.display=(d.trading_enabled&&d.paper_mode)?'block':'none';

    // Config panel
    document.getElementById('cfg-sl').textContent  = d.sl_pct+'%';
    document.getElementById('cfg-tp').textContent  = d.tp_pct+'%';
    document.getElementById('cfg-cap').textContent = '₹'+d.capital.toLocaleString('en-IN');
    document.getElementById('cfg-mode').textContent= d.paper_mode?'📄 Paper':'🔴 Live';
  }catch(e){}
}

function handleKiteClick(){
  if(_kite.connected){return;}
  window.location.href='/kite/login';
}

async function toggleTrading(){
  const newVal=!_kite.enabled;
  if(newVal&&!_kite.paper){
    if(!confirm('⚠️ Enable LIVE trading? Real money orders will be placed on Zerodha.\n\nClick OK only if markets are open and you want to trade.'))return;
  }
  await fetch('/api/trading-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:newVal})});
  await loadKiteStatus();
}

// ── Stats ────────────────────────────────────────────────────────────────
async function loadStats(){
  try{
    const d=await(await fetch('/api/stats')).json();
    document.getElementById('s-total').textContent  =d.total;
    document.getElementById('s-buys').textContent   =d.buys;
    document.getElementById('s-sells').textContent  =d.sells;
    document.getElementById('s-high').textContent   =d.high_confidence;
    document.getElementById('s-tickers').textContent=d.unique_tickers;
    document.getElementById('s-latest').textContent =fmtTs(d.latest_signal);
  }catch(e){}
}

// ── Recommended ──────────────────────────────────────────────────────────
async function loadRecommended(){
  const grid=document.getElementById('recGrid');
  try{
    const data=await(await fetch('/api/recommended')).json();
    if(!data.length){grid.innerHTML='<div class="rec-empty">No high-confidence buy signals in the last 72 hours.</div>';return;}
    grid.innerHTML=data.map(s=>{
      const c=s.calc;
      const priceRows=c?`
        <div class="rec-prices">
          <div class="prc-itm"><div class="prc-lbl">Entry${c.ltp?'(LTP)':''}</div><div class="prc-val e">₹${n2(c.entry)}</div></div>
          <div class="prc-itm"><div class="prc-lbl">SL −${c.sl_pct}%</div><div class="prc-val s">₹${n2(c.sl)}</div></div>
          <div class="prc-itm"><div class="prc-lbl">TP +${c.tp_pct}%</div><div class="prc-val t">₹${n2(c.tp)}</div></div>
        </div>
        <div class="rec-meta">
          <span>Qty ~${c.quantity} shares</span>
          <span>Total ~₹${Math.round(c.total).toLocaleString('en-IN')}</span>
          <span>Max loss −₹${Math.round(c.max_loss)}</span>
          <span>Target +₹${Math.round(c.max_gain)}</span>
        </div>`:
        `<div class="rec-meta" style="color:var(--red)">No entry price — connect Kite for LTP</div>`;
      const canBuy=_kite.enabled && (c && c.entry>0);
      return `<div class="rec-card" id="rc-${s.message_id.replace(/[^a-z0-9]/gi,'_')}">
        <div class="rec-header">
          <span class="rec-ticker">${s.ticker}</span>
          <span class="abadge buy">BUY</span>
          <span class="cbadge ${(s.confidence||'').toLowerCase()}">${s.confidence||''}</span>
          ${c&&c.ltp?`<span class="ltp-badge">LTP ₹${n2(c.ltp)}</span>`:''}
        </div>
        ${priceRows}
        ${s.summary?`<div class="rec-summary">${s.summary}</div>`:''}
        <div class="rec-meta"><span>${s.channel||''}</span><span>${fmtTs(s.timestamp)}</span></div>
        <div class="rec-actions">
          <button class="btn-confirm" onclick='openModal(${JSON.stringify(s)})' ${canBuy?'':'title="Enable trading first"'}>
            ${_kite.enabled?'✓ Confirm Buy':'Enable Trading to Buy'}
          </button>
          <button class="btn-dismiss" onclick="dismissRec('rc-${s.message_id.replace(/[^a-z0-9]/gi,'_')}')">✕</button>
        </div>
      </div>`;
    }).join('');
  }catch(e){grid.innerHTML='<div class="rec-empty">Failed to load recommendations.</div>';}
}

function dismissRec(id){document.getElementById(id)?.remove();}

// ── Signal feed ──────────────────────────────────────────────────────────
async function loadSignals(){
  const feed=document.getElementById('sigFeed');
  const p=new URLSearchParams({limit:60});
  if(_state.action)p.set('action',_state.action);
  if(_state.conf)p.set('confidence',_state.conf);
  if(_state.ticker)p.set('ticker',_state.ticker);
  try{
    const data=await(await fetch('/api/signals?'+p)).json();
    if(!data.length){feed.innerHTML='<div class="empty">No signals match the current filters.</div>';return;}
    feed.innerHTML=data.map(renderCard).join('');
  }catch(e){feed.innerHTML='<div class="empty">Failed to load signals.</div>';}
}

function renderCard(s){
  const ac=aClass(s.action);
  const ep=s.entry_price?'₹'+n2(s.entry_price):null;
  const tp=s.target_price?'₹'+n2(s.target_price):null;
  const sl=s.stop_loss?'₹'+n2(s.stop_loss):null;
  const rr=rrCalc(s.entry_price,s.target_price,s.stop_loss);
  return `<div class="sig-card ${ac}">
    <div class="sh">
      <span class="sticker">${s.ticker||'—'}</span>
      ${s.action?`<span class="abadge ${ac}">${s.action.toUpperCase()}</span>`:''}
      ${s.confidence?`<span class="cbadge ${(s.confidence||'').toLowerCase()}">${s.confidence}</span>`:''}
      <span class="tbadge">${s.message_type||'text'}</span>
    </div>
    ${(ep||tp||sl)?`<div class="prices">
      ${ep?`<div class="pi"><div class="pl">Entry</div><div class="pv e">${ep}</div></div>`:''}
      ${tp?`<div class="pi"><div class="pl">Target</div><div class="pv t">${tp}</div></div>`:''}
      ${sl?`<div class="pi"><div class="pl">Stop</div><div class="pv s">${sl}</div></div>`:''}
      ${rr?`<div class="pi" style="margin-left:auto"><div class="pl">R:R</div><span class="rr">1:${rr}</span></div>`:''}
    </div>`:''}
    ${s.summary?`<div class="summ">${s.summary}</div>`:''}
    <div class="sf">
      <span class="chan">${s.channel||''}</span>
      ${s.timeframe?`<span class="tftag">${s.timeframe}</span>`:''}
      <span class="ts">${fmtTs(s.timestamp)}</span>
    </div>
  </div>`;
}

// ── Top tickers ──────────────────────────────────────────────────────────
async function loadTopTickers(){
  try{
    const data=await(await fetch('/api/top_tickers')).json();
    if(!data.length){document.getElementById('topTickers').textContent='No data';return;}
    const max=data[0].count;
    document.getElementById('topTickers').innerHTML=data.map(t=>`
      <div class="tkrow">
        <div><div class="tkname">${t.ticker}</div><div class="tkcnt">${t.count} · ${t.buys} buy</div></div>
        <div class="tkbar-w"><div class="tkbar" style="width:${Math.round(t.count/max*100)}%"></div></div>
      </div>`).join('');
  }catch(e){}
}

// ── Trades ───────────────────────────────────────────────────────────────
async function loadTrades(){
  try{
    const data=await(await fetch('/api/trades?limit=5')).json();
    if(!data.length){return;}
    document.getElementById('recentTrades').innerHTML=data.map(t=>`
      <div class="tr-row">
        <div style="display:flex;align-items:center;gap:6px">
          <span class="tr-ticker">${t.ticker}</span>
          <span class="tr-badge ${t.mode}">${t.mode}</span>
        </div>
        <div class="tr-detail">x${t.quantity} @ ₹${n2(t.entry_price)} · SL ₹${n2(t.sl_price)} · TP ₹${n2(t.tp_price)}</div>
        <div class="tr-detail">${fmtTs(t.entered_at)}</div>
      </div>`).join('');
  }catch(e){}
}

// ── Modal ────────────────────────────────────────────────────────────────
function openModal(signal){
  _pending=signal;
  const c=signal.calc;
  if(!c||!c.entry){alert('No price data available. Connect Kite to fetch LTP.');return;}
  if(!_kite.enabled){alert('Enable Trading first using the toggle in the header.');return;}
  document.getElementById('m-ticker').textContent=signal.ticker;
  document.getElementById('m-entry').textContent='₹'+n2(c.entry)+(c.ltp?' (live)':' (signal)');
  document.getElementById('m-qty').textContent=c.quantity+' shares';
  document.getElementById('m-total').textContent='₹'+Math.round(c.total).toLocaleString('en-IN');
  document.getElementById('m-sl').textContent='₹'+n2(c.sl);
  document.getElementById('m-loss').textContent='−₹'+Math.round(c.max_loss);
  document.getElementById('m-tp').textContent='₹'+n2(c.tp);
  document.getElementById('m-gain').textContent='+₹'+Math.round(c.max_gain);
  document.getElementById('m-sl-pct').textContent=c.sl_pct;
  document.getElementById('m-tp-pct').textContent=c.tp_pct;
  const isPaper=_kite.paper;
  document.getElementById('m-mode-badge').className='modal-mode '+(isPaper?'paper':'live');
  document.getElementById('m-mode-badge').textContent=isPaper?'📄 Paper Trade — no real order will be placed':'🔴 LIVE Trade — real order on Zerodha';
  const execBtn=document.getElementById('m-exec-btn');
  execBtn.className='btn-exec '+(isPaper?'paper':'live');
  execBtn.textContent=isPaper?'Execute Paper Trade':'⚠️ Execute LIVE Trade';
  document.getElementById('tradeModal').classList.add('show');
}

function closeModal(){
  document.getElementById('tradeModal').classList.remove('show');
  _pending=null;
}

async function executeConfirmed(){
  if(!_pending)return;
  const execBtn=document.getElementById('m-exec-btn');
  execBtn.disabled=true; execBtn.textContent='Placing…';
  try{
    const r=await fetch('/api/execute-trade',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({signal_id:_pending.message_id})
    });
    const d=await r.json();
    if(d.error){alert('Trade failed: '+d.error);execBtn.disabled=false;execBtn.textContent='Retry';return;}
    closeModal();
    alert(`✅ ${d.mode.toUpperCase()} Trade placed!\n${d.ticker} × ${d.quantity} @ ₹${n2(d.entry_price)}\nSL ₹${n2(d.sl_price)} | TP ₹${n2(d.tp_price)}`);
    loadTrades();
  }catch(e){alert('Network error: '+e);execBtn.disabled=false;}
}

document.getElementById('tradeModal').onclick=e=>{if(e.target===e.currentTarget)closeModal();};

// ── Helpers ──────────────────────────────────────────────────────────────
function n2(v){return v!=null?Number(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';}
function rrCalc(e,t,s){if(!e||!t||!s)return null;const r=Math.abs(t-e),ri=Math.abs(e-s);return ri?( r/ri).toFixed(1):null;}
function aClass(a){if(!a)return'';const u=a.toUpperCase();return u==='BUY'?'buy':u==='SELL'?'sell':u==='HOLD'?'hold':'watch';}
function fmtTs(ts){
  if(!ts)return'';
  try{const d=new Date(ts);return d.toLocaleDateString('en-IN',{day:'2-digit',month:'short'})+' '+d.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',hour12:true});}
  catch{return ts;}
}

// ── Boot ─────────────────────────────────────────────────────────────────
async function loadAll(){
  document.getElementById('lastUpd').textContent='Refreshing…';
  await Promise.all([loadKiteStatus(),loadStats(),loadRecommended(),loadSignals(),loadTopTickers(),loadTrades()]);
  document.getElementById('lastUpd').textContent='Updated '+new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// Handle callback messages
const urlP=new URLSearchParams(window.location.search);
if(urlP.get('kite')==='connected')setTimeout(()=>alert('✅ Kite connected! Access token saved.'),300);
if(urlP.get('kite')==='error')setTimeout(()=>alert('❌ Kite connection failed. Try again.'),300);

loadAll();
setInterval(loadAll, 30_000);
</script>
</body>
</html>
"""
