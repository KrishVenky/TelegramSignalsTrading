"""
utils.py — Shared helper utilities.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def to_iso8601(dt: Optional[datetime]) -> Optional[str]:
    """
    Convert a datetime object to an ISO-8601 string (UTC, with 'Z' suffix).
    Returns None if *dt* is None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso8601() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return to_iso8601(datetime.now(timezone.utc))  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Text cleaning helpers
# ---------------------------------------------------------------------------

_MULTI_WHITESPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    """
    Lightly clean a Telegram message for LLM consumption.
    Collapses whitespace and removes null bytes.
    """
    text = text.replace("\x00", "")
    text = _MULTI_WHITESPACE_RE.sub(" ", text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def strip_json_fences(text: str) -> str:
    """Remove Markdown code fences (```json ... ```) from LLM responses."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    text = re.sub(r"^`(.*)`$", r"\1", text.strip(), flags=re.DOTALL)
    return text.strip()


# ---------------------------------------------------------------------------
# Promo / slop filter  (Layer 1 — runs before any LLM call)
# ---------------------------------------------------------------------------

# Keywords strongly associated with promotional / non-signal content.
_PROMO_RE = re.compile(
    r"""
    t\.me/              # Telegram invite links
    | telegram\.me/
    | (?:join|subscribe|follow)\s+(?:our|my|the|us|now|here|channel|group|link)
    | (?:free|paid)\s+(?:course|group|signals?|mentorship|training|webinar)
    | (?:enroll|register|sign\s*up)\s+now
    | (?:limited|exclusive)\s+(?:offer|seats?|slots?)
    | (?:refer(?:ral)?|affiliate)\s*(?:link|code|program)?
    | (?:dm\s+(?:me|us)|inbox\s+(?:me|us)).*(?:course|group|signal|mentorship)
    | (?:check\s+out|visit|see)\s+(?:my|our)\s+(?:channel|profile|link|website)
    | discount\s*code
    | promo\s*code
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Minimum length for a message to even be considered.
_MIN_TEXT_LENGTH = 15


def is_promo_message(text: str) -> bool:
    """
    Return True if *text* looks like a promotional / slop message that should
    be skipped entirely before reaching the LLM.

    Catches:
    - Telegram invite links (t.me/...)
    - Course / paid group promotions
    - Referral / affiliate links
    - Channel subscribe / follow requests
    - Very short messages (< 15 chars) — unlikely to contain a signal
    """
    if not text:
        return True
    stripped = text.strip()
    if len(stripped) < _MIN_TEXT_LENGTH:
        return True
    return bool(_PROMO_RE.search(stripped))


# ---------------------------------------------------------------------------
# Message ID helper
# ---------------------------------------------------------------------------

def make_message_id(channel: str, message_id: int) -> str:
    """Build a globally unique message identifier: channel:msg_id."""
    return f"{channel.lstrip('@')}:{message_id}"

