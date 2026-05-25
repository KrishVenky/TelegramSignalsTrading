"""
kite/client.py — KiteConnect auth and session management.

Token stored in kite_token.json (gitignored). Refreshed daily via the dashboard
login button — Zerodha requires a fresh session every midnight.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger

_TOKEN_FILE = Path(__file__).parent.parent / "kite_token.json"
_kite_instance = None   # KiteConnect, lazily initialised


def _api_key() -> str:
    from config import KITE_API_KEY
    if not KITE_API_KEY:
        raise ValueError("KITE_API_KEY not set in .env")
    return KITE_API_KEY


def get_kite():
    """Return a configured KiteConnect instance (cached per process)."""
    global _kite_instance
    if _kite_instance is None:
        from kiteconnect import KiteConnect
        _kite_instance = KiteConnect(api_key=_api_key())
        token = load_token()
        if token:
            _kite_instance.set_access_token(token)
            logger.debug("Kite: loaded stored access token.")
    return _kite_instance


def get_login_url() -> str:
    """Return the Zerodha OAuth login URL."""
    return get_kite().login_url()


def complete_login(request_token: str) -> str:
    """
    Exchange a request_token (from OAuth redirect) for a persistent access_token.
    Saves the token to kite_token.json. Call once after each daily login.
    """
    from config import KITE_API_SECRET
    kite = get_kite()
    data = kite.generate_session(request_token, api_secret=KITE_API_SECRET)
    access_token: str = data["access_token"]
    kite.set_access_token(access_token)
    save_token(access_token)
    logger.info("Kite: login complete — access token saved.")
    return access_token


def save_token(token: str) -> None:
    _TOKEN_FILE.write_text(json.dumps({"access_token": token}), encoding="utf-8")


def load_token() -> Optional[str]:
    try:
        if _TOKEN_FILE.exists():
            return json.loads(_TOKEN_FILE.read_text(encoding="utf-8")).get("access_token")
    except Exception:
        pass
    return None


def is_connected() -> bool:
    """True if a stored token exists (doesn't make a live API call)."""
    return bool(load_token())


def get_ltp(ticker: str) -> Optional[float]:
    """Fetch Last Traded Price for a NSE symbol. Returns None on any failure."""
    try:
        result = get_kite().ltp(f"NSE:{ticker}")
        price = result.get(f"NSE:{ticker}", {}).get("last_price")
        return float(price) if price else None
    except Exception as exc:
        logger.warning("Kite LTP fetch failed for {}: {}", ticker, exc)
        return None


def get_nifty_ltp() -> Optional[float]:
    """Fetch NIFTY 50 LTP for market sentiment display."""
    try:
        result = get_kite().ltp("NSE:NIFTY 50")
        return float(result["NSE:NIFTY 50"]["last_price"])
    except Exception:
        return None
