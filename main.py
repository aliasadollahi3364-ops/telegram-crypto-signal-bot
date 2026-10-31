"""WEEX Telegram signal bot foundation: Persian controls, persistent stats, chart alerts.
Strategy scanning is deliberately not enabled until the original Pine source is ported and validated.
No exchange order placement is implemented.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

import httpx
from dotenv import load_dotenv

from storage import Store
from charting import render_signal_chart, render_market_chart
from strategy import StrategyConfig, evaluate_latest, ob_trailing_candidate

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("signal-bot")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"

    async def send(self, message: str, chat_id: str | None = None,
                   reply_markup: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id or self.chat_id, "text": message}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(f"{self.base}/sendMessage", json=payload)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram rejected message: {data!r}")

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text[:200]},
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram rejected callback answer: {data!r}")

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
                params={"offset": offset, "timeout": 25, "allowed_updates": '["message","callback_query"]'})
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


def setting_bool(store: Store, key: str, env_name: str, default: bool) -> bool:
    """Persistent Telegram setting takes precedence over the environment default."""
    fallback = os.getenv(env_name, str(default).lower()).strip().lower() == "true"
    value = store.get_setting("user_" + key, "")
    return fallback if value == "" else value.lower() == "true"


def settings_keyboard(store: Store) -> dict[str, Any]:
    sma = setting_bool(store, "sma200", "USE_SMA200_FILTER", True)
    be = setting_bool(store, "break_even", "USE_BREAK_EVEN", True)
    trail = setting_bool(store, "trailing_stop", "USE_TRAILING_STOP", True)
    return {"inline_keyboard": [
        [{"text": f"📈 فیلتر SMA200: {'روشن ✅' if sma else 'خاموش ❌'}",
          "callback_data": "settings:toggle:sma200"}],
        [{"text": f"🟰 استاپ سر‌به‌سر: {'روشن ✅' if be else 'خاموش ❌'}",
          "callback_data": "settings:toggle:break_even"}],
        [{"text": f"🛑 استاپ شناور: {'روشن ✅' if trail else 'خاموش ❌'}",
          "callback_data": "settings:toggle:trailing_stop"}],
        [{"text": "📊 جفت‌ارزهای انتخاب‌شده", "callback_data": "settings:pairs"}],
        [{"text": "📉 نمودار BTC/USDT", "callback_data": "settings:chart"}],
    ]}


async def show_settings(chat_id: str, notifier: TelegramNotifier, store: Store) -> None:
    text = (
        "⚙️ تنظیمات دستی ربات WEEX\n\n"
        "با زدن دکمه‌ها، گزینه‌ها روشن/خاموش می‌شوند و در پایگاه داده ذخیره می‌مانند.\n"
        "تایم‌فریم اسکن فعلاً ۱۵ دقیقه است و در این نسخه قابل تغییر نیست.\n"
        "حجم معامله واقعی در این نسخه کاربردی ندارد؛ ارسال سفارش واقعی غیرفعال است."
    )
    await notifier.send(text, chat_id, settings_keyboard(store))


HELP = (
    "🇮🇷 راهنمای پنل ربات WEEX\n\n"
    "/settings — تنظیمات دستی با دکمه‌های فارسی\n"
    "/pairs — نمایش جفت‌ارزهای انتخاب‌شده\n"
    "/auto_pairs — مدیریت افزودن خودکار ارزهای برتر\n"
    "/add BTC — افزودن ارز (مثلاً BTC)\n"
    "/remove BTC — حذف ارز (مثلاً BTC)\n"
    "/stats — آمار کلی از زمان شروع ثبت\n"
    "/stats BTC/USDT — آمار یک جفت‌ارز\n"
    "/open — نمایش سیگنال‌های باز\n"
    "/status — وضعیت ربات\n"
    "/chart BTC/USDT — نمودار تازه بازار\n"
    "/help — راهنما\n\n"
    "حساب دمو داخلی: /demo | تغییر مارجین: /demo_size 5 | تغییر اهرم: /demo_leverage 5\n"
    "سیگنال‌های جدید در صورت کافی بودن مارجین، به‌صورت شبیه‌سازی‌شده وارد حساب دمو می‌شوند. "
    "هیچ سفارش واقعی ارسال نمی‌شود."
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
        if command == "/start":
            await show_settings(chat_id, notifier, store)
    elif command == "/settings":
        await show_settings(chat_id, notifier, store)
    elif command == "/pairs":
        pairs = store.list_pairs()
        await notifier.send("📌 جفت‌ارزهای انتخاب‌شده:\n" + ("\n".join(pairs) if pairs else "هنوز جفت‌ارزی انتخاب نشده است.\nمثال: /add BTC"), chat_id)
    elif command == "/auto_pairs":
        mode = arg.lower()
        if mode in ("on", "روشن"):
            store.set_setting("auto_pairs_enabled", "true")
            store.set_setting("auto_pairs_force_sync", "true")
            await notifier.send(
                "✅ افزودن خودکار ارزهای برتر روشن شد. در چرخه بعدی، فهرست WEEX به‌روزرسانی می‌شود.\n"
                f"ارزهای خودکار فعلی: {store.count_auto_pairs()} | کل ارزهای اسکن: {len(store.list_pairs())}",
                chat_id,
            )
        elif mode in ("off", "خاموش"):
            store.set_setting("auto_pairs_enabled", "false")
            await notifier.send(
                "⏸ افزودن خودکار خاموش شد؛ ارزهای فعلی حذف نمی‌شوند و اسکن آن‌ها ادامه دارد.",
                chat_id,
            )
        else:
            enabled = store.get_setting("auto_pairs_enabled", "true").lower() == "true"
            await notifier.send(
                "🤖 مدیریت انتخاب خودکار ارزها\n"
                f"وضعیت: {'روشن ✅' if enabled else 'خاموش ❌'}\n"
                f"ارزهای خودکار: {store.count_auto_pairs()}\n"
                f"کل ارزهای اسکن: {len(store.list_pairs())}\n"
                "انتخاب: ۳۰ قرارداد دائمی USDT برتر WEEX بر اساس حجم معاملات ۲۴ ساعته "
                "(حداقل ۱ میلیون USDT).\n"
                "به‌روزرسانی خودکار هر ۶ ساعت. ارزهایی که دستی اضافه کرده‌ای حفظ می‌شوند.\n"
                "روشن: /auto_pairs on | خاموش: /auto_pairs off",
                chat_id,
            )
    elif command == "/add":
        if not arg:
            await notifier.send(
                "➕ فقط نام ارز را بفرست؛ مثلاً: /add BTC\n"
                "نمونه‌های دیگر: ETH ، TRX ، SOL\n"
                "ربات خودش آن را به جفت‌ارز USDT تبدیل می‌کند.",
                chat_id,
            )
        else:
            base = arg.upper().replace("/", "").replace("-", "").replace("_", "")
            if base.endswith("USDT"):
                base = base[:-4]
            if not base or not base.isalnum():
                await notifier.send("❌ نام ارز معتبر نیست. مثال: /add BTC", chat_id)
            else:
                symbol = f"{base}/USDT"
                added = store.add_pair(symbol)
                await notifier.send(("✅ اضافه شد: " if added else "ℹ️ از قبل در فهرست است: ") + symbol, chat_id)
    elif command == "/remove":
        if not arg:
            await notifier.send(
                "➖ فقط نام ارز را بفرست؛ مثلاً: /remove BTC\n"
                "ربات آن را به جفت‌ارز USDT تبدیل می‌کند.",
                chat_id,
            )
        else:
            base = arg.upper().replace("/", "").replace("-", "").replace("_", "")
            if base.endswith("USDT"):
                base = base[:-4]
            if not base or not base.isalnum():
                await notifier.send("❌ نام ارز معتبر نیست. مثال: /remove BTC", chat_id)
            else:
                symbol = f"{base}/USDT"
                removed = store.remove_pair(symbol)
                await notifier.send(("🗑 حذف شد: " if removed else "این جفت‌ارز در فهرست نبود: ") + symbol, chat_id)
    elif command == "/demo":
        d = store.demo_summary()
        await notifier.send(
            "🧪 حساب دمو داخلی (بدون سفارش واقعی)\n"
            f"موجودی اولیه: {d['initial']:.2f}$\n"
            f"سود/ضرر قطعی: {d['realized']:+.4f}$\n"
            f"سود/ضرر شناور: {d['floating']:+.4f}$\n"
            f"ارزش کل حساب: {d['equity']:.4f}$\n"
            f"مارجین درگیر: {d['used_margin']:.2f}$\n"
            f"مارجین آزاد: {d['free_margin']:.4f}$\n"
            f"پوزیشن‌های دمو باز: {d['open_count']}\n"
            f"هر معامله: {d['margin_per_trade']:.2f}$ مارجین × {d['leverage']:.2f} اهرم = {d['notional']:.2f}$ ارزش معامله",
            chat_id)
    elif command == "/demo_size":
        if not arg:
            d = store.demo_summary()
            await notifier.send(f"مبلغ مارجین فعلی هر معامله: {d['margin_per_trade']:.2f}$\nتغییر: /demo_size 5", chat_id)
        else:
            try:
                value = float(arg)
                if not 0.5 <= value <= 100:
                    raise ValueError()
                store.set_demo_parameter("demo_margin_per_trade", value)
                await notifier.send(f"✅ مارجین هر معامله دمو روی {value:g}$ تنظیم شد. پوزیشن‌های باز قبلی تغییر نمی‌کنند.", chat_id)
            except ValueError:
                await notifier.send("❌ مقدار معتبر نیست. بازه مجاز 0.5 تا 100 دلار است.", chat_id)
    elif command == "/demo_leverage":
        if not arg:
            d = store.demo_summary()
            await notifier.send(f"اهرم فعلی دمو: {d['leverage']:g}\nتغییر: /demo_leverage 5", chat_id)
        else:
            try:
                value = float(arg)
                if not 1 <= value <= 20:
                    raise ValueError()
                store.set_demo_parameter("demo_leverage", value)
                await notifier.send(f"✅ اهرم دمو روی {value:g}x تنظیم شد. پوزیشن‌های باز قبلی تغییر نمی‌کنند.", chat_id)
            except ValueError:
                await notifier.send("❌ اهرم باید بین 1 تا 20 باشد.", chat_id)
    elif command == "/stats":
        await notifier.send(fmt_stats(store.stats(arg or None), arg or "کل ربات"), chat_id)
    elif command == "/open":
        rows = store.open_signals()
        if not rows:
            await notifier.send("در حال حاضر سیگنال باز ثبت‌شده‌ای وجود ندارد.", chat_id)
        else:
            lines = ["📂 سیگنال‌های باز ثبت‌شده:"]
            for row in rows[:30]:
                opened_at = str(row.get("created_at") or "نامشخص").replace("T", " ").replace("+00:00", " UTC")
                lines.append(
                    f"#{row['id']} {row['symbol']} {row['side']}\n"
                    f"زمان باز شدن: {opened_at}\n"
                    f"ورود: {row['entry']} | حدضرر: {row['stop_loss']} | حدسود: {row['take_profit']}"
                )
            await notifier.send("\n".join(lines), chat_id)
    elif command in ("/chart", "/live"):
        if not arg or "/" not in arg:
            await notifier.send("فرمت درست: /chart BTC/USDT", chat_id)
        else:
            from market_data import WeexMarketData
            market = WeexMarketData()
            try:
                candles = await market.fetch_ohlcv(arg, timeframe="15m", limit=300)
                candles4h = await market.fetch_ohlcv(arg, timeframe="4h", limit=300)
                safe_name = arg.replace("/", "_").replace(":", "_")
                path = str(Path(tempfile.gettempdir()) / f"weex_market_{safe_name}.png")
                import pandas as pd
                closes = pd.Series([float(c[4]) for c in candles])
                sma_series = closes.rolling(200, min_periods=200).mean()
                sma_values = [None if pd.isna(v) else float(v) for v in sma_series]
                cfg = StrategyConfig(
                    use_trend_filter=True,
                    use_sma200_filter=setting_bool(store, "sma200", "USE_SMA200_FILTER", True),
                    use_break_even=setting_bool(store, "break_even", "USE_BREAK_EVEN", True),
                    use_trailing_stop=setting_bool(store, "trailing_stop", "USE_TRAILING_STOP", True),
                )
                signal = evaluate_latest(candles, candles4h, cfg)
                order_blocks = signal.get("order_blocks", []) if signal else []
                choch_price = signal.get("choch_price") if signal else None
                await asyncio.to_thread(
                    render_market_chart, candles, arg, "15m", path,
                    sma_values=sma_values, order_blocks=order_blocks,
                    choch_price=choch_price, signal=signal,
                )
                last = float(candles[-1][4])
                caption = (f"📈 نمودار تازه WEEX | {arg}\n"
                           f"تایم‌فریم: ۱۵ دقیقه\nآخرین کندل: {last:g}\n"
                           f"SMA200: {'نمایش داده شد' if any(v is not None for v in sma_values) else 'داده کافی نیست'}\n"
                           f"سیگنال جدید روی آخرین کندل: {'بله' if signal else 'خیر'}\n"
                           "Order Block و CHoCH فقط وقتی منطق فعلی استراتژی سیگنال تأیید کند نمایش داده می‌شوند.\n"
                           "FVG نمایش داده نمی‌شود. تصویر فقط هنگام درخواست ساخته می‌شود.")
                await notifier.send_photo(path, caption, chat_id)
            except Exception as exc:
                log.exception("Unable to create market chart for %s", arg)
                await notifier.send(f"❌ دریافت نمودار {arg} ناموفق بود. نماد را بررسی کن و دوباره تلاش کن.\nجزئیات: {str(exc)[:250]}", chat_id)
            finally:
                await market.close()
    elif command == "/status":
        await notifier.send(
            "🟢 فرایند ربات فعال است.\n"
            f"شروع ثبت آمار: {store.get_setting('started_at')}\n"
            f"جفت‌ارزهای انتخاب‌شده: {len(store.list_pairs())}\n"
            f"افزودن خودکار ارزها: {'روشن' if store.get_setting('auto_pairs_enabled', 'true').lower() == 'true' else 'خاموش'} | خودکار: {store.count_auto_pairs()}\n"
            f"اسکن استراتژی: {'آزمایشی روشن' if os.getenv('SCANNER_ENABLED', 'true').lower() == 'true' else 'خاموش در تنظیمات سرویس'}\n"
            f"SMA200: {'روشن' if setting_bool(store, 'sma200', 'USE_SMA200_FILTER', True) else 'خاموش'}\n"
            f"استاپ سر‌به‌سر: {'روشن' if setting_bool(store, 'break_even', 'USE_BREAK_EVEN', True) else 'خاموش'}\n"
            f"استاپ شناور: {'روشن' if setting_bool(store, 'trailing_stop', 'USE_TRAILING_STOP', True) else 'خاموش'}\n"
            f"حساب دمو: موجودی {store.demo_summary()['equity']:.2f}$ | مارجین هر معامله {store.demo_summary()['margin_per_trade']:.2f}$ | اهرم {store.demo_summary()['leverage']:g}x\n"
            "وضعیت دمو: فعال | معامله واقعی: غیرفعال\n"
            "نمودار بازار: /chart BTC/USDT",
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
    break_even_enabled: bool = False, trailing_enabled: bool = False,
    entry_ob_timestamp: int | None = None,
) -> int:
    """Called by the validated strategy adapter when a genuinely new signal appears."""
    path = str(Path(tempfile.gettempdir()) / f"weex_signal_{symbol.replace('/', '_')}_{side.lower()}.png")
    await asyncio.to_thread(
        render_signal_chart, candles, symbol, timeframe, side, entry, stop_loss,
        take_profit, order_blocks, fvgs, choch_price, path,
    )
    signal_id = store.record_signal(
        symbol, side, entry, stop_loss, take_profit, path,
        break_even_enabled=break_even_enabled, trailing_enabled=trailing_enabled,
        entry_ob_timestamp=entry_ob_timestamp,
    )
    caption = (
        f"📣 سیگنال جدید WEEX | {symbol}\n"
        f"جهت: {'خرید 🟢' if side.upper() == 'BUY' else 'فروش 🔴'}\n"
        f"تایم‌فریم: {timeframe}\nورود: {entry}\n"
        f"حد ضرر: {stop_loss if stop_loss is not None else 'تعریف نشده'}\n"
        f"حد سود: {take_profit if take_profit is not None else 'تعریف نشده'}\n"
        f"استاپ سر‌به‌سر: {'روشن' if break_even_enabled else 'خاموش'} | استاپ شناور: {'روشن' if trailing_enabled else 'خاموش'}\n"
        f"شماره ثبت: #{signal_id}\n"
        "تصویر: کندل‌ها و نواحی‌ای که استراتژی تأیید کرده است.\n"
        "این پیام سیگنال است؛ سفارش واقعی ارسال نشده."
    )
    await notifier.send_photo(path, caption)
    return signal_id


async def notify_closed_signals(
    notifier: TelegramNotifier, store: Store, symbol: str,
    candle_high: float, candle_low: float,
) -> int:
    """Call once for each newly closed 15m candle, after the signal is already recorded."""
    closed = store.evaluate_candle(symbol, candle_high, candle_low, candle_close=None)
    for row in closed:
        pnl_pct = float(row["pnl_pct"])
        # Realized demo PnL is persisted by Store for positions actually opened in demo.
        pnl_usd = row.get("demo_pnl_usd")
        if pnl_usd is None:
            pnl_usd = 0.0
        if pnl_usd > 1e-10:
            emoji, outcome = "🟢", "سود"
        elif pnl_usd < -1e-10:
            emoji, outcome = "🔴", "ضرر"
        else:
            emoji, outcome = "🟡", "سر‌به‌سر"
        title = "حد سود فعال شد" if row["exit_label"] == "TP" else "حد ضرر فعال شد"
        await notifier.send(
            f"{emoji} {title} | {row['symbol']}\n"
            f"جهت سیگنال: {'خرید' if row['side'] == 'BUY' else 'فروش'}\n"
            f"شماره سیگنال: #{row['id']}\n"
            f"قیمت ورود: {row['entry']}\n"
            f"قیمت خروج محاسباتی: {row['exit_price']}\n"
            f"نتیجه: {outcome} | {pnl_usd:+.4f} دلار ({pnl_pct:+.3f}%)\n"
            "مبنای محاسبه دمو: ۵ دلار مارجین با اهرم ۵ (ارزش معامله ۲۵ دلار).\n"
            "مبلغ ناخالص تخمینی است و کارمزد/فاندینگ از آن کم نشده؛ سفارش واقعی ارسال نشده."
        )
    return len(closed)


async def scanner_loop(
    store: Store, notifier: TelegramNotifier, market: Any,
    scan_interval: int, auto_sync_interval: int, scanner_enabled: bool,
) -> None:
    """Run market discovery and strategy scans independently of Telegram polling."""
    last_scan = 0.0
    last_auto_sync = 0.0
    while True:
        try:
            if scanner_enabled and market is not None:
                auto_enabled = store.get_setting("auto_pairs_enabled", "true").lower() == "true"
                force_auto_sync = store.get_setting("auto_pairs_force_sync", "false").lower() == "true"
                if auto_enabled and (force_auto_sync or time.monotonic() - last_auto_sync >= auto_sync_interval):
                    try:
                        ranked_symbols = await market.top_liquid_perpetual_symbols(
                            limit=int(os.getenv("AUTO_PAIRS_LIMIT", "30")),
                            min_quote_volume=float(os.getenv("AUTO_PAIRS_MIN_QUOTE_VOLUME", "1000000")),
                        )
                        if not ranked_symbols:
                            raise RuntimeError("WEEX returned no eligible liquid USDT perpetual markets")
                        sync_result = store.sync_auto_pairs(ranked_symbols)
                        store.set_setting("auto_pairs_force_sync", "false")
                        last_auto_sync = time.monotonic()
                        if sync_result["added"] or sync_result["removed"] or force_auto_sync:
                            await notifier.send(
                                "🤖 فهرست خودکار WEEX به‌روزرسانی شد.\n"
                                f"ارزهای جدید: {sync_result['added']} | حذف از فهرست خودکار: {sync_result['removed']}\n"
                                f"ارزهای خودکار فعال: {sync_result['auto_count']} | کل ارزهای اسکن: {sync_result['total_count']}\n"
                                "معیار انتخاب: حجم معاملات ۲۴ ساعته قراردادهای دائمی USDT.",
                            )
                    except Exception:
                        log.exception("Automatic WEEX market discovery failed")
                        last_auto_sync = time.monotonic()
                if time.monotonic() - last_scan >= scan_interval:
                    last_scan = time.monotonic()
                    config = StrategyConfig(
                        use_trend_filter=True,
                        use_sma200_filter=setting_bool(store, "sma200", "USE_SMA200_FILTER", True),
                        use_break_even=setting_bool(store, "break_even", "USE_BREAK_EVEN", True),
                        use_trailing_stop=setting_bool(store, "trailing_stop", "USE_TRAILING_STOP", True),
                    )
                    for requested in store.list_pairs():
                        try:
                            candles15 = await market.fetch_ohlcv(requested, timeframe="15m", limit=300)
                            candles4h = await market.fetch_ohlcv(requested, timeframe="4h", limit=300)
                            if len(candles15) < 20 or len(candles4h) < 20:
                                continue
                            latest_ts = str(int(candles15[-1][0]))
                            eval_key = "last_eval_candle_" + requested.replace("/", "_").replace(":", "_")
                            if store.get_setting(eval_key) != latest_ts:
                                ob_candidates: dict[int, float] = {}
                                if config.use_trailing_stop:
                                    for opened in store.open_signals(requested):
                                        candidate = ob_trailing_candidate(
                                            candles15, candles4h, opened["side"],
                                            opened.get("entry_ob_timestamp"),
                                            opened.get("stop_loss"), config,
                                        )
                                        if candidate is not None:
                                            ob_candidates[int(opened["id"])] = candidate
                                closed_rows = store.evaluate_candle(
                                    requested, float(candles15[-1][2]),
                                    float(candles15[-1][3]), float(candles15[-1][4]),
                                    ob_trailing_candidates=ob_candidates,
                                )
                                for row in closed_rows:
                                    pnl_pct = float(row["pnl_pct"])
                                    pnl_usd = row.get("demo_pnl_usd")
                                    emoji = "🟢" if (pnl_usd or 0) > 0 else ("🔴" if (pnl_usd or 0) < 0 else "🟡")
                                    title = "حد سود فعال شد" if row["exit_label"] == "TP" else "حد ضرر فعال شد"
                                    await notifier.send(
                                        f"{emoji} {title} | {row['symbol']}\nشماره: #{row['id']}\n"
                                        f"ورود: {row['entry']} | خروج: {row['exit_price']}\n"
                                        f"نتیجه دمو: {(pnl_usd or 0):+.4f}$ ({pnl_pct:+.3f}%)\n"
                                        "کارمزد و فاندینگ فعلاً محاسبه نشده؛ سفارش واقعی ارسال نشده."
                                    )
                                store.set_setting(eval_key, latest_ts)
                            signal = evaluate_latest(candles15, candles4h, config)
                            if signal:
                                signal_key = "last_signal_candle_" + requested.replace("/", "_").replace(":", "_")
                                if store.get_setting(signal_key) != str(signal["timestamp"]):
                                    if store.has_open_signal(requested, signal["side"]):
                                        log.info(
                                            "Skipping duplicate %s signal for %s: same-direction position is already open.",
                                            signal["side"], requested,
                                        )
                                        store.set_setting(signal_key, str(signal["timestamp"]))
                                    else:
                                        await send_signal_alert(
                                            notifier, store, symbol=requested, side=signal["side"],
                                            entry=signal["entry"], candles=candles15,
                                            stop_loss=signal["stop_loss"], take_profit=signal["take_profit"],
                                            order_blocks=signal["order_blocks"], fvgs=signal["fvgs"],
                                            choch_price=signal["choch_price"], timeframe="15m",
                                            break_even_enabled=config.use_break_even,
                                            trailing_enabled=config.use_trailing_stop,
                                            entry_ob_timestamp=signal.get("ob_timestamp"))
                                        store.set_setting(signal_key, str(signal["timestamp"]))
                        except Exception:
                            log.exception("Scanner error for %s", requested)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background scanner cycle failed; retrying shortly.")
            await asyncio.sleep(5)


async def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    timeframe = os.getenv("TIMEFRAME", "15m").strip()
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Render environment variables.")
    if timeframe != "15m":
        raise RuntimeError("This project is configured for the requested 15m timeframe.")

    store = Store()
    # Apply the agreed V2 defaults once; users can still change SMA200 from Telegram settings.
    if store.get_setting("strategy_v2_defaults_applied") != "true":
        store.set_setting("user_sma200", "true")
        store.set_setting("strategy_v2_defaults_applied", "true")
    notifier = TelegramNotifier(token, chat_id)
    log.info("Bot online; signals-only; timeframe=%s", timeframe)
    scanner_enabled = os.getenv("SCANNER_ENABLED", "true").strip().lower() == "true"
    scan_interval = max(30, int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
    market = None
    if scanner_enabled:
        from market_data import WeexMarketData
        market = WeexMarketData()
    await notifier.send(
        "ربات تلگرام WEEX روشن شد.\n"
        "پنل فارسی و ذخیره آمار فعال است.\n"
        "شروع ثبت آمار: " + store.get_setting("started_at") + "\n"
        + ("⚠️ اسکنر آزمایشی روشن است؛ تطبیق با TradingView هنوز باید انجام شود.\nمعامله واقعی غیرفعال است."
           if scanner_enabled else "اسکنر خاموش است تا منطق با TradingView تطبیق داده شود.\nمعامله واقعی غیرفعال است.")
    )
    offset = int(store.get_setting("telegram_offset", "0"))
    auto_sync_interval = max(3600, int(os.getenv("AUTO_PAIRS_REFRESH_SECONDS", "21600")))
    scanner_task = None
    if scanner_enabled and market is not None:
        scanner_task = asyncio.create_task(
            scanner_loop(store, notifier, market, scan_interval, auto_sync_interval, scanner_enabled),
            name="weex-market-scanner",
        )
    while True:
        try:
            updates = await notifier.updates(offset)
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                store.set_setting("telegram_offset", str(offset))
                callback = update.get("callback_query")
                if callback:
                    source_chat = str(((callback.get("message") or {}).get("chat") or {}).get("id", ""))
                    if source_chat == str(chat_id):
                        data = str(callback.get("data", ""))
                        try:
                            if data == "settings:menu":
                                await show_settings(source_chat, notifier, store)
                                await notifier.answer_callback(str(callback["id"]))
                            elif data == "settings:pairs":
                                pairs = store.list_pairs()
                                await notifier.send(
                                    "📌 جفت‌ارزهای انتخاب‌شده:\n" + ("\n".join(pairs) if pairs else "فهرست خالی است.")
                                    + "\n\nبرای افزودن/حذف فقط نماد ارز را وارد کن؛ مثال: /add BTC یا /remove BTC.",
                                    source_chat,
                                    {"inline_keyboard": [[{"text": "⬅️ بازگشت به تنظیمات", "callback_data": "settings:menu"}]]},
                                )
                                await notifier.answer_callback(str(callback["id"]))
                            elif data == "settings:chart":
                                await notifier.answer_callback(str(callback["id"]), "در حال ساخت نمودار...")
                                await handle_command("/chart BTC/USDT", source_chat, notifier, store, chat_id)
                            elif data.startswith("settings:toggle:"):
                                key = data.rsplit(":", 1)[1]
                                config_map = {
                                    "sma200": ("USE_SMA200_FILTER", True, "فیلتر SMA200"),
                                    "break_even": ("USE_BREAK_EVEN", True, "استاپ سر‌به‌سر"),
                                    "trailing_stop": ("USE_TRAILING_STOP", True, "استاپ شناور"),
                                }
                                if key not in config_map:
                                    await notifier.answer_callback(str(callback["id"]), "گزینه نامعتبر است.")
                                else:
                                    env_name, default, label = config_map[key]
                                    current = setting_bool(store, key, env_name, default)
                                    store.set_setting("user_" + key, str(not current).lower())
                                    await notifier.answer_callback(str(callback["id"]), f"{label}: {'روشن' if not current else 'خاموش'}")
                                    await show_settings(source_chat, notifier, store)
                            else:
                                await notifier.answer_callback(str(callback["id"]))
                        except Exception:
                            log.exception("Settings callback failed")
                            await notifier.answer_callback(str(callback.get("id", "")), "خطا؛ دوباره تلاش کن.")
                    continue
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
