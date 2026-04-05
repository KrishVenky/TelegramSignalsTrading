"""
kite/paper_trader.py — Paper trading simulator for signal validation.

Strategy:
  - Monitor signals.db for new DIRECT_CALL BUY signals (HIGH/MEDIUM confidence)
  - Simulate entry at current NSE price (no real orders)
  - Exit rules:
      * Take profit: +2% (skim the pump top)
      * Stop loss:   -1% (hard cut, no mercy)
      * Time exit:   2:45 PM IST (avoid MIS auto-square at 3:20 PM)
      * If LTP drops below entry after TP not hit within 2h → exit early
  - Tracks all trades in trades.db
  - Prints a P&L summary on exit

Run:
    python -m kite.paper_trader
    python -m kite.paper_trader --summary    # just show P&L table
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Generator, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from kite.price_fetcher import close_session, get_ltp, get_quote

# ---------------------------------------------------------------------------
# Constants / config
# ---------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

CAPITAL_PER_TRADE: float = 10_000.0   # ₹ per position
MAX_OPEN_POSITIONS: int = 3
MAX_DAILY_LOSS: float = 1_500.0        # kill switch — stop today after this loss

TAKE_PROFIT_PCT: float = 0.02          # +2%
STOP_LOSS_PCT: float = 0.01            # -1%
TIME_EXIT_IST: time = time(14, 45)     # force-close at 2:45 PM
MARKET_OPEN_IST: time = time(9, 20)    # don't trade before this
MARKET_CLOSE_IST: time = time(14, 45)  # don't enter new positions after this

POLL_INTERVAL_SECONDS: int = 30        # how often to check signals.db + prices
MIN_VOLUME: int = 50_000               # minimum daily volume to trade

SIGNALS_DB = "signals.db"
TRADES_DB = "trades.db"

# ---------------------------------------------------------------------------
# Trades DB schema
# ---------------------------------------------------------------------------

_CREATE_TRADES_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    signal_type     TEXT,
    confidence      TEXT,
    channel         TEXT,
    signal_ts       TEXT,
    entry_price     REAL,
    quantity        INTEGER,
    capital         REAL,
    entry_time      TEXT,
    exit_price      REAL,
    exit_time       TEXT,
    pnl             REAL,
    pnl_pct         REAL,
    exit_reason     TEXT,   -- TP | SL | TIME | MANUAL | MARKET_CLOSED
    trade_date      TEXT,
    is_paper        INTEGER DEFAULT 1
);
"""

_CREATE_TRADES_IDX = """
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades (trade_date);
"""


@contextmanager
def _trades_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(TRADES_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_trades_db() -> None:
    with _trades_conn() as conn:
        conn.execute(_CREATE_TRADES_SQL)
        for stmt in _CREATE_TRADES_IDX.strip().split("\n"):
            if stmt.strip():
                conn.execute(stmt)
    logger.info("Trades DB initialised at {}", TRADES_DB)


# ---------------------------------------------------------------------------
# Open position tracking
# ---------------------------------------------------------------------------

@dataclass
class Position:
    signal_id: str
    ticker: str
    entry_price: float
    quantity: int
    capital: float
    entry_time: datetime
    signal_type: str
    confidence: str
    channel: str
    signal_ts: str

    tp_price: float = field(init=False)
    sl_price: float = field(init=False)

    def __post_init__(self):
        self.tp_price = round(self.entry_price * (1 + TAKE_PROFIT_PCT), 2)
        self.sl_price = round(self.entry_price * (1 - STOP_LOSS_PCT), 2)

    @property
    def pnl(self) -> float:
        return 0.0  # calculated on exit

    def pnl_at(self, ltp: float) -> float:
        return (ltp - self.entry_price) * self.quantity

    def pnl_pct_at(self, ltp: float) -> float:
        return (ltp - self.entry_price) / self.entry_price


# ---------------------------------------------------------------------------
# Core paper trader logic
# ---------------------------------------------------------------------------

class PaperTrader:
    def __init__(self):
        self.open_positions: dict[str, Position] = {}   # ticker → Position
        self.daily_pnl: float = 0.0
        self.today: date = datetime.now(IST).date()
        self._seen_signal_ids: set[str] = set()

    # ---- helpers -----------------------------------------------------------

    def _now_ist(self) -> datetime:
        return datetime.now(IST)

    def _is_market_hours(self) -> bool:
        now_t = self._now_ist().time()
        return MARKET_OPEN_IST <= now_t <= MARKET_CLOSE_IST

    def _reset_if_new_day(self) -> None:
        today = self._now_ist().date()
        if today != self.today:
            logger.info("New trading day — resetting daily P&L and open positions.")
            self.today = today
            self.daily_pnl = 0.0
            # Force-exit carried overnight (shouldn't happen with time exit, but safety)
            self.open_positions.clear()

    def _load_seen_signals(self) -> None:
        """On startup, mark all existing trade signal_ids as seen to avoid re-entry."""
        try:
            with _trades_conn() as conn:
                rows = conn.execute(
                    "SELECT signal_id FROM trades WHERE trade_date = ?",
                    (str(self.today),)
                ).fetchall()
            self._seen_signal_ids = {r["signal_id"] for r in rows}
            logger.info("Loaded {} already-traded signals for today.", len(self._seen_signal_ids))
        except sqlite3.Error:
            pass

    # ---- signal fetching ---------------------------------------------------

    def _fetch_new_signals(self) -> list[dict]:
        """Poll signals.db for new DIRECT_CALL BUY signals not yet traded today."""
        try:
            conn = sqlite3.connect(SIGNALS_DB, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Check if signal_type column exists
            cols = [r[1] for r in conn.execute("PRAGMA table_info(signals);").fetchall()]
            has_signal_type = "signal_type" in cols

            if has_signal_type:
                rows = conn.execute("""
                    SELECT message_id, ticker, action, signal_type, confidence,
                           channel, timestamp, entry_price
                    FROM signals
                    WHERE action IN ('BUY', 'Buy', 'buy')
                      AND signal_type = 'DIRECT_CALL'
                      AND confidence != 'LOW'
                      AND ticker IS NOT NULL
                    ORDER BY timestamp DESC
                    LIMIT 100
                """).fetchall()
            else:
                # Fallback: no signal_type yet — pick any BUY with HIGH/MEDIUM confidence
                logger.warning("signal_type column not found — using fallback query (all BUY signals)")
                rows = conn.execute("""
                    SELECT message_id, ticker, action, confidence,
                           channel, timestamp, entry_price,
                           NULL as signal_type
                    FROM signals
                    WHERE action IN ('BUY', 'Buy', 'buy')
                      AND confidence IN ('HIGH', 'MEDIUM')
                      AND ticker IS NOT NULL
                    ORDER BY timestamp DESC
                    LIMIT 100
                """).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error("Error reading signals.db: {}", e)
            return []

    # ---- entry -------------------------------------------------------------

    def _can_enter(self, signal: dict) -> tuple[bool, str]:
        """Gate check — returns (ok, reason_if_not)."""
        if not self._is_market_hours():
            return False, "outside market hours"
        if self.daily_pnl <= -MAX_DAILY_LOSS:
            return False, f"daily loss limit hit (₹{self.daily_pnl:.0f})"
        if len(self.open_positions) >= MAX_OPEN_POSITIONS:
            return False, f"max positions ({MAX_OPEN_POSITIONS}) open"
        ticker = signal["ticker"].upper()
        if ticker in self.open_positions:
            return False, "already in this ticker"
        if signal["message_id"] in self._seen_signal_ids:
            return False, "already traded this signal"
        return True, ""

    async def _try_enter(self, signal: dict) -> None:
        ticker = signal["ticker"].upper()
        ok, reason = self._can_enter(signal)
        if not ok:
            logger.debug("Skip {}: {}", ticker, reason)
            return

        quote = await get_quote(ticker)
        ltp = quote.get("ltp")
        volume = quote.get("volume") or 0

        if ltp is None:
            logger.warning("Cannot get LTP for {} — skipping signal.", ticker)
            return

        if volume < MIN_VOLUME:
            logger.info("Skip {} — volume {} < min {}", ticker, volume, MIN_VOLUME)
            return

        quantity = max(1, int(CAPITAL_PER_TRADE // ltp))
        actual_capital = quantity * ltp

        pos = Position(
            signal_id=signal["message_id"],
            ticker=ticker,
            entry_price=ltp,
            quantity=quantity,
            capital=actual_capital,
            entry_time=self._now_ist(),
            signal_type=signal.get("signal_type") or "UNKNOWN",
            confidence=signal.get("confidence") or "MEDIUM",
            channel=signal.get("channel") or "",
            signal_ts=signal.get("timestamp") or "",
        )
        self.open_positions[ticker] = pos
        self._seen_signal_ids.add(signal["message_id"])

        logger.info(
            "📈 PAPER ENTER | {} | ₹{:.2f} x {} = ₹{:.0f} | TP: ₹{:.2f} | SL: ₹{:.2f} | Source: {}",
            ticker, ltp, quantity, actual_capital, pos.tp_price, pos.sl_price,
            quote.get("source", "?")
        )

    # ---- exit --------------------------------------------------------------

    async def _monitor_exits(self) -> None:
        now = self._now_ist()
        force_time_exit = now.time() >= TIME_EXIT_IST

        for ticker in list(self.open_positions.keys()):
            pos = self.open_positions[ticker]
            exit_reason: Optional[str] = None

            if force_time_exit:
                exit_reason = "TIME"
            else:
                ltp = await get_ltp(ticker)
                if ltp is None:
                    continue

                unrealised_pnl_pct = pos.pnl_pct_at(ltp)

                if ltp >= pos.tp_price:
                    exit_reason = "TP"
                elif ltp <= pos.sl_price:
                    exit_reason = "SL"

                if exit_reason is None:
                    logger.debug(
                        "{} | LTP ₹{:.2f} | P&L {:.2f}% | TP ₹{:.2f} | SL ₹{:.2f}",
                        ticker, ltp, unrealised_pnl_pct * 100, pos.tp_price, pos.sl_price
                    )
                    continue  # still holding

                # Use the ltp we just fetched for exit price
                exit_price = ltp

            if exit_reason:
                if exit_reason == "TIME":
                    exit_price = await get_ltp(ticker) or pos.entry_price

                await self._close_position(pos, exit_price, exit_reason)

    async def _close_position(self, pos: Position, exit_price: float, reason: str) -> None:
        pnl = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        self.daily_pnl += pnl

        # Approximate brokerage: ₹20 each side + 0.025% STT on sell
        brokerage = 40 + (exit_price * pos.quantity * 0.00025)
        net_pnl = pnl - brokerage

        emoji = "✅" if pnl > 0 else "❌"
        logger.info(
            "{} PAPER EXIT | {} | Entry ₹{:.2f} → Exit ₹{:.2f} | "
            "Raw P&L ₹{:.2f} ({:+.2f}%) | After costs ₹{:.2f} | Reason: {}",
            emoji, pos.ticker, pos.entry_price, exit_price,
            pnl, pnl_pct, net_pnl, reason
        )

        # Log to trades.db
        with _trades_conn() as conn:
            conn.execute("""
                INSERT INTO trades (
                    signal_id, ticker, action, signal_type, confidence,
                    channel, signal_ts, entry_price, quantity, capital,
                    entry_time, exit_price, exit_time, pnl, pnl_pct,
                    exit_reason, trade_date, is_paper
                ) VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                pos.signal_id, pos.ticker, pos.signal_type, pos.confidence,
                pos.channel, pos.signal_ts, pos.entry_price, pos.quantity,
                pos.capital, pos.entry_time.isoformat(), exit_price,
                self._now_ist().isoformat(), net_pnl, pnl_pct,
                reason, str(self.today)
            ))

        del self.open_positions[pos.ticker]

    # ---- main loop ---------------------------------------------------------

    async def run(self) -> None:
        init_trades_db()
        self._load_seen_signals()
        logger.info(
            "Paper trader started | Capital/trade: ₹{} | TP: +{}% | SL: -{}% | Exit by: {}",
            CAPITAL_PER_TRADE, TAKE_PROFIT_PCT * 100,
            STOP_LOSS_PCT * 100, TIME_EXIT_IST.strftime("%H:%M")
        )

        try:
            while True:
                self._reset_if_new_day()
                now = self._now_ist()

                if not self._is_market_hours():
                    next_open = datetime.combine(now.date(), MARKET_OPEN_IST, tzinfo=IST)
                    if now.time() > MARKET_CLOSE_IST:
                        next_open += timedelta(days=1)
                    wait_secs = (next_open - now).total_seconds()
                    logger.info(
                        "Market closed. Daily P&L: ₹{:.2f}. Next open in {:.0f} min.",
                        self.daily_pnl, wait_secs / 60
                    )
                    await asyncio.sleep(min(wait_secs, 300))
                    continue

                # Check exits first (existing positions)
                if self.open_positions:
                    await self._monitor_exits()

                # Check for new signals
                signals = self._fetch_new_signals()
                for signal in signals:
                    await self._try_enter(signal)

                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except asyncio.CancelledError:
            logger.info("Paper trader shutting down…")
        finally:
            await close_session()
            self._print_summary()

    def _print_summary(self) -> None:
        try:
            with _trades_conn() as conn:
                rows = conn.execute("""
                    SELECT ticker, entry_price, exit_price, pnl, pnl_pct,
                           exit_reason, entry_time, exit_time, signal_type
                    FROM trades WHERE trade_date = ? AND is_paper = 1
                    ORDER BY entry_time
                """, (str(self.today),)).fetchall()

            if not rows:
                logger.info("No trades today.")
                return

            print("\n" + "=" * 70)
            print(f"  PAPER TRADING SUMMARY — {self.today}")
            print("=" * 70)
            print(f"  {'TICKER':<12} {'ENTRY':>9} {'EXIT':>9} {'P&L (₹)':>10} {'%':>7} {'REASON':<8} {'TYPE'}")
            print("-" * 70)
            total_pnl = 0.0
            wins = losses = 0
            for r in rows:
                pnl = r["pnl"] or 0
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1
                print(
                    f"  {r['ticker']:<12} ₹{r['entry_price']:>8.2f} ₹{r['exit_price']:>8.2f} "
                    f"  ₹{pnl:>8.2f} {(r['pnl_pct'] or 0):>+6.2f}%  {r['exit_reason']:<8} {r['signal_type'] or '-'}"
                )
            print("-" * 70)
            win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
            print(f"  Total: {wins}W / {losses}L  |  Win rate: {win_rate:.0f}%  |  Net P&L: ₹{total_pnl:+.2f}")
            print("=" * 70 + "\n")
        except Exception as e:
            logger.error("Summary failed: {}", e)


# ---------------------------------------------------------------------------
# P&L summary command
# ---------------------------------------------------------------------------

def show_summary(days: int = 7) -> None:
    try:
        with _trades_conn() as conn:
            rows = conn.execute("""
                SELECT trade_date,
                       COUNT(*) as trades,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                       ROUND(SUM(pnl), 2) as total_pnl,
                       ROUND(AVG(pnl_pct), 2) as avg_pct
                FROM trades
                WHERE is_paper = 1
                GROUP BY trade_date
                ORDER BY trade_date DESC
                LIMIT ?
            """, (days,)).fetchall()

        if not rows:
            print("No paper trades yet.")
            return

        print("\n" + "=" * 60)
        print("  PAPER TRADE HISTORY")
        print("=" * 60)
        print(f"  {'DATE':<12} {'TRADES':>6} {'W':>4} {'L':>4} {'NET P&L':>10} {'AVG%':>7}")
        print("-" * 60)
        for r in rows:
            print(
                f"  {r['trade_date']:<12} {r['trades']:>6} {r['wins']:>4} {r['losses']:>4} "
                f"  ₹{r['total_pnl']:>8.2f} {r['avg_pct']:>+6.2f}%"
            )
        print("=" * 60 + "\n")
    except sqlite3.OperationalError:
        print("trades.db not found — run paper trader first.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trader — signal validation")
    parser.add_argument("--summary", action="store_true",
                        help="Show P&L summary and exit")
    parser.add_argument("--days", type=int, default=7,
                        help="Days of history for --summary (default: 7)")
    args = parser.parse_args()

    if args.summary:
        show_summary(args.days)
        return

    trader = PaperTrader()
    try:
        asyncio.run(trader.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
