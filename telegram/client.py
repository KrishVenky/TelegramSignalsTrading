"""
telegram/client.py — Telethon setup, session management, and channel utilities.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from loguru import logger
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat

load_dotenv()

_API_ID: int = int(os.environ["TELEGRAM_API_ID"])
_API_HASH: str = os.environ["TELEGRAM_API_HASH"]
_PHONE: str = os.environ["TELEGRAM_PHONE"]

SESSION_FILE = "telegram.session"

_client: TelegramClient | None = None


def _load_session_string() -> str:
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


def _save_session_string(session_str: str) -> None:
    with open(SESSION_FILE, "w", encoding="utf-8") as fh:
        fh.write(session_str)
    logger.info("Session string saved to {}", SESSION_FILE)


async def get_client() -> TelegramClient:
    """
    Return the authenticated TelegramClient singleton.
    Prompts for OTP on first run, reuses session on subsequent runs.
    """
    global _client

    if _client is not None and _client.is_connected():
        return _client

    session_str = _load_session_string()
    client = TelegramClient(StringSession(session_str), _API_ID, _API_HASH)

    await client.connect()

    if not await client.is_user_authorized():
        logger.info("Not authorised — starting phone authentication for {}", _PHONE)
        await client.send_code_request(_PHONE)

        try:
            otp_code = input("Enter the OTP code Telegram sent to your phone: ").strip()
            await client.sign_in(_PHONE, otp_code)
        except SessionPasswordNeededError:
            password = input("Two-step verification password: ").strip()
            await client.sign_in(password=password)

        logger.info("Authentication successful.")

    _save_session_string(client.session.save())

    _client = client
    logger.info("TelegramClient ready (user: {})", await client.get_me())
    return _client


async def resolve_channel(
    client: TelegramClient, identifier: str
) -> Channel | Chat:
    """Resolve a channel username or invite link to a Telethon entity."""
    try:
        entity = await client.get_entity(identifier)
    except Exception as exc:
        raise ValueError(f"Could not resolve channel {identifier!r}: {exc}") from exc

    if not isinstance(entity, (Channel, Chat)):
        raise ValueError(
            f"{identifier!r} resolved to {type(entity).__name__}, expected Channel or Chat."
        )
    return entity


async def disconnect_client() -> None:
    global _client
    if _client is not None and _client.is_connected():
        await _client.disconnect()
        logger.info("TelegramClient disconnected.")
    _client = None
