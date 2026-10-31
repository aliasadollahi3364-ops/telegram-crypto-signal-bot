from __future__ import annotations

import numpy as np
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy import StrategyConfig, evaluate_latest


def _candles(count: int, interval_ms: int, seed: int) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    price = 100.0
    result = []
    start = 1_750_000_000_000
    for i in range(count):
        move = float(rng.normal(0, 0.8))
        open_price = price
        close = max(1.0, open_price + move)
        high = max(open_price, close) + float(rng.uniform(0.05, 0.7))
        low = min(open_price, close) - float(rng.uniform(0.05, 0.7))
        result.append([start + i * interval_ms, open_price, high, low, close, float(rng.uniform(100, 1000))])
        price = close
    return result


def test_strategy_replay_is_safe_on_closed_market_data():
    candles_15m = _candles(500, 15 * 60 * 1000, 10)
    candles_4h = _candles(300, 4 * 60 * 60 * 1000, 20)
    signal = evaluate_latest(candles_15m, candles_4h, StrategyConfig())
    assert signal is None or signal["side"] in {"BUY", "SELL"}
    if signal:
        assert signal["entry"] > 0
        assert signal["stop_loss"] > 0
        assert signal["take_profit"] > 0
        assert signal["timestamp"] == candles_15m[-1][0]


def test_sma_filter_waits_for_enough_history():
    candles_15m = _candles(100, 15 * 60 * 1000, 30)
    candles_4h = _candles(100, 4 * 60 * 60 * 1000, 40)
    assert evaluate_latest(
        candles_15m, candles_4h, StrategyConfig(use_sma200_filter=True)
    ) is None



def test_ob_trailing_moves_only_for_newer_same_direction_ob():
    from strategy import _apply_ob_trailing_stop

    cfg = StrategyConfig(use_trailing_stop=True, trailing_atr_mult=1.5)
    trade = {"side": "BUY", "sl": 90.0, "ob_timestamp": 100}
    new_obs = [{
        "active": True, "direction": 1, "timestamp": 200,
        "bottom": 95.0, "top": 98.0,
    }]
    assert _apply_ob_trailing_stop(trade, new_obs, atr=2.0, close=100.0, cfg=cfg)
    assert trade["sl"] == 92.0

    # Older or opposite-side blocks cannot move the stop.
    trade["sl"] = 92.0
    old_or_opposite = [
        {"active": True, "direction": 1, "timestamp": 90, "bottom": 99.0, "top": 100.0},
        {"active": True, "direction": -1, "timestamp": 300, "bottom": 94.0, "top": 98.0},
    ]
    assert not _apply_ob_trailing_stop(trade, old_or_opposite, 1.0, 101.0, cfg)
    assert trade["sl"] == 92.0


def test_ob_trailing_short_only_tightens_and_stays_above_market():
    from strategy import _apply_ob_trailing_stop

    cfg = StrategyConfig(use_trailing_stop=True, trailing_atr_mult=1.5)
    trade = {"side": "SELL", "sl": 110.0, "ob_timestamp": 100}
    new_obs = [{
        "active": True, "direction": -1, "timestamp": 200,
        "bottom": 102.0, "top": 105.0,
    }]
    assert _apply_ob_trailing_stop(trade, new_obs, atr=2.0, close=100.0, cfg=cfg)
    assert trade["sl"] == 108.0

    # A candidate below the market is rejected; stop never loosens.
    trade["sl"] = 108.0
    too_low = [{"active": True, "direction": -1, "timestamp": 300,
                "bottom": 90.0, "top": 92.0}]
    assert not _apply_ob_trailing_stop(trade, too_low, 1.0, 100.0, cfg)
    assert trade["sl"] == 108.0

def test_ob_trailing_uses_latest_closed_15m_bar_open_timestamp(monkeypatch):
    import strategy as strategy_module
    from strategy import ob_trailing_candidate

    interval = 15 * 60 * 1000
    start = 1_750_000_000_000
    candles_15m = [
        [start + i * interval, 99.0, 101.0, 99.0, 100.0, 10.0]
        for i in range(3)
    ]
    latest_open = pd.to_datetime(candles_15m[-1][0], unit="ms", utc=True)
    event = {
        "timestamp": latest_open, "direction": 1,
        "top": 99.0, "bottom": 96.0, "left": latest_open,
    }
    htf = pd.DataFrame({
        "timestamp": [latest_open - pd.Timedelta(hours=4)],
        "close": [100.0],
    })
    monkeypatch.setattr(strategy_module, "_build_ob_events", lambda _candles, _cfg: ([event], htf))

    candidate = ob_trailing_candidate(
        candles_15m, [[start, 99.0, 101.0, 99.0, 100.0, 10.0]],
        "BUY", start, 90.0,
        StrategyConfig(atr_len=3, use_trailing_stop=True, trailing_atr_mult=1.5),
    )
    assert candidate == 93.0


def test_ob_trailing_does_not_delay_event_to_next_15m_close(monkeypatch):
    import strategy as strategy_module
    from strategy import ob_trailing_candidate

    interval = 15 * 60 * 1000
    start = 1_750_000_000_000
    candles_15m = [
        [start + i * interval, 99.0, 101.0, 99.0, 100.0, 10.0]
        for i in range(3)
    ]
    latest_open = pd.to_datetime(candles_15m[-1][0], unit="ms", utc=True)
    event = {
        "timestamp": latest_open + pd.Timedelta(minutes=15), "direction": 1,
        "top": 99.0, "bottom": 96.0, "left": latest_open,
    }
    htf = pd.DataFrame({
        "timestamp": [latest_open - pd.Timedelta(hours=4)],
        "close": [100.0],
    })
    monkeypatch.setattr(strategy_module, "_build_ob_events", lambda _candles, _cfg: ([event], htf))

    candidate = ob_trailing_candidate(
        candles_15m, [[start, 99.0, 101.0, 99.0, 100.0, 10.0]],
        "BUY", start, 90.0,
        StrategyConfig(atr_len=3, use_trailing_stop=True, trailing_atr_mult=1.5),
    )
    assert candidate is None

