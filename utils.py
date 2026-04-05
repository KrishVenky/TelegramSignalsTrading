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


_INVALID_UNICODE_ESCAPE_RE = re.compile(r"\\u\{[0-9A-Fa-f]+\}")


def _clean_bad_unicode_escapes(text: str) -> str:
    """
    LLaMA Vision emits JS-style unicode escapes (e.g. backslash-u{1F4XX})
    which are invalid JSON. Replace them so json.loads does not choke.
    """
    return _INVALID_UNICODE_ESCAPE_RE.sub("\\u00ff", text)


def extract_json_from_response(text: str) -> str:
    """
    Robust JSON extractor for LLM responses that may:
      1. Wrap JSON in markdown fences
      2. Preface the JSON with explanatory prose
      3. Contain invalid JS-style unicode escapes

    Returns the best-effort extracted JSON string.
    """
    # Step 1: strip markdown code fences
    text = strip_json_fences(text)

    # Step 2: fix invalid unicode escapes before any parsing attempt
    text = _clean_bad_unicode_escapes(text)

    # Step 3: try to find JSON even when LLaMA prefaced it with prose.
    # Find the first { or [ and the matching closing bracket.
    for open_char, close_char in (("{" , "}"), ("[", "]")):
        start = text.find(open_char)
        if start == -1:
            continue
        # Walk from end to find the last matching close bracket
        end = text.rfind(close_char)
        if end != -1 and end > start:
            candidate = text[start:end + 1]
            return candidate

    return text


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
# Signal type pre-classifier  (Layer 0 — fast regex, runs before/after LLM)
# ---------------------------------------------------------------------------
# Only DIRECT_CALL signals get traded. All others stored for analysis only.

_DIRECT_CALL_RE = re.compile(
    r"(?:\U0001F525|\U0001F680|\U0001F4A5)"   # fire / rocket / boom emojis
    r"|(?:buy|entry|enter|accumulate|add)\s+now"
    r"|BUY\s+NOW"
    r"|\burgent\b"
    r"|\bbreakout\s+now\b",
    re.IGNORECASE,
)

_BROKER_CALL_RE = re.compile(
    r"jefferies|goldman\s+sachs|morgan\s+stanley|jp\s+morgan"
    r"|citibank|nomura|macquarie|kotak\s+(?:securities|institutional)"
    r"|motilal|edelweiss|\banalyst\b|\bprice\s+target\b|\bPT\s*[0-9]"
    r"|\b(?:initiates?|upgrades?|downgrades?|maintains?)\s+(?:with|at|coverage)?",
    re.IGNORECASE,
)

_RECAP_RE = re.compile(
    r"gave\s+\d+\s*%|up\s+\d+\s*%\s+from\s+our|booked\s+profit"
    r"|target\s+(?:1|2|3|achieved|hit|met)\b"
    r"|\bfrom\s+our\s+call\b|\bcall\s+given\b"
    r"|previous\s+call|yesterday(?:'s)?\s+call",
    re.IGNORECASE,
)

_CHART_SETUP_RE = re.compile(
    r"breakout|breakdown|cup\s+and\s+handle|head\s+and\s+shoulders"
    r"|support\s+zone|resistance\s+zone|\bBO\s+zone\b|double\s+(?:top|bottom)"
    r"|\bconsolidati(?:on|ng)\b|bull\s+flag|bear\s+flag|ascending\s+triangle"
    r"|\bwatch\s+list\b|\bon\s+radar\b|\bmonitoring\b",
    re.IGNORECASE,
)


def classify_signal_type(text: str) -> str:
    """
    Fast regex pre-classification. Returns one of:
      DIRECT_CALL  — urgent buy now, fire emojis, URGENT keyword
      BROKER_CALL  — analyst/brokerage recommendation with PT
      CHART_SETUP  — technical analysis setup, no urgency
      RECAP        — past performance recap
      GENERAL      — market commentary, no specific actionable call

    Applied AFTER LLM extraction as an override/supplement.
    """
    if not text:
        return "GENERAL"
    if _DIRECT_CALL_RE.search(text):
        return "DIRECT_CALL"
    if _BROKER_CALL_RE.search(text):
        return "BROKER_CALL"
    if _RECAP_RE.search(text):
        return "RECAP"
    if _CHART_SETUP_RE.search(text):
        return "CHART_SETUP"
    return "GENERAL"


# ---------------------------------------------------------------------------
# Message ID helper
# ---------------------------------------------------------------------------

def make_message_id(channel: str, message_id: int) -> str:
    """Build a globally unique message identifier: channel:msg_id."""
    return f"{channel.lstrip('@')}:{message_id}"

