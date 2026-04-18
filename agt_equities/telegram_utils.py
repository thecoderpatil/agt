"""Synchronous Telegram send helpers — CLI-safe, no asyncio required."""
from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger("agt_bridge")

_TELEGRAM_API_BASE = "https://api.telegram.org"
_DEFAULT_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
_DEFAULT_CHAT_ID_ENV = "TELEGRAM_USER_ID"


def send_telegram_message(
    text: str,
    *,
    chat_id: str | None = None,
    parse_mode: str | None = "HTML",
    bot_token: str | None = None,
    timeout: float = 10.0,
) -> dict:
    """Send one Telegram message. Fail-open: never raises.

    Returns the Telegram API response dict, or {"ok": False, "error": ...}
    on network error or missing configuration.
    """
    token = bot_token or os.environ.get(_DEFAULT_BOT_TOKEN_ENV, "")
    cid = chat_id or os.environ.get(_DEFAULT_CHAT_ID_ENV, "")
    if not token or not cid:
        msg = "TELEGRAM_BOT_TOKEN or TELEGRAM_USER_ID not configured"
        logger.warning("send_telegram_message: %s", msg)
        return {"ok": False, "error": msg}
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload: dict = {"chat_id": cid, "text": text}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        body = resp.json()
        if not body.get("ok"):
            logger.warning("send_telegram_message: API error: %s", body)
        return body
    except Exception as exc:
        logger.warning("send_telegram_message: request failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def send_telegram_digest(
    messages: list[str],
    *,
    chat_id: str | None = None,
    parse_mode: str | None = "HTML",
    bot_token: str | None = None,
) -> list[dict]:
    """Send a list of messages with 0.3 s sleep between sends (rate-limit hygiene).

    Returns one response dict per message. Fail-open per message.
    """
    results: list[dict] = []
    for i, msg in enumerate(messages):
        results.append(
            send_telegram_message(
                msg,
                chat_id=chat_id,
                parse_mode=parse_mode,
                bot_token=bot_token,
            )
        )
        if i < len(messages) - 1:
            time.sleep(0.3)
    return results
