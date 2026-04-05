"""
processing/database.py — SQLite schema, insert, query, and deduplication logic.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Generator, Optional

from loguru import logger

from utils import now_iso8601

DB_PATH = "signals.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT    UNIQUE NOT NULL,
    channel      TEXT,
    timestamp    TEXT,
    ticker       TEXT,
    action       TEXT,
    entry_price  REAL,
    target_price REAL,
    stop_loss    REAL,
    sentiment    TEXT,
    confidence   TEXT,
    timeframe    TEXT,
    signal_type  TEXT,
    summary      TEXT,
    raw_message  TEXT,
    message_type TEXT,
    processed_at TEXT
);
"""

_CREATE_IDX_TICKER_SQL = "CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals (ticker);"
_CREATE_IDX_TIMESTAMP_SQL = "CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals (timestamp);"
_CREATE_IDX_TYPE_SQL = "CREATE INDEX IF NOT EXISTS idx_signals_message_type ON signals (message_type);"
_ADD_COLUMN_SQL = "ALTER TABLE signals ADD COLUMN message_type TEXT;"

_INSERT_SQL = """
INSERT OR IGNORE INTO signals (
    message_id, channel, timestamp, ticker, action,
    entry_price, target_price, stop_loss, sentiment,
    confidence, timeframe, signal_type, summary, raw_message,
    message_type, processed_at
) VALUES (
    :message_id, :channel, :timestamp, :ticker, :action,
    :entry_price, :target_price, :stop_loss, :sentiment,
    :confidence, :timeframe, :signal_type, :summary, :raw_message,
    :message_type, :processed_at
);
"""


@contextmanager
def get_connection(db_path: str = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path, check_same_thread=False)
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


def _ensure_message_type_column(conn: sqlite3.Connection) -> None:
    cols = [row[1] for row in conn.execute("PRAGMA table_info(signals);").fetchall()]
    if "message_type" not in cols:
        conn.execute(_ADD_COLUMN_SQL)
        logger.info("Migrated DB: added message_type column.")
    if "signal_type" not in cols:
        conn.execute("ALTER TABLE signals ADD COLUMN signal_type TEXT;")
        logger.info("Migrated DB: added signal_type column.")


def init_db(db_path: str = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(_CREATE_TABLE_SQL)
        _ensure_message_type_column(conn)
        conn.execute(_CREATE_IDX_TICKER_SQL)
        conn.execute(_CREATE_IDX_TIMESTAMP_SQL)
        conn.execute(_CREATE_IDX_TYPE_SQL)
    logger.info("Database initialised at {}", db_path)


# Text fields that must be a plain string (not a list) for SQLite.
_TEXT_FIELDS = (
    "message_id", "channel", "timestamp", "ticker", "action",
    "sentiment", "confidence", "timeframe", "signal_type",
    "summary", "raw_message", "message_type", "processed_at",
)


def _coerce_signal(signal: dict[str, Any]) -> dict[str, Any]:
    # Coerce numeric fields.
    for field in ("entry_price", "target_price", "stop_loss"):
        raw = signal.get(field)
        if raw is not None:
            try:
                signal[field] = float(raw)
            except (TypeError, ValueError):
                signal[field] = None

    # Coerce text fields — LLaMA sometimes returns a list instead of a string.
    for field in _TEXT_FIELDS:
        val = signal.get(field)
        if isinstance(val, list):
            # Join non-None elements; use None if the list is empty.
            signal[field] = ", ".join(str(v) for v in val if v is not None) or None

    signal.setdefault("processed_at", now_iso8601())
    signal.setdefault("message_type", "text")
    signal.setdefault("signal_type", "GENERAL")
    return signal


def insert_signal(signal: dict[str, Any], db_path: str = DB_PATH) -> bool:
    signal = _coerce_signal(signal)
    try:
        with get_connection(db_path) as conn:
            cursor = conn.execute(_INSERT_SQL, signal)
            inserted = cursor.rowcount > 0
            if inserted:
                logger.debug("Inserted signal message_id={} ticker={} type={}",
                             signal.get("message_id"), signal.get("ticker"), signal.get("message_type"))
            else:
                logger.debug("Skipped duplicate message_id={}", signal.get("message_id"))
            return inserted
    except sqlite3.Error as exc:
        logger.error("DB insert error for message_id={}: {}", signal.get("message_id"), exc)
        return False


def _is_actionable(signal: dict[str, Any]) -> bool:
    """
    Return True if the signal has at least a ticker OR an action.
    Signals with both null are almost always promos or filler — skip them.
    """
    return bool(signal.get("ticker") or signal.get("action"))


def bulk_insert_signals(
    signals: list[dict[str, Any]], db_path: str = DB_PATH
) -> tuple[int, int]:
    """
    Insert signals one at a time, each in its own transaction.
    - Non-actionable signals (no ticker, no action) are silently skipped.
    - A single malformed record cannot wipe out the rest.
    """
    inserted = skipped = 0
    for signal in signals:
        coerced = _coerce_signal(signal)
        if not _is_actionable(coerced):
            logger.debug(
                "Skipping non-actionable signal message_id={}", coerced.get("message_id")
            )
            skipped += 1
            continue
        try:
            with get_connection(db_path) as conn:
                cursor = conn.execute(_INSERT_SQL, coerced)
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
        except sqlite3.IntegrityError:
            skipped += 1
        except sqlite3.Error as exc:
            logger.error(
                "DB insert error for message_id={}: {}",
                coerced.get("message_id"), exc,
            )
            skipped += 1

    logger.info("Bulk insert: {} inserted, {} skipped", inserted, skipped)
    return inserted, skipped


def exists(message_id: str, db_path: str = DB_PATH) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM signals WHERE message_id = ? LIMIT 1;", (message_id,)
        ).fetchone()
        return row is not None


def query_signals(
    ticker: Optional[str] = None,
    message_type: Optional[str] = None,
    limit: int = 100,
    db_path: str = DB_PATH,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if ticker:
        clauses.append("ticker = ?")
        params.append(ticker.upper())
    if message_type:
        clauses.append("message_type = ?")
        params.append(message_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with get_connection(db_path) as conn:
        return conn.execute(
            f"SELECT * FROM signals {where} ORDER BY timestamp DESC LIMIT ?;", params
        ).fetchall()
