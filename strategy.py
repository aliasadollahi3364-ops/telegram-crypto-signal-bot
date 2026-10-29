"""Python signal adapter for the user's WEEX Pine strategy.

This is a conservative port of the submitted rules, not a claim of bit-for-bit
TradingView parity. It uses only closed candles and emits at most the latest
new 15m signal. Do not enable notifications as production signals until
cross-checked against TradingView on matching symbols and candle history.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class StrategyConfig:
    htf: str = "4h"
    pivot4: int = 3
    body_len: int = 10
    ob_search: int = 12
    pivot15: int = 3
    max_wait: int = 17
    atr_len: int = 7
    use_trend_filter: bool = False
    require_ob_touch: bool = True
    use_touch_freshness: bool = True
    max_bars_since_touch: int = 14
    use_choch_filter: bool = True
    use_choch_strength: bool = True
    min_choch_body_atr: float = 0.85
    use_retest_filter: bool = True
    retest_atr: float = 0.15
    use_confirm_candle: bool = True
    use_confirm_strength: bool = True
    min_confirm_body_atr: float = 0.20
    risk_reward: float = 2.0
    sl_buffer_atr: float = 0.15
    use_ob_boundary_sl: bool = True
    use_sma200_filter: bool = False
    sma_length: int = 200
    use_break_even: bool = True
    break_even_trigger_r: float = 1.0
    use_trailing_stop: bool = True
    trailing_atr_mult: float = 1.5  # Kept for config compatibility; OB-based trailing uses the ATR buffer below.


def _prepare(candles: list[list[float]]) -> pd.DataFrame:
    if not candles:
        raise ValueError("No candles provided")
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length, min_periods=length).mean()


def _confirmed_pivots(df: pd.DataFrame, left: int, right: int) -> tuple[pd.Series, pd.Series]:
    """Pivot values are returned on the confirmation bar, after right bars exist."""
    highs = pd.Series(np.nan, index=df.index, dtype=float)
    lows = pd.Series(np.nan, index=df.index, dtype=float)
    for confirm_idx in range(left + right, len(df)):
        pivot_idx = confirm_idx - right
        hwindow = df["high"].iloc[pivot_idx-left:pivot_idx+right+1]
        lwindow = df["low"].iloc[pivot_idx-left:pivot_idx+right+1]
        if len(hwindow) == left + right + 1 and df["high"].iloc[pivot_idx] == hwindow.max():
            highs.iloc[confirm_idx] = float(df["high"].iloc[pivot_idx])
        if len(lwindow) == left + right + 1 and df["low"].iloc[pivot_idx] == lwindow.min():
            lows.iloc[confirm_idx] = float(df["low"].iloc[pivot_idx])
    return highs, lows


def _build_ob_events(htf_candles: list[list[float]], cfg: StrategyConfig) -> tuple[list[dict[str, Any]], pd.DataFrame]:
    htf = _prepare(htf_candles)
    ph, pl = _confirmed_pivots(htf, cfg.pivot4, cfg.pivot4)
    htf["ph"], htf["pl"] = ph, pl
    body = (htf["close"] - htf["open"]).abs()
    htf["avg_body"] = body.rolling(cfg.body_len, min_periods=cfg.body_len).mean()
    last_ph = last_pl = None
    trend = 0
    events: list[dict[str, Any]] = []
    htf["trend"] = 0
    for i, row in htf.iterrows():
        if pd.notna(row["ph"]):
            last_ph = float(row["ph"])
        if pd.notna(row["pl"]):
            last_pl = float(row["pl"])
        strong = i > 0 and pd.notna(htf.at[i-1, "avg_body"]) and body.iloc[i] > htf.at[i-1, "avg_body"]
        bull_bos = last_ph is not None and row["close"] > last_ph and (i == 0 or htf.at[i-1, "close"] <= last_ph)
        bear_bos = last_pl is not None and row["close"] < last_pl and (i == 0 or htf.at[i-1, "close"] >= last_pl)
        if bull_bos:
            trend = 1
            if strong:
                found = next((j for j in range(i-1, max(-1, i-cfg.ob_search-1), -1)
                              if htf.at[j, "close"] < htf.at[j, "open"]), None)
                if found is not None:
                    events.append({"timestamp": row["timestamp"] + pd.Timedelta(hours=4), "direction": 1,
                                   "top": float(htf.at[found, "high"]), "bottom": float(htf.at[found, "low"]),
                                   "left": htf.at[found, "timestamp"], "trend": trend})
        if bear_bos:
            trend = -1
            if strong:
                found = next((j for j in range(i-1, max(-1, i-cfg.ob_search-1), -1)
                              if htf.at[j, "close"] > htf.at[j, "open"]), None)
                if found is not None:
                    events.append({"timestamp": row["timestamp"] + pd.Timedelta(hours=4), "direction": -1,
                                   "top": float(htf.at[found, "high"]), "bottom": float(htf.at[found, "low"]),
                                   "left": htf.at[found, "timestamp"], "trend": trend})
        htf.at[i, "trend"] = trend
    return events, htf


def evaluate_latest(
    candles_15m: list[list[float]],
    candles_4h: list[list[float]],
    cfg: StrategyConfig | None = None,
) -> dict[str, Any] | None:
    """Replay closed bars and return a signal only if one is generated on the latest bar."""
    cfg = cfg or StrategyConfig()
    df = _prepare(candles_15m)
    if len(df) < max(cfg.sma_length if cfg.use_sma200_filter else 0, cfg.atr_len + cfg.pivot15 * 2 + 3):
        return None
    events, htf = _build_ob_events(candles_4h, cfg)
    ph15, pl15 = _confirmed_pivots(df, cfg.pivot15, cfg.pivot15)
    df["ph15"], df["pl15"] = ph15, pl15
    df["atr"] = _atr(df, cfg.atr_len)
    df["sma"] = df["close"].rolling(cfg.sma_length, min_periods=cfg.sma_length).mean()

    obs: list[dict[str, Any]] = []
    last_ph15 = last_pl15 = None
    trend4 = 0
    event_idx = 0
    in_position = False
    trade: dict[str, Any] | None = None
    latest_signal = None

    for i, row in df.iterrows():
        ts = row["timestamp"]
        # Only HTF candles whose close timestamp is at/before this 15m close are considered.
        while event_idx < len(events) and events[event_idx]["timestamp"] <= ts:
            obs.append({**events[event_idx], "active": True, "used": False, "touched": False,
                        "touch_bar": None, "has_choch": False, "choch_level": None, "choch_bar": None})
            event_idx += 1
        prior_htf = htf[htf["timestamp"] + pd.Timedelta(hours=4) <= ts]
        if not prior_htf.empty:
            trend4 = int(prior_htf.iloc[-1]["trend"])
            confirmed_close = float(prior_htf.iloc[-1]["close"])
            for ob in obs:
                if ob["active"] and ((ob["direction"] == 1 and confirmed_close < ob["bottom"]) or
                                     (ob["direction"] == -1 and confirmed_close > ob["top"])):
                    ob["active"] = False

        if pd.notna(row["ph15"]):
            last_ph15 = float(row["ph15"])
        if pd.notna(row["pl15"]):
            last_pl15 = float(row["pl15"])
        atr = row["atr"]
        if pd.isna(atr):
            continue

        # Resolve a position against the levels that were active before this candle.
        # If it survives, update BE/trailing for the next candle only; never let a
        # newly moved stop trigger retroactively on the same OHLC bar.
        if trade is not None:
            high, low = float(row["high"]), float(row["low"])
            if trade["side"] == "BUY":
                sl_hit = low <= trade["sl"]
                tp_hit = high >= trade["tp"]
            else:
                sl_hit = high >= trade["sl"]
                tp_hit = low <= trade["tp"]
            if sl_hit or tp_hit:
                trade = None
            else:
                if trade["side"] == "BUY":
                    if cfg.use_break_even and high >= trade["entry"] + trade["initial_risk"] * cfg.break_even_trigger_r:
                        trade["sl"] = max(trade["sl"], trade["entry"])
                    if cfg.use_trailing_stop:
                        # Trail only behind a confirmed, active bullish order block.
                        # Never move the stop down or into the selected OB.
                        candidates = [ob for ob in obs
                                      if ob["active"] and ob["direction"] == 1
                                      and ob["bottom"] < float(row["close"])]
                        if candidates:
                            latest_ob = max(candidates, key=lambda x: x["timestamp"])
                            trail = float(latest_ob["bottom"]) - float(atr) * cfg.sl_buffer_atr
                            trail = min(trail, trade["tp"] - 1e-12)
                            trade["sl"] = max(trade["sl"], trail)
                else:
                    if cfg.use_break_even and low <= trade["entry"] - trade["initial_risk"] * cfg.break_even_trigger_r:
                        trade["sl"] = min(trade["sl"], trade["entry"])
                    if cfg.use_trailing_stop:
                        # Trail only behind a confirmed, active bearish order block.
                        # Never move the stop up or into the selected OB.
                        candidates = [ob for ob in obs
                                      if ob["active"] and ob["direction"] == -1
                                      and ob["top"] > float(row["close"])]
                        if candidates:
                            latest_ob = max(candidates, key=lambda x: x["timestamp"])
                            trail = float(latest_ob["top"]) + float(atr) * cfg.sl_buffer_atr
                            trail = max(trail, trade["tp"] + 1e-12)
                            trade["sl"] = min(trade["sl"], trail)
        if trade is not None:
            continue

        for ob in obs:
            if not ob["active"] or ob["used"]:
                continue
            direction, top, bottom = ob["direction"], ob["top"], ob["bottom"]
            if cfg.use_trend_filter and not ((direction == 1 and trend4 == 1) or (direction == -1 and trend4 == -1)):
                continue
            touched_now = float(row["low"]) <= top and float(row["high"]) >= bottom
            if touched_now:
                ob["touched"], ob["touch_bar"] = True, i
            touched = ob["touched"]
            touch_bar = ob["touch_bar"]
            fresh = touched and (not cfg.use_touch_freshness or (touch_bar is not None and i - touch_bar <= cfg.max_bars_since_touch))
            can_use = not cfg.require_ob_touch or fresh
            has_choch = ob["has_choch"]
            bull_cross = last_ph15 is not None and row["close"] > last_ph15 and (i == 0 or df.at[i-1, "close"] <= last_ph15) and row["close"] > row["open"]
            bear_cross = last_pl15 is not None and row["close"] < last_pl15 and (i == 0 or df.at[i-1, "close"] >= last_pl15) and row["close"] < row["open"]
            bull_strong = not cfg.use_choch_strength or row["close"] - row["open"] >= atr * cfg.min_choch_body_atr
            bear_strong = not cfg.use_choch_strength or row["open"] - row["close"] >= atr * cfg.min_choch_body_atr
            if can_use and cfg.use_choch_filter and not has_choch and direction == 1 and bull_cross and bull_strong:
                ob.update(has_choch=True, choch_level=last_ph15, choch_bar=i)
            if can_use and cfg.use_choch_filter and not has_choch and direction == -1 and bear_cross and bear_strong:
                ob.update(has_choch=True, choch_level=last_pl15, choch_bar=i)
            has_choch, level, choch_bar = ob["has_choch"], ob["choch_level"], ob["choch_bar"]
            if has_choch and choch_bar is not None and i - choch_bar > cfg.max_wait:
                ob.update(has_choch=False, choch_level=None, choch_bar=None)
                has_choch, level, choch_bar = False, None, None
            after_choch = (not cfg.use_choch_filter) or (has_choch and choch_bar is not None and i > choch_bar)
            retest_level = level if cfg.use_choch_filter else (last_ph15 if direction == 1 else last_pl15)
            tolerance = float(atr) * cfg.retest_atr
            retest_detected = after_choch and retest_level is not None and float(row["low"]) <= retest_level + tolerance and float(row["high"]) >= retest_level - tolerance
            retest = not cfg.use_retest_filter or retest_detected
            bull_dir = not cfg.use_confirm_candle or (row["close"] > row["open"] and (not cfg.use_retest_filter or retest_level is None or row["close"] > retest_level))
            bear_dir = not cfg.use_confirm_candle or (row["close"] < row["open"] and (not cfg.use_retest_filter or retest_level is None or row["close"] < retest_level))
            bull_confirm = bull_dir and (not cfg.use_confirm_strength or row["close"] - row["open"] >= atr * cfg.min_confirm_body_atr)
            bear_confirm = bear_dir and (not cfg.use_confirm_strength or row["open"] - row["close"] >= atr * cfg.min_confirm_body_atr)
            side = None
            sl = None
            if can_use and retest and direction == 1 and bull_confirm and last_pl15 is not None:
                sl = (min(last_pl15, bottom) if cfg.use_ob_boundary_sl else last_pl15) - atr * cfg.sl_buffer_atr
                if sl < row["close"] and row["close"] - sl > 0:
                    side = "BUY"
            if can_use and retest and direction == -1 and bear_confirm and last_ph15 is not None:
                sl = (max(last_ph15, top) if cfg.use_ob_boundary_sl else last_ph15) + atr * cfg.sl_buffer_atr
                if sl > row["close"] and sl - row["close"] > 0:
                    side = "SELL"
            if side and (not cfg.use_sma200_filter or (pd.notna(row["sma"]) and ((side == "BUY" and row["close"] > row["sma"]) or (side == "SELL" and row["close"] < row["sma"])))):
                entry = float(row["close"])
                distance = entry - float(sl) if side == "BUY" else float(sl) - entry
                if distance <= 0:
                    continue
                tp = entry + distance * cfg.risk_reward if side == "BUY" else entry - distance * cfg.risk_reward
                trade = {"side": side, "entry": entry, "sl": float(sl), "tp": tp,
                         "initial_risk": distance, "trail_distance": distance,
                         "extreme": float(row["high"] if side == "BUY" else row["low"]),
                         "ob_top": float(top), "ob_bottom": float(bottom),
                         "ob_direction": int(direction)}
                ob["used"] = True
                if i == len(df) - 1:
                    latest_signal = {
                    "side": side, "entry": entry, "stop_loss": float(sl), "take_profit": float(tp),
                    "timestamp": int(ts.timestamp() * 1000),
                    "choch_price": float(level) if level is not None else None,
                    "order_blocks": [{"low": bottom, "high": top, "start": 0, "end": len(df)-1,
                                      "label": "Order Block صعودی" if direction == 1 else "Order Block نزولی"}],
                    "fvgs": _valid_fvgs(df.iloc[:i+1]),
                    }
                break
    return latest_signal


def _valid_fvgs(df: pd.DataFrame) -> list[dict[str, Any]]:
    zones: list[dict[str, Any]] = []
    for i in range(2, len(df)):
        high_2 = float(df.iloc[i-2]["high"])
        low_2 = float(df.iloc[i-2]["low"])
        high, low, close = float(df.iloc[i]["high"]), float(df.iloc[i]["low"]), float(df.iloc[i]["close"])
        if low > high_2:
            zones.append({"low": high_2, "high": low, "label": "FVG صعودی"})
        elif high < low_2:
            zones.append({"low": high, "high": low_2, "label": "FVG نزولی"})
    valid = []
    for z in zones:
        invalid = False
        # Locate the bar where this zone formed; only later closes can invalidate it.
        formed = next((j for j in range(2, len(df)) if
                       (float(df.iloc[j]["low"]) > float(df.iloc[j-2]["high"]) and "صعودی" in z["label"] and
                        abs(float(df.iloc[j]["low"]) - z["high"]) < 1e-12) or
                       (float(df.iloc[j]["high"]) < float(df.iloc[j-2]["low"]) and "نزولی" in z["label"] and
                        abs(float(df.iloc[j]["high"]) - z["low"]) < 1e-12)), len(df)-1)
        for j in range(formed + 1, len(df)):
            close = float(df.iloc[j]["close"])
            if ("صعودی" in z["label"] and close <= z["low"]) or ("نزولی" in z["label"] and close >= z["high"]):
                invalid = True
                break
        if not invalid:
            valid.append(z)
    return valid[-100:]
