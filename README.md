# WEEX Telegram Crypto Signal Bot

A signals-only Python bot intended to scan eligible WEEX futures markets on 15-minute candles and send strategy alerts to Telegram.

## Current status

This repository is the project scaffold. The strategy port and live market-data integration must be tested against the original Pine Script before signals should be treated as equivalent. **No exchange order placement is implemented or enabled.**

## Planned components

- Discover eligible futures symbols from public market metadata (target: 300+ symbols if WEEX makes that many available).
- Fetch 15-minute OHLCV candles.
- Port the supplied Pine Script logic without adding new entry filters or changing its rules.
- Send signal messages to Telegram.
- Add tests for key calculations and candle-close behavior.

## Configuration

Set these environment variables in Render (do not commit real tokens):

- `TELEGRAM_BOT_TOKEN`: token from BotFather.
- `TELEGRAM_CHAT_ID`: destination chat or channel ID.
- `SCAN_INTERVAL_SECONDS`: scan interval (default 60).

## Local setup

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Copy `.env.example` to `.env` for local development and populate it with your own values. Do not upload secrets to GitHub.

## Deployment

The included `render.yaml` describes a Render Background Worker. Configure the secret environment variables in the Render dashboard before starting it.

## Safety

This project is for alerts only. It must not submit real orders or require exchange trading API keys. Verify the strategy port against TradingView historical results before relying on alerts.
