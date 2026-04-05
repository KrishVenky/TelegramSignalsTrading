"""
kite/decision_engine.py — Signal gating logic for the paper (and eventually live) trader.

Reads the latest untraded DIRECT_CALL signals from signals.db, applies all entry
guards, and returns actionable candidates.

This module is stateless — it just filters. The PaperTrader holds position state.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN: time = time(9, 20)
MARKET_CLOSE: time = time(14, 45)

SIGNALS_DB = "signals.db"


def _market_hours_now() -> bool:
    now_t = datetime.now(IST).time()
    return MARKET_OPEN <= now_t <= MARKET_CLOSE


def fetch_actionable_signals(
    already_seen: set[str],
    open_tickers: set[str],
    daily_pnl: float,
    max_daily_loss: float,
    confidence_filter: tuple[str, ...] = ("HIGH", "MEDIUM"),
) -> list[dict]:
    """
    Return signals that pass ALL entry guards:
      - Action is BUY
      - signal_type is DIRECT_CALL (if col exists)
      - Confidence is HIGH or MEDIUM
      - Not already in a position for this ticker
      - Signal not already traded today
      - Daily loss limit not breached
      - Within market hours

    Returns list of signal dicts ready for entry consideration.
    """
    if daily_pnl <= -max_daily_loss:
        logger.warning("Daily loss limit hit (₹{:.0f}) — no new entries.", daily_pnl)
        return []

    if not _market_hours_now():
        return []

    try:
        conn = sqlite3.connect(SIGNALS_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        cols = [r[1] for r in conn.execute("PRAGMA table_info(signals);").fetchall()]
        has_signal_type = "signal_type" in cols

        placeholders_conf = ", ".join("?" * len(confidence_filter))

        if has_signal_type:
            sql = f"""
                SELECT message_id, ticker, action, signal_type, confidence,
                       channel, timestamp, entry_price, sentiment
                FROM signals
                WHERE action IN ('BUY', 'Buy', 'buy')
                  AND signal_type IN ('DIRECT_CALL')
                  AND confidence IN ({placeholders_conf})
                  AND ticker IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 200
            """
            rows = conn.execute(sql, list(confidence_filter)).fetchall()
        else:
            sql = f"""
                SELECT message_id, ticker, action, confidence,
                       channel, timestamp, entry_price, sentiment,
                       NULL as signal_type
                FROM signals
                WHERE action IN ('BUY', 'Buy', 'buy')
                  AND confidence IN ({placeholders_conf})
                  AND ticker IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT 200
            """
            rows = conn.execute(sql, list(confidence_filter)).fetchall()

        conn.close()
    except sqlite3.Error as e:
        logger.error("Decision engine DB error: {}", e)
        return []

    actionable = []
    for row in rows:
        sig = dict(row)
        msg_id = sig["message_id"]
        ticker = (sig["ticker"] or "").upper()

        if msg_id in already_seen:
            continue
        if ticker in open_tickers:
            continue

        actionable.append(sig)

    logger.debug("Decision engine: {} actionable signal(s) found.", len(actionable))
    return actionable


def classify_exit(
    entry_price: float,
    current_price: float,
    take_profit_pct: float = 0.02,
    stop_loss_pct: float = 0.01,
) -> Optional[str]:
    """
    Given entry and current price, return exit reason or None if still hold.
    Returns: 'TP' | 'SL' | None
    """
    change = (current_price - entry_price) / entry_price
    if change >= take_profit_pct:
        return "TP"
    if change <= -stop_loss_pct:
        return "SL"
    return None
