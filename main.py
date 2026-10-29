"""WEEX Telegram signal bot foundation: Persian controls, persistent stats, chart alerts.
Strategy scanning is deliberately not enabled until the original Pine source is ported and validated.
No exchange order placement is implemented.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

import httpx
from dotenv import load_dotenv

from storage import Store
from charting import render_signal_chart

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signal-bot")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"

    async def send(self, message: str, chat_id: str | None = None) -> None:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(f"{self.base}/sendMessage",
                json={"chat_id": chat_id or self.chat_id, "text": message})
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram rejected message: {data!r}")

    async def send_photo(self, path: str, caption: str, chat_id: str | None = None) -> None:
        async with httpx.AsyncClient(timeout=45) as client:
            with open(path, "rb") as photo:
                response = await client.post(
                    f"{self.base}/sendPhoto",
                    data={"chat_id": chat_id or self.chat_id, "caption": caption[:1024]},
                    files={"photo": (Path(path).name, photo, "image/png")},
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram rejected photo: {data!r}")

    async def updates(self, offset: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=40) as client:
            response = await client.get(f"{self.base}/getUpdates",
                params={"offset": offset, "timeout": 25, "allowed_updates": '["message"]'})
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram getUpdates failed: {data!r}")
            return data.get("result", [])


def fmt_stats(stats: dict[str, Any], title: str = "کل ربات") -> str:
    return (
        f"📊 آمار {title}\n"
        f"کل سیگنال‌های ثبت‌شده: {stats['total']}\n"
        f"سیگنال‌های باز: {stats['opened']}\n"
        f"نتیجه مثبت: {stats['wins']}\n"
        f"نتیجه منفی: {stats['losses']}\n"
        f"درصد موفقیت معاملات بسته‌شده: {stats['win_rate']:.1f}%\n"
        f"مجموع نتیجه تخمینی: {stats['pnl_pct']:.2f}%"
    )


HELP = (
    "🇮🇷 راهنمای پنل ربات WEEX\n\n"
    "/pairs — نمایش جفت‌ارزهای انتخاب‌شده\n"
    "/add BTC/USDT — افزودن جفت‌ارز\n"
    "/remove BTC/USDT — حذف جفت‌ارز\n"
    "/stats — آمار کلی از زمان شروع ثبت\n"
    "/stats BTC/USDT — آمار یک جفت‌ارز\n"
    "/open — نمایش سیگنال‌های باز\n"
    "/status — وضعیت ربات\n"
    "/help — راهنما\n\n"
    "توجه: اتصال اسکنر و تولید سیگنال هنوز در حال پیاده‌سازی است؛ "
    "تا آن زمان هیچ سیگنال ساختگی ثبت نمی‌شود و معامله واقعی انجام نمی‌شود."
)


async def handle_command(text: str, chat_id: str, notifier: TelegramNotifier, store: Store,
                         allowed_chat_id: str) -> None:
    # Only the configured owner/chat can change the selected markets.
    if str(chat_id) != str(allowed_chat_id):
        log.warning("Ignoring Telegram command from unconfigured chat id.")
        return
    parts = text.strip().split()
    if not parts:
        return
    command = parts[0].split("@", 1)[0].lower()
    arg = parts[1].upper() if len(parts) > 1 else ""

    if command in ("/start", "/help"):
        await notifier.send(HELP, chat_id)
    elif command == "/pairs":
        pairs = store.list_pairs()
        await notifier.send("📌 جفت‌ارزهای انتخاب‌شده:\n" + ("\n".join(pairs) if pairs else "هنوز جفت‌ارزی انتخاب نشده است.\nمثال: /add BTC/USDT"), chat_id)
    elif command == "/add":
        if not arg or "/" not in arg:
            await notifier.send("فرمت درست: /add BTC/USDT", chat_id)
        else:
            added = store.add_pair(arg)
            await notifier.send(("✅ اضافه شد: " if added else "ℹ️ از قبل در فهرست است: ") + arg, chat_id)
    elif command == "/remove":
        if not arg or "/" not in arg:
            await notifier.send("فرمت درست: /remove BTC/USDT", chat_id)
        else:
            removed = store.remove_pair(arg)
            await notifier.send(("🗑 حذف شد: " if removed else "این جفت‌ارز در فهرست نبود: ") + arg, chat_id)
    elif command == "/stats":
        await notifier.send(fmt_stats(store.stats(arg or None), arg or "کل ربات"), chat_id)
    elif command == "/open":
        rows = store.open_signals()
        if not rows:
            await notifier.send("در حال حاضر سیگنال باز ثبت‌شده‌ای وجود ندارد.", chat_id)
        else:
            lines = ["📂 سیگنال‌های باز ثبت‌شده:"]
            for row in rows[:30]:
                lines.append(f"#{row['id']} {row['symbol']} {row['side']} | ورود: {row['entry']} | حدضرر: {row['stop_loss']} | حدسود: {row['take_profit']}")
            await notifier.send("\n".join(lines), chat_id)
    elif command == "/status":
        await notifier.send(
            "🟢 فرایند ربات فعال است.\n"
            f"شروع ثبت آمار: {store.get_setting('started_at')}\n"
            f"جفت‌ارزهای انتخاب‌شده: {len(store.list_pairs())}\n"
            "اسکن بازار: هنوز وصل نشده\nمعامله واقعی: غیرفعال",
            chat_id,
        )
    else:
        await notifier.send("دستور شناخته نشد. /help را بفرست.", chat_id)


async def send_signal_alert(
    notifier: TelegramNotifier, store: Store, *,
    symbol: str, side: str, entry: float, candles: Sequence[Sequence[float]],
    stop_loss: float | None = None, take_profit: float | None = None,
    order_blocks: Sequence[dict[str, Any]] = (), fvgs: Sequence[dict[str, Any]] = (),
    choch_price: float | None = None, timeframe: str = "15m",
) -> int:
    """Called by the validated strategy adapter when a genuinely new signal appears."""
    path = str(Path(tempfile.gettempdir()) / f"weex_signal_{symbol.replace('/', '_')}_{side.lower()}.png")
    render_signal_chart(candles, symbol, timeframe, side, entry, stop_loss, take_profit,
                        order_blocks, fvgs, choch_price, path)
    signal_id = store.record_signal(symbol, side, entry, stop_loss, take_profit, path)
    caption = (
        f"📣 سیگنال جدید WEEX | {symbol}\n"
        f"جهت: {'خرید 🟢' if side.upper() == 'BUY' else 'فروش 🔴'}\n"
        f"تایم‌فریم: {timeframe}\nورود: {entry}\n"
        f"حد ضرر: {stop_loss if stop_loss is not None else 'تعریف نشده'}\n"
        f"حد سود: {take_profit if take_profit is not None else 'تعریف نشده'}\n"
        f"شماره ثبت: #{signal_id}\n"
        "تصویر: کندل‌ها و نواحی‌ای که استراتژی تأیید کرده است.\n"
        "این پیام سیگنال است؛ سفارش واقعی ارسال نشده."
    )
    await notifier.send_photo(path, caption)
    return signal_id


async def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    timeframe = os.getenv("TIMEFRAME", "15m").strip()
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Render environment variables.")
    if timeframe != "15m":
        raise RuntimeError("This project is configured for the requested 15m timeframe.")

    store = Store()
    notifier = TelegramNotifier(token, chat_id)
    log.info("Bot online; signals-only; timeframe=%s", timeframe)
    await notifier.send(
        "ربات تلگرام WEEX روشن شد.\n"
        "پنل فارسی و ذخیره آمار فعال است.\n"
        "شروع ثبت آمار: " + store.get_setting("started_at") + "\n"
        "⚠️ اسکن بازار و تولید سیگنال هنوز متصل نشده است؛ پیام ساختگی ارسال نمی‌شود."
    )
    offset = int(store.get_setting("telegram_offset", "0"))
    while True:
        try:
            updates = await notifier.updates(offset)
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                store.set_setting("telegram_offset", str(offset))
                message = update.get("message") or {}
                incoming = message.get("text", "")
                source_chat = str((message.get("chat") or {}).get("id", ""))
                if incoming.startswith("/"):
                    await handle_command(incoming, source_chat, notifier, store, chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram polling error; retrying shortly.")
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutdown requested.")
