from __future__ import annotations

import numpy as np
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
