"""Persistent SQLite storage for signals created after bot startup."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("DATA_DIR", "/var/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path(".")
DB_PATH = DATA_DIR / "signal_bot.sqlite3"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS pairs (
                symbol TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL, side TEXT NOT NULL,
                created_at TEXT NOT NULL, entry REAL NOT NULL,
                stop_loss REAL, take_profit REAL, status TEXT NOT NULL DEFAULT 'OPEN',
                closed_at TEXT, exit_price REAL, result TEXT, pnl_pct REAL,
                chart_path TEXT,
                initial_stop_loss REAL, initial_risk REAL, extreme REAL,
                break_even_enabled INTEGER NOT NULL DEFAULT 0,
                trailing_enabled INTEGER NOT NULL DEFAULT 0,
                trail_distance REAL
            )""")
            # Migrate databases created by older versions without discarding user data.
            columns = {row["name"] for row in c.execute("PRAGMA table_info(signals)")}
            migrations = {
                "initial_stop_loss": "REAL",
                "initial_risk": "REAL",
                "extreme": "REAL",
                "break_even_enabled": "INTEGER NOT NULL DEFAULT 0",
                "trailing_enabled": "INTEGER NOT NULL DEFAULT 0",
                "trail_distance": "REAL",
            }
            for name, sql_type in migrations.items():
                if name not in columns:
                    c.execute(f"ALTER TABLE signals ADD COLUMN {name} {sql_type}")
            # The baseline is set once at first database creation. No historical candles
            # are imported as trades; only signal events recorded after this point count.
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('started_at',?)", (now_iso(),))
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('telegram_offset','0')")

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def list_pairs(self) -> list[str]:
        with self._connect() as c:
            rows = c.execute("SELECT symbol FROM pairs WHERE enabled=1 ORDER BY symbol").fetchall()
            return [r["symbol"] for r in rows]

    def add_pair(self, symbol: str) -> bool:
        with self._connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO pairs(symbol,enabled,added_at) VALUES(?,1,?)",
                (symbol, now_iso()),
            )
            return cur.rowcount > 0

    def remove_pair(self, symbol: str) -> bool:
        with self._connect() as c:
            cur = c.execute("DELETE FROM pairs WHERE symbol=?", (symbol,))
            return cur.rowcount > 0

    def record_signal(
        self, symbol: str, side: str, entry: float, stop_loss: float | None,
        take_profit: float | None, chart_path: str | None = None,
        *, break_even_enabled: bool = False, trailing_enabled: bool = False,
    ) -> int:
        normalized_side = side.upper()
        risk = None if stop_loss is None else (
            entry - stop_loss if normalized_side == "BUY" else stop_loss - entry
        )
        valid_risk = risk is not None and risk > 0
        with self._connect() as c:
            cur = c.execute("""INSERT INTO signals
                (symbol,side,created_at,entry,stop_loss,take_profit,chart_path,
                 initial_stop_loss,initial_risk,extreme,break_even_enabled,
                 trailing_enabled,trail_distance)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (symbol, normalized_side, now_iso(), entry, stop_loss, take_profit,
                 chart_path, stop_loss, risk if valid_risk else None, entry,
                 int(break_even_enabled and valid_risk), int(trailing_enabled and valid_risk),
                 risk if valid_risk else None))
            return int(cur.lastrowid)

    def open_signals(self, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as c:
            if symbol:
                rows = c.execute(
                    "SELECT * FROM signals WHERE status='OPEN' AND symbol=? ORDER BY id DESC",
                    (symbol,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM signals WHERE status='OPEN' ORDER BY id DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def close_signal(self, signal_id: int, exit_price: float, result: str, pnl_pct: float) -> bool:
        with self._connect() as c:
            cur = c.execute("""UPDATE signals
                SET status='CLOSED',closed_at=?,exit_price=?,result=?,pnl_pct=?
                WHERE id=? AND status='OPEN'""",
                (now_iso(), exit_price, result, pnl_pct, signal_id))
            return cur.rowcount > 0

    def evaluate_candle(
        self, symbol: str, candle_high: float, candle_low: float
    ) -> list[dict[str, Any]]:
        """Evaluate a newly closed candle for paper positions.

        Existing SL/TP levels are checked first. If both are touched, SL wins
        conservatively. Break-even/trailing levels are only updated after the candle
        survives, so a newly moved stop cannot be triggered retroactively by that
        same candle's low/high. Updated stops apply from the next candle onward.
        """
        closed: list[dict[str, Any]] = []
        for row in self.open_signals(symbol):
            side = row["side"].upper()
            sl, tp = row["stop_loss"], row["take_profit"]
            sl_hit = sl is not None and (
                (side == "BUY" and candle_low <= sl) or
                (side == "SELL" and candle_high >= sl)
            )
            tp_hit = tp is not None and (
                (side == "BUY" and candle_high >= tp) or
                (side == "SELL" and candle_low <= tp)
            )
            if sl_hit or tp_hit:
                # Conservative tie-break: SL first when OHLC cannot show the order.
                if sl_hit:
                    exit_price, result, label = float(sl), "LOSS", "SL"
                else:
                    exit_price, result, label = float(tp), "WIN", "TP"
                entry = float(row["entry"])
                pnl_pct = (
                    (exit_price - entry) / entry * 100.0
                    if side == "BUY" else (entry - exit_price) / entry * 100.0
                )
                if self.close_signal(int(row["id"]), exit_price, result, pnl_pct):
                    closed.append({
                        **row, "exit_price": exit_price, "result": result,
                        "pnl_pct": pnl_pct, "exit_label": label,
                    })
                continue

            # Stops can only ratchet toward profit; never loosen the existing stop.
            entry = float(row["entry"])
            current_sl = float(sl) if sl is not None else None
            risk = row["initial_risk"]
            new_extreme = row["extreme"]
            if new_extreme is None:
                new_extreme = entry
            new_sl = current_sl

            if risk is not None and risk > 0 and row["break_even_enabled"]:
                trigger = entry + risk if side == "BUY" else entry - risk
                reached = candle_high >= trigger if side == "BUY" else candle_low <= trigger
                if reached and new_sl is not None:
                    new_sl = max(new_sl, entry) if side == "BUY" else min(new_sl, entry)

            if risk is not None and risk > 0 and row["trailing_enabled"]:
                distance = float(row["trail_distance"] or risk)
                if side == "BUY":
                    new_extreme = max(float(new_extreme), candle_high)
                    candidate = float(new_extreme) - distance
                    if tp is not None:
                        candidate = min(candidate, float(tp) * (1.0 - 1e-12))
                    if new_sl is not None:
                        new_sl = max(new_sl, candidate)
                else:
                    new_extreme = min(float(new_extreme), candle_low)
                    candidate = float(new_extreme) + distance
                    if tp is not None:
                        candidate = max(candidate, float(tp) * (1.0 + 1e-12))
                    if new_sl is not None:
                        new_sl = min(new_sl, candidate)

            if new_sl != current_sl or new_extreme != row["extreme"]:
                with self._connect() as c:
                    c.execute(
                        "UPDATE signals SET stop_loss=?,extreme=? WHERE id=? AND status='OPEN'",
                        (new_sl, new_extreme, row["id"]),
                    )
        return closed

    def stats(self, symbol: str | None = None) -> dict[str, Any]:
        with self._connect() as c:
            where = "WHERE symbol=?" if symbol else ""
            params = (symbol,) if symbol else ()
            row = c.execute(f"""SELECT COUNT(*) total,
                SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) opened,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) losses,
                COALESCE(SUM(CASE WHEN status='CLOSED' THEN pnl_pct ELSE 0 END),0) pnl_pct
                FROM signals {where}""", params).fetchone()
            d = dict(row)
            d["total"] = d["total"] or 0
            d["opened"] = d["opened"] or 0
            d["wins"] = d["wins"] or 0
            d["losses"] = d["losses"] or 0
            d["pnl_pct"] = d["pnl_pct"] or 0.0
            closed_count = d["wins"] + d["losses"]
            d["win_rate"] = (100.0 * d["wins"] / closed_count) if closed_count else 0.0
            return d
