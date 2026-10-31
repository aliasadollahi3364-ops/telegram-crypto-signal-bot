"""Public WEEX perpetual-market data access (signals only; no private API credentials)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import ccxt
import httpx

log = logging.getLogger("signal-bot.market")


class WeexMarketData:
    """CCXT adapter restricted to public linear perpetual market data."""

    def __init__(self) -> None:
        self.exchange = ccxt.weex({
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": "swap"},
        })
        self._loaded = False

    async def load_markets(self) -> dict[str, Any]:
        markets = await asyncio.to_thread(self.exchange.load_markets)
        self._loaded = True
        return markets

    async def perpetual_symbols(self) -> list[str]:
        if not self._loaded:
            await self.load_markets()
        return sorted(
            symbol for symbol, market in self.exchange.markets.items()
            if market.get("swap") and market.get("linear")
            and market.get("quote") == "USDT"
            and market.get("active", True)
        )

    async def top_liquid_perpetual_symbols(
        self, limit: int = 30, min_quote_volume: float = 1_000_000
    ) -> list[str]:
        """Return the most liquid active USDT linear perpetuals by 24h quote volume."""
        if not self._loaded:
            await self.load_markets()
        url = "https://api-contract.weex.com/capi/v3/market/ticker/24hr"
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.get(url)
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected WEEX 24h ticker response")
        markets_by_id = {
            str(market.get("id", "")).upper(): (symbol, market)
            for symbol, market in self.exchange.markets.items()
            if market.get("swap") and market.get("linear")
            and market.get("quote") == "USDT"
            and market.get("active", True)
        }
        ranked: list[tuple[float, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            match = markets_by_id.get(str(row.get("symbol", "")).upper())
            if not match:
                continue
            symbol, market = match
            try:
                quote_volume = float(row.get("quoteVolume") or 0)
                last_price = float(row.get("lastPrice") or 0)
            except (TypeError, ValueError):
                continue
            if quote_volume < min_quote_volume or last_price <= 0:
                continue
            # Use the plain BASE/USDT form already understood by resolve_symbol.
            ranked.append((quote_volume, f"{market.get('base')}/USDT"))
        ranked.sort(key=lambda item: item[0], reverse=True)
        unique: list[str] = []
        seen: set[str] = set()
        for _, symbol in ranked:
            if symbol not in seen:
                unique.append(symbol)
                seen.add(symbol)
            if len(unique) >= max(1, min(int(limit), 100)):
                break
        return unique

    async def resolve_symbol(self, requested: str) -> str:
        """Accept BTC/USDT as a convenience and resolve only to a USDT linear swap."""
        if not self._loaded:
            await self.load_markets()
        symbol = requested.strip().upper()
        if ":" not in symbol and "/" in symbol:
            base, quote = symbol.split("/", 1)
            symbol = f"{base}/{quote}:{quote}"
        market = self.exchange.markets.get(symbol)
        if not market or not market.get("swap") or not market.get("linear") or market.get("quote") != "USDT":
            raise ValueError(f"{requested} is not an active USDT linear perpetual market on WEEX.")
        return symbol

    async def fetch_ohlcv(self, requested: str, timeframe: str = "15m", limit: int = 300) -> list[list[float]]:
        if timeframe not in ("15m", "4h"):
            raise ValueError("Supported strategy timeframes are 15m and 4h.")
        symbol = await self.resolve_symbol(requested)
        candles = await asyncio.to_thread(self.exchange.fetch_ohlcv, symbol, timeframe, None, min(limit, 300))
        if not candles:
            raise RuntimeError(f"WEEX returned no candles for {symbol}")
        # Do not treat the currently-forming candle as confirmed.
        interval_ms = 15 * 60 * 1000 if timeframe == "15m" else 4 * 60 * 60 * 1000
        now_ms = self.exchange.milliseconds()
        return [c for c in candles if int(c[0]) + interval_ms <= now_ms]

    async def close(self) -> None:
        await asyncio.to_thread(self.exchange.close)
