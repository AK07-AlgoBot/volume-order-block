# Multi-Script Trading Bot v2.0

Automated trading bot for Indian markets using Upstox API v2 with an EMA crossover strategy, plus a FastAPI + React dashboard for auth, per-user broker credentials, and trade visibility.

## Features

- **Real-time market data**: Historical + intraday 5-minute candles  
- **EMA crossover strategy**: Short and long exponential moving averages  
- **Multi-script support**: NIFTY, BANKNIFTY, SENSEX, FINNIFTY (configurable)  
- **Risk management**: Portfolio stop loss and trailing stop per position  
- **State persistence**: Trading state survives restarts (per user)  
- **Dashboard**: JWT login, per-user Upstox credentials, live trade updates  

## Requirements

- Python 3.8+
- Node.js (for the `client/` dashboard)
- Active Upstox Pro account and API access token

## Repository layout

| Path | Role |
|------|------|
| `client/` | React + Vite UI (`npm install`, `npm run dev` / `npm run build`) |
| `server/src/app/` | FastAPI app (`PYTHONPATH=server/src`, `uvicorn app.main:app`) |
| `server/data/` | Runtime data (gitignored): `users_auth.json`, per-user dirs under `users/<username>/` |
| `server/templates/upstox_credentials.example.json` | Example shape for `upstox_credentials.json` when creating a user file by hand |
| `trading_bot.py` | Trading worker; posts to the API with `X-Trading-User` and optional `X-Bot-Token` |

### Root-level (outside `client/` and `server/`)

These stay at the repo root so `python trading_bot.py` and `PYTHONPATH=server/src` imports stay simple (shared modules are imported from the project root).

| Path | Role |
|------|------|
| `trading_bot.py` | Bot entrypoint (multi-account loop). |
| `upstox_credentials_store.py` | Paths + read/write for per-user `server/data/users/<user>/upstox_credentials.json` (used by bot and API). |
| `bot_process_control.py` | Start/stop/recycle bot process; imported by the FastAPI app. |
| `archive_day.py` | Moves a user’s logs/state into `archive/<timestamp>/` (uses `TRADING_USER` env). |
| `scripts/` | Standalone tools (`fetch_ob_snapshot.py`, trade analysis). Run from repo root. |
| `requirements.txt` | `pip install -r` for bot + scripts (API deps are `server/requirements.txt`). |
| `docker-compose.yml` | Optional local stack: API container + nginx for `client/dist`. |
| `start.bat`, `start.ps1` | Launch API, Vite client, and/or bot on Windows. |
| `archive/` | Runtime daily archives produced by `archive_day.py` / shutdown (mostly **gitignored**; only `archive/unused/` is tracked). |
| `README.md`, `QUICKSTART.md`, `DASHBOARD_SETUP.md`, `CHANGELOG.md`, `STRATEGY_LOGIC.md` | Documentation. |

**Noise you may see at repo root:** `trading_bot.log`, `orders.log`, `market_status.log`, `trading_state.json`, `trading_bot.lock`, or `upstox_credentials.json` are **legacy or leftover** if the bot was run with an old layout, or copy-paste errors. Active per-user data belongs under `server/data/users/<user>/`. Those patterns are in `.gitignore`; delete stray root copies if you do not need them.

**Demo users** (bcrypt-hashed in `server/data/users_auth.json`): `admin` / `admin`, `user-1` / `user-1`, … `user-5` / `user-5`. Admins can use **View as** to inspect another user’s trades.

**Environment:** Copy `server/.env.example` to `server/.env` and set `JWT_SECRET`. For the bot calling the API from a non-localhost host, set `BOT_API_TOKEN` on the server and the same value in the bot’s environment.

**Logs:** Order and bot logs live under `server/data/users/<username>/logs/` (e.g. `orders.log`, `trading_bot.log`). Audit JSON lines rotate under `server/data/logs/audit/<actor>/actions.log`.

## Installation

```bash
pip install -r requirements.txt
pip install -r server/requirements.txt

cd client && npm install && npm run dev

# From repo root — multi-account bot (see “Running the bot”)
python trading_bot.py
```

## Running the bot

- **Which accounts:** Set `TRADING_USERS=admin,user-1,user-2` (comma-separated). If unset, usernames are taken from `users_auth.json` (all known users). Users without a saved Upstox token in `server/data/users/<user>/upstox_credentials.json` are skipped.
- **API posts:** The bot sends `X-Trading-User` (per account) and `X-Bot-Token` when `BOT_API_TOKEN` is set. From localhost, the API may accept bot posts without the token for local development (see server security settings).

**Windows:** `start.bat` or `.\start.ps1` starts the API, UI, and bot; `start.bat -BotOnly` runs only the bot.

## Configuration

Edit `TRADING_CONFIG` in `trading_bot.py` (scripts, interval, EMAs, stops, loop interval).

## Trading strategy (summary)

- **Entry:** EMA(short) crosses above (BUY) or below (SELL) EMA(long) as configured  
- **Exit:** Opposite crossover, trailing stop, or portfolio stop  

## Files generated (per user)

Under `server/data/users/<username>/`:

- `logs/trading_bot.log` — operational log (errors, signals, EOD, VERIFY lines). Also echoed to **stdout** with a `[username]` prefix when multiple accounts run in one process (files stay separate; only the console is shared).
- `logs/orders.log` — **only** structured trade lines (`ACTION=ENTRY|EXIT|SKIP|…`) for the dashboard and scripts; not a copy of `trading_bot.log`.
- `logs/market_status.log` — optional per-loop EMA/signal snapshot per script (similar information appears in the colored console table). Set `TRADING_BOT_WRITE_MARKET_STATUS_LOG=0` to disable this file.
- `trading_state.json` — open positions / persisted state  
- `trading_preferences.json` — optional subset of symbols to trade today (`enabled_scripts`); `null` means all instruments from `TRADING_CONFIG`. Set in the dashboard **Symbols to trade** card; the bot reloads it every loop.

A stray `orders.log` at the **repo root** is legacy; the bot only writes under `server/data/users/<user>/logs/`.

## API credentials

Upstox tokens are **per dashboard user**, stored at:

`server/data/users/<username>/upstox_credentials.json` (gitignored).

Users save credentials in the UI; admins can save on behalf of another user. To seed a file without the UI, copy `server/templates/upstox_credentials.example.json` to that path and edit.

**Optional:** `DASHBOARD_ADMIN_TOKEN` for legacy admin flows; `DASHBOARD_CORS_ORIGINS` if the UI is not on `localhost:5173`.

**Bot recycle on save:** After saving Upstox credentials, the API can restart `trading_bot.py` using `trading_bot.lock`. Set `DASHBOARD_RESTART_BOT_ON_SAVE=0` to disable. For systemd, set `DASHBOARD_SYSTEMD_UNIT=your-bot.service`. Override Python with `TRADING_BOT_PYTHON` if needed.

## Helper scripts

OB% snapshot (uses the same candle logic as the bot; credentials for `--user`):

```bash
python scripts/fetch_ob_snapshot.py
python scripts/fetch_ob_snapshot.py --json
python scripts/fetch_ob_snapshot.py --user user-1 --scripts CRUDE NIFTY --json
```

Analysis scripts accept `--user` (default `user-1`) for log paths under `server/data/users/<user>/`:

```bash
python scripts/analyze_trade_patterns.py --user user-1
python scripts/trade_probability_report.py --user user-1
```

## Production notes

Live order placement in code is intentionally disabled / commented for safety. Only enable after thorough testing.

## License

MIT License — use at your own risk. Trading involves financial risk.

**Disclaimer:** Educational purposes. Past performance does not guarantee future results.
