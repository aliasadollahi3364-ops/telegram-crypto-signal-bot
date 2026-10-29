"""Candlestick chart renderer with strategy overlays for Telegram signal images."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence


def render_signal_chart(
    candles: Sequence[Sequence[float]],
    symbol: str,
    timeframe: str,
    side: str,
    entry: float,
    stop_loss: float | None = None,
    take_profit: float | None = None,
    order_blocks: Sequence[dict[str, Any]] = (),
    fvgs: Sequence[dict[str, Any]] = (),
    choch_price: float | None = None,
    output_path: str = "/tmp/weex_signal.png",
    trailing_enabled: bool = False,
) -> str:
    """Render OHLCV candles and provided overlays. Expects [timestamp, open, high, low, close, volume]."""
    if len(candles) < 3:
        raise ValueError("At least three candles are required to render a chart.")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime, timezone
    from matplotlib.patches import Rectangle

    dates = [datetime.fromtimestamp(float(c[0]) / 1000, tz=timezone.utc) for c in candles]
    fig, ax = plt.subplots(figsize=(12, 7))
    xs = list(range(len(candles)))
    width = 0.62
    for i, c in enumerate(candles):
        _, op, hi, lo, cl = c[:5]
        rising = float(cl) >= float(op)
        ax.vlines(i, float(lo), float(hi), linewidth=1)
        bottom = min(float(op), float(cl))
        height = max(abs(float(cl) - float(op)), max(abs(float(hi)) * 0.00003, 1e-9))
        ax.add_patch(Rectangle((i - width / 2, bottom), width, height,
                               facecolor=("#16a34a" if rising else "#dc2626"),
                               edgecolor=("#15803d" if rising else "#b91c1c"),
                               fill=True, alpha=0.82, linewidth=0.7))
    # Zones are supplied by the strategy adapter; this function does not invent them.
    for zone in order_blocks:
        lo, hi = float(zone["low"]), float(zone["high"])
        start = int(zone.get("start", 0))
        end = int(zone.get("end", len(candles) - 1))
        ax.axhspan(lo, hi, xmin=max(0, start) / max(1, len(candles)),
                   xmax=min(len(candles)-1, end) / max(1, len(candles)),
                   alpha=0.18, label=str(zone.get("label", "Order Block")))
    for zone in fvgs:
        lo, hi = float(zone["low"]), float(zone["high"])
        ax.axhspan(lo, hi, alpha=0.10, label=str(zone.get("label", "FVG")))
    ax.axhline(float(entry), linewidth=1.5, linestyle="-", label=f"Entry {entry:g}")
    if stop_loss is not None:
        sl_label = f"SL / استاپ شناور اولیه {stop_loss:g}" if trailing_enabled else f"SL {stop_loss:g}"
        ax.axhline(float(stop_loss), linewidth=1.2, linestyle="--", label=sl_label)
    if take_profit is not None:
        ax.axhline(float(take_profit), linewidth=1.2, linestyle="--", label=f"TP {take_profit:g}")
    if choch_price is not None:
        ax.axhline(float(choch_price), linewidth=1, linestyle=":", label=f"CHoCH {choch_price:g}")
    ax.set_title(f"WEEX | {symbol} | {timeframe} | {side.upper()}\nزمان کندل‌ها: UTC")
    ax.set_ylabel("قیمت")
    ax.set_xlabel("کندل")
    ticks = list(range(0, len(dates), max(1, len(dates)//8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates[i].strftime("%m-%d %H:%M") for i in ticks], rotation=30, ha="right")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)


def render_market_chart(
    candles: Sequence[Sequence[float]],
    symbol: str,
    timeframe: str = "15m",
    output_path: str = "/tmp/weex_market.png",
) -> str:
    """Render a fresh market snapshot; no signals or strategy levels are invented."""
    if len(candles) < 3:
        raise ValueError("WEEX returned fewer than three confirmed candles.")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from datetime import datetime, timezone
    from matplotlib.patches import Rectangle

    dates = [datetime.fromtimestamp(float(c[0]) / 1000, tz=timezone.utc) for c in candles]
    fig, ax = plt.subplots(figsize=(12, 7))
    width = 0.62
    for i, c in enumerate(candles):
        _, op, hi, lo, cl = c[:5]
        op, hi, lo, cl = map(float, (op, hi, lo, cl))
        rising = cl >= op
        ax.vlines(i, lo, hi, linewidth=1, color=("#15803d" if rising else "#b91c1c"))
        bottom = min(op, cl)
        height = max(abs(cl-op), max(abs(hi)*0.00003, 1e-9))
        ax.add_patch(Rectangle((i-width/2, bottom), width, height,
                               facecolor=("#16a34a" if rising else "#dc2626"),
                               edgecolor=("#15803d" if rising else "#b91c1c"),
                               linewidth=0.7, alpha=0.85))
    last = float(candles[-1][4])
    ax.axhline(last, linestyle="--", linewidth=1, label=f"آخرین بسته‌شدن: {last:g}")
    ticks = list(range(0, len(dates), max(1, len(dates)//8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([dates[i].strftime("%m-%d %H:%M") for i in ticks], rotation=30, ha="right")
    ax.set_title(f"WEEX | {symbol} | {timeframe} | نمای بازار\nکندل‌های تأییدشده، زمان UTC")
    ax.set_ylabel("قیمت")
    ax.set_xlabel("زمان کندل (UTC)")
    ax.grid(True, alpha=0.2)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)
