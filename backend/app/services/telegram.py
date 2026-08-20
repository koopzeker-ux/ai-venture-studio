import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_telegram_message(text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram alert skipped: bot token or chat id not configured")
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Telegram alert failed to send")
        return False
    return True
