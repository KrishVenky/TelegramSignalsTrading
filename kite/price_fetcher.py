"""
kite/price_fetcher.py — Real-time & near-real-time price data for Indian equities.

Uses NSEpy / NSE India unofficial API for live prices (no Kite subscription needed).
Falls back to yfinance on failure.

NSE data is delayed ~1-5 min on their public feed but is free and sufficient for
paper trading entry/exit simulation.

Usage:
    price = await get_ltp("RELIANCE")      # Last Traded Price
    quote = await get_quote("RELIANCE")    # Full OHLCV + volume
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

import aiohttp
from loguru import logger

# ---------------------------------------------------------------------------
# NSE unofficial JSON feed — real-time (no auth needed)
# ---------------------------------------------------------------------------

_NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol={symbol}"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.nseindia.com",
    "Connection": "keep-alive",
}

# NSE requires a session cookie. We fetch the homepage once to seed the session.
_nse_session: Optional[aiohttp.ClientSession] = None


async def _get_nse_session() -> aiohttp.ClientSession:
    global _nse_session
    if _nse_session is None or _nse_session.closed:
        _nse_session = aiohttp.ClientSession(headers=_NSE_HEADERS)
        # Seed NSE cookie — wait briefly so the cookie is set before the data request
        try:
            async with _nse_session.get(
                "https://www.nseindia.com", timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                await resp.read()
            await asyncio.sleep(1)   # NSE needs ~1s between homepage hit and API call
        except Exception as e:
            logger.warning("NSE session seed failed: {}", e)
    return _nse_session


async def _nse_quote(symbol: str) -> Optional[dict]:
    """Fetch real-time quote from NSE India. Returns raw dict or None."""
    symbol = symbol.upper().strip()
    url = _NSE_QUOTE_URL.format(symbol=symbol)
    try:
        sess = await _get_nse_session()
        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                return data
            logger.warning("NSE quote {} returned HTTP {}", symbol, resp.status)
    except Exception as e:
        logger.warning("NSE quote fetch failed for {}: {}", symbol, e)
    return None


def _extract_ltp(nse_data: dict) -> Optional[float]:
    """Pull lastPrice from NSE response."""
    try:
        price_info = nse_data.get("priceInfo", {})
        ltp = price_info.get("lastPrice")
        if ltp is not None:
            return float(ltp)
    except Exception:
        pass
    return None


def _extract_volume(nse_data: dict) -> Optional[int]:
    """Pull totalTradedVolume from NSE response."""
    try:
        mkt = nse_data.get("marketDeptOrderBook", {})
        vol = mkt.get("tradeInfo", {}).get("totalTradedVolume")
        if vol is not None:
            return int(vol)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# yfinance fallback (async via run_in_executor)
# ---------------------------------------------------------------------------

async def _yfinance_ltp(symbol: str) -> Optional[float]:
    """Fallback: yfinance for NSE symbols. ~1-5 min delayed."""
    loop = asyncio.get_event_loop()

    def _sync_fetch():
        try:
            import yfinance as yf  # type: ignore
            ticker = yf.Ticker(f"{symbol}.NS")
            hist = ticker.fast_info
            return float(hist.last_price)
        except Exception as e:
            logger.warning("yfinance fallback failed for {}: {}", symbol, e)
            return None

    return await loop.run_in_executor(None, _sync_fetch)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_ltp(symbol: str) -> Optional[float]:
    """
    Return Last Traded Price for an NSE symbol.
    Tries NSE real-time feed first, falls back to yfinance.
    """
    symbol = symbol.upper().strip()
    # Remove .NS suffix if user passed it
    symbol = re.sub(r"\.NS$", "", symbol, flags=re.IGNORECASE)

    data = await _nse_quote(symbol)
    if data:
        ltp = _extract_ltp(data)
        if ltp:
            logger.debug("NSE LTP for {}: {}", symbol, ltp)
            return ltp

    logger.info("NSE failed for {}, trying yfinance fallback…", symbol)
    return await _yfinance_ltp(symbol)


async def get_quote(symbol: str) -> dict:
    """
    Return a standardised quote dict:
    {symbol, ltp, volume, open, high, low, prev_close, change_pct, source}
    All fields may be None if unavailable.
    """
    symbol = symbol.upper().strip()
    symbol = re.sub(r"\.NS$", "", symbol, flags=re.IGNORECASE)

    result: dict = {
        "symbol": symbol,
        "ltp": None,
        "volume": None,
        "open": None,
        "high": None,
        "low": None,
        "prev_close": None,
        "change_pct": None,
        "source": None,
    }

    data = await _nse_quote(symbol)
    if data:
        price_info = data.get("priceInfo", {})
        result.update({
            "ltp": price_info.get("lastPrice"),
            "open": price_info.get("open"),
            "high": price_info.get("intraDayHighLow", {}).get("max"),
            "low": price_info.get("intraDayHighLow", {}).get("min"),
            "prev_close": price_info.get("previousClose"),
            "change_pct": price_info.get("pChange"),
            "volume": _extract_volume(data),
            "source": "NSE",
        })
        if result["ltp"]:
            return result

    # yfinance fallback
    loop = asyncio.get_event_loop()

    def _sync_full():
        try:
            import yfinance as yf  # type: ignore
            t = yf.Ticker(f"{symbol}.NS")
            info = t.fast_info
            return {
                "ltp": float(info.last_price),
                "open": float(info.open),
                "high": float(info.day_high),
                "low": float(info.day_low),
                "prev_close": float(info.previous_close),
                "volume": int(info.three_month_average_volume or 0),
                "change_pct": None,
                "source": "yfinance",
            }
        except Exception:
            return {}

    yf_data = await loop.run_in_executor(None, _sync_full)
    result.update(yf_data)
    return result


async def close_session() -> None:
    global _nse_session
    if _nse_session and not _nse_session.closed:
        await _nse_session.close()
        _nse_session = None
