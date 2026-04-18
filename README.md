# app.bot-paper

Paper trading bot — simulated strategies only, no real money at risk.

## Safety by design

This repo **physically cannot place live orders** — it has no `hyperliquid_executor.py`, no `eth_account` dependency, no API secret usage. Even if you accidentally pointed real keys at it, there's no code path to submit an order to the exchange.

## Setup (Mac)

```bash
git clone https://github.com/GitMatttt/APP.BOT-PAPER.git ~/bot
cd ~/bot
pip3 install -r requirements.txt
```

## Run

Two terminals:

```bash
# Terminal 1 — the bot
python3 paper_trader.py
```

```bash
# Terminal 2 — the dashboard
python3 paper_web.py
```

Then open http://localhost:8000

## What's inside

- `paper_trader.py` — simulates 39 strategies, checks 8H candles, records trades. Reads live prices from Hyperliquid public API (read-only, no keys).
- `paper_web.py` — lean FastAPI dashboard that reads `data/paper_trading/state.json`.
- `templates/paper.html` — dashboard UI.

## Strategies

31 conservative (TV-verified) + 8 aggressive (backtested 2021+):
- **1000%+ tier:** SOL_moon, AVAX_moon
- **500%+ tier:** ETH_moon, NEAR_moon, kPEPE_moon
- **400%+ tier:** DOGE_yolo, kBONK_moon, HYPE_moon

See `paper_trader.py` STRATEGIES dict for all configs.
