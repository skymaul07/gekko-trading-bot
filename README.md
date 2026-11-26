# Gekko Bot: automated BTC trading bot (MetaTrader 5)

An automated trading bot for BTC/USDT on MetaTrader 5, written in Python. It runs 24/7, reads market data, detects the market regime, confirms signals with real-time order flow, and manages risk with several independent safety layers. Every event is reported to Telegram.

> **Public showcase version.** The proprietary entry rules and tuned parameters have been removed and replaced with simple placeholder strategies. The architecture, risk management and execution code are the real ones.

## What it does

- **Market data:** pulls H1 and M15 candles from MetaTrader 5.
- **Regime detection:** classifies the market as trending up, trending down or ranging (EMA50 vs EMA200 + ADX), with an optional AI supervisor (DeepSeek API).
- **Order-flow confirmation:** a background thread polls Kraken's public trades API and computes Cumulative Volume Delta (CVD) over a rolling window.
- **News filter:** reads the weekly economic calendar and blocks new trades around high-impact USD news.
- **Execution:** sends market orders with stop loss and take profit, picking the broker's supported filling mode.
- **Telegram reporting:** startup, hourly heartbeat, every open/close with P&L, and risk alerts.

## Risk management

| Layer | What it does |
|---|---|
| Daily breaker | Stops trading for the day after a set loss |
| Audit pause | Locks the bot after a 15% drawdown until manual review |
| Kill switch | Shuts the bot down after a 30% drawdown |
| Anti re-entry | 30-minute cooldown after a close and a minimum price distance from recent entries |
| Startup cooldown | 5-minute warm-up so a restart can't trigger duplicate entries |
| Persistent state | Saves daily P&L and entry history to JSON so limits survive restarts |

## Tech

Python · MetaTrader5 API · pandas · requests · threading · REST APIs (Kraken, Telegram, DeepSeek)

## Run it (demo account only)

```bash
pip install -r requirements.txt
# copy .env.example to .env and fill in your own values, then:
python gekko_bot.py
```

Requires MetaTrader 5 on Windows, logged into a demo account.

## Disclaimer

Educational project. Not financial advice. Use only on demo accounts.

## License

Copyright (c) 2026 Manuel Rosales Naveda. All rights reserved. See [LICENSE](LICENSE).
