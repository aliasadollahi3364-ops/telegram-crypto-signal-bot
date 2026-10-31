from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "signals.sqlite3")


def test_break_even_stop_updates_for_next_candle_only(tmp_path: Path):
    store = _store(tmp_path)
    signal_id = store.record_signal(
        "BTC/USDT", "BUY", 100.0, 90.0, 120.0,
        break_even_enabled=True,
    )

    # Reaches +1R but does not hit the old stop or target.
    assert store.evaluate_candle("BTC/USDT", candle_high=111.0, candle_low=101.0) == []
    opened = store.open_signals("BTC/USDT")[0]
    assert opened["id"] == signal_id
    assert opened["stop_loss"] == 100.0

    # The break-even stop is now active and applies to this subsequent candle.
    closed = store.evaluate_candle("BTC/USDT", candle_high=105.0, candle_low=99.5)
    assert len(closed) == 1
    assert closed[0]["exit_label"] == "SL"
    assert closed[0]["result"] == "BREAKEVEN"
    assert closed[0]["pnl_pct"] == 0.0


def test_ob_trailing_stop_ratchets_and_profitable_stop_counts_as_win(tmp_path: Path):
    store = _store(tmp_path)
    signal_id = store.record_signal(
        "BTC/USDT", "BUY", 100.0, 90.0, 120.0,
        trailing_enabled=True,
    )

    # The OB trailing engine supplies a candidate stop at 102 after this
    # candle survives; the updated stop must not be tested retroactively.
    assert store.evaluate_candle(
        "BTC/USDT",
        candle_high=112.0,
        candle_low=101.0,
        ob_trailing_candidates={signal_id: 102.0},
    ) == []
    opened = store.open_signals("BTC/USDT")[0]
    assert opened["stop_loss"] == 102.0

    # A later candle reaches the ratcheted stop and closes profitably.
    closed = store.evaluate_candle("BTC/USDT", candle_high=105.0, candle_low=101.5)
    assert len(closed) == 1
    assert closed[0]["exit_label"] == "SL"
    assert closed[0]["result"] == "WIN"
    assert closed[0]["pnl_pct"] == 2.0
    stats = store.stats("BTC/USDT")
    assert stats["wins"] == 1
    assert stats["losses"] == 0


def test_same_candle_sl_and_tp_is_conservatively_loss(tmp_path: Path):
    store = _store(tmp_path)
    store.record_signal("ETH/USDT", "BUY", 100.0, 95.0, 110.0)

    closed = store.evaluate_candle("ETH/USDT", candle_high=111.0, candle_low=94.0)
    assert len(closed) == 1
    assert closed[0]["exit_label"] == "SL"
    assert closed[0]["result"] == "LOSS"
    assert closed[0]["exit_price"] == 95.0


def test_same_direction_duplicate_open_signal_is_blocked_but_opposite_is_allowed(tmp_path: Path):
    store = _store(tmp_path)
    first_id = store.record_signal("GRASS/USDT", "BUY", 0.5903, 0.56, 0.65)

    assert store.has_open_signal("GRASS/USDT", "BUY") is True
    assert store.has_open_signal("GRASS/USDT", "SELL") is False

    # A different direction remains eligible under the agreed per-direction rule.
    store.record_signal("GRASS/USDT", "SELL", 0.5903, 0.62, 0.52)
    assert len(store.open_signals("GRASS/USDT")) == 2

    # Once the BUY is closed, another BUY is no longer blocked.
    assert store.close_signal(first_id, 0.56, "LOSS", -5.0) is True
    assert store.has_open_signal("GRASS/USDT", "BUY") is False
