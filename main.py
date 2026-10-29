"""Signals-only WEEX Telegram bot scaffold.

No order-placement functions or exchange trading credentials are used here.
The strategy and market-data adapter are intentionally separated so they can
be validated against the supplied Pine Script before production alerts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("signal-bot")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    async def send(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                json={"chat_id": self.chat_id, "text": message},
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram API rejected message: {data!r}")


async def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    interval = max(15, int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
    timeframe = os.getenv("TIMEFRAME", "15m").strip()

    if not token or not chat_id:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Render environment variables."
        )
    if timeframe != "15m":
        raise RuntimeError("This project is configured for the requested 15m timeframe.")

    notifier = TelegramNotifier(token, chat_id)
    log.info("Bot started in SIGNALS-ONLY mode; timeframe=%s", timeframe)
    await notifier.send(
        "ربات سیگنال WEEX راه‌اندازی شد.\n"
        "وضعیت: فقط ارسال پیام؛ معامله واقعی غیرفعال است.\n"
        "اتصال بازار و بررسی استراتژی هنوز در مرحله آماده‌سازی است."
    )

    # The scanner will be connected here after the WEEX public market adapter
    # and Pine-equivalent strategy implementation have been tested.
    while True:
        log.info("Scaffold is alive; market scanner is not connected yet.")
        await asyncio.sleep(interval)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
