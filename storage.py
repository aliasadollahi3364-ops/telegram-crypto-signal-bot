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
                chart_path TEXT
            )""")
            # The baseline is set once at first database creation. No historical candles
            # are imported as trades; only signal events recorded after this point count.
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('started_at',?)", (now_iso(),))
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('telegram_offset','0')",)

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def list_pairs(self) -> list[str]:
        with self._connect() as c:
            rows = c.execute("SELECT symbol FROM pairs WHERE enabled=1 ORDER BY symbol").fetchall()
            return [r["symbol"] for r in rows]

    def add_pair(self, symbol: str) -> bool:
        with self._connect() as c:
            cur = c.execute("INSERT OR IGNORE INTO pairs(symbol,enabled,added_at) VALUES(?,1,?)", (symbol, now_iso()))
            return cur.rowcount > 0

    def remove_pair(self, symbol: str) -> bool:
        with self._connect() as c:
            cur = c.execute("DELETE FROM pairs WHERE symbol=?", (symbol,))
            return cur.rowcount > 0

    def record_signal(self, symbol: str, side: str, entry: float, stop_loss: float | None,
                      take_profit: float | None, chart_path: str | None = None) -> int:
        with self._connect() as c:
            cur = c.execute("""INSERT INTO signals
                (symbol,side,created_at,entry,stop_loss,take_profit,chart_path)
                VALUES(?,?,?,?,?,?,?)""",
                (symbol, side.upper(), now_iso(), entry, stop_loss, take_profit, chart_path))
            return int(cur.lastrowid)

    def open_signals(self, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as c:
            if symbol:
                rows = c.execute("SELECT * FROM signals WHERE status='OPEN' AND symbol=? ORDER BY id DESC", (symbol,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def close_signal(self, signal_id: int, exit_price: float, result: str, pnl_pct: float) -> bool:
        with self._connect() as c:
            cur = c.execute("""UPDATE signals SET status='CLOSED',closed_at=?,exit_price=?,result=?,pnl_pct=?
                WHERE id=? AND status='OPEN'""", (now_iso(), exit_price, result, pnl_pct, signal_id))
            return cur.rowcount > 0

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
            closed = d["wins"] + d["losses"]
            d["win_rate"] = (100.0 * d["wins"] / closed) if closed else 0.0
            return d
