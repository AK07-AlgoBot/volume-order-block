# Multi-Script Trading Bot v2.0 (AK07)

Automated trading bot for Indian markets using Upstox API v2 with an EMA crossover strategy, plus a FastAPI + React dashboard. This branch is **single-tenant**: dashboard login is **AK07** only; data and Upstox credentials live under `server/data/users/AK07/`.

## Features

- **Real-time market data**: Historical + intraday 5-minute candles  
- **EMA crossover strategy**: Short and long exponential moving averages  
- **Multi-script support**: NIFTY, BANKNIFTY, SENSEX, FINNIFTY (configurable)  
- **Risk management**: Portfolio stop loss and trailing stop per position  
- **State persistence**: Trading state survives restarts  
- **Dashboard**: JWT login (AK07), Upstox credentials, live trade updates  

## Requirements

- Python 3.8+ (bot + local API)
- Node.js 20+ (for the `client/` dashboard)
- Docker + Compose (optional production stack)
- Active Upstox Pro account and API access token

## Repository layout

| Path | Role |
|------|------|
| `client/` | React + Vite UI (`npm install`, `npm run dev` / `npm run build`) |
| `server/src/app/` | FastAPI app (`PYTHONPATH=server/src`, `uvicorn app.main:app`) |
| `server/data/` | Runtime data (gitignored): `users_auth.json`, `users/AK07/` |
| `deploy/docker/` | `Dockerfile.api`, `Dockerfile.ui`, nginx config for the UI container |
| `trading_bot.py` | Trading worker for AK07; posts to the API with `X-Trading-User: AK07` and `X-Bot-Token` when required |

### Root-level Python modules

Shared between the API and the bot (imports from repo root):

| Path | Role |
|------|------|
| `trading_bot.py` | Bot entrypoint (AK07 only). |
| `upstox_credentials_store.py` | Paths + read/write for `server/data/users/AK07/upstox_credentials.json`. |
| `bot_process_control.py` | Start/stop/recycle bot process; used by the API after credential save. |
| `trading_preferences_store.py`, `trading_script_constants.py` | Symbol scope for the bot. |
| `archive_day.py` | Archives logs/state (uses `TRADING_USER` env; use `AK07`). |
| `scripts/` | Standalone tools. Run from repo root. |
| `requirements.txt` | Bot + scripts; API also uses `server/requirements.txt`. |
| `docker-compose.yml` | **api** + **web** (nginx + static build, proxies `/api/` and `/ws/` to the API). |

**Legacy files at repo root** (`trading_bot.log`, `orders.log`, root `upstox_credentials.json`, etc.) are ignored or safe to delete; live data belongs under `server/data/users/AK07/`.

## Authentication

- Single dashboard user: **`AK07`**. Password is seeded from **`AK07_PASSWORD`** when `users_auth.json` is first created (see `server/src/app/services/users_store.py`). After that, rotate by updating the file on disk or removing it once to re-seed.
- **JWT:** Set `JWT_SECRET` in `.env` (see `server/.env.example` for local uvicorn, or repo root `.env.example` for Docker).

## Installation (local dev)

```bash
pip install -r requirements.txt
pip install -r server/requirements.txt

cd client && npm install && npm run dev

# Terminal: API (from repo root)
set PYTHONPATH=server\src
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Terminal: bot
python trading_bot.py
```

On Windows, `start.bat` / `start.ps1` can still launch pieces; ensure `TRADING_USER`/env matches AK07 where used.

## Running the bot

- The bot runs **only for AK07** and reads `server/data/users/AK07/upstox_credentials.json`.
- **API posts:** Send `X-Trading-User: AK07` and `X-Bot-Token` matching `BOT_API_TOKEN` when not calling from loopback (required in production / Docker).

## Docker (production-style)

1. Copy **`.env.example`** to **`.env`** at the repo root and set `JWT_SECRET`, `AK07_PASSWORD`, `BOT_API_TOKEN`, and `DASHBOARD_CORS_ORIGINS` as needed.
2. `docker compose up -d --build`  
   - **api** on the internal network (port 8000 not published by default).  
   - **web** on **host `8080`** → nginx serves the SPA and proxies API/WebSocket to `api`.

For **https://ak07.in** (server example **204.168.232.148**), terminate TLS on the **host** with nginx (or another edge) and `proxy_pass` to `127.0.0.1:8080`. See `deploy/host-nginx-ak07.conf.example`.

The UI build uses **same-origin** API URLs by default (empty `VITE_DASHBOARD_API_BASE`), so the browser talks to `/api/...` and `/ws/...` through the web container.

## GitHub Actions → EC2

Workflow: **`.github/workflows/deploy-ec2.yml`** (on push to **`AK07`**).

Configure repository **secrets**: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `DEPLOY_PATH`. On the instance, install Docker, clone this repo to `DEPLOY_PATH`, create `.env`, then pushes will run `git pull` and `docker compose up -d`.

## Configuration

Edit `TRADING_CONFIG` in `trading_bot.py` (scripts, interval, EMAs, stops, loop interval).

## Trading strategy (summary)

- **Entry:** EMA(short) crosses above (BUY) or below (SELL) EMA(long) as configured  
- **Exit:** Opposite crossover, trailing stop, or portfolio stop  

## Files under `server/data/users/AK07/`

- `logs/trading_bot.log`, `logs/orders.log`, `logs/market_status.log`  
- `trading_state.json`  
- `trading_preferences.json` — symbol subset; configured in the dashboard  
- `upstox_credentials.json` — Upstox tokens (via dashboard)  

Audit JSON lines: `server/data/logs/audit/<actor>/actions.log`.

## Helper scripts

```bash
python scripts/fetch_ob_snapshot.py --user AK07 --scripts CRUDE NIFTY --json
python scripts/analyze_trade_patterns.py --user AK07
```

## Production notes

Live order placement may be disabled for safety; enable only after thorough testing.

## License

MIT License — use at your own risk. Trading involves financial risk.

**Disclaimer:** Educational purposes. Past performance does not guarantee future results.
