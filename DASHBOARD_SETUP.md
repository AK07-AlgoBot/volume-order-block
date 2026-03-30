# AK07 Dashboard Setup

## 1) Start FastAPI backend

```powershell
pip install -r server/requirements.txt
$env:PYTHONPATH = "$PWD\server\src"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Copy `server/.env.example` to `server/.env` and set `JWT_SECRET` (and `BOT_API_TOKEN` if the bot runs off localhost).

## 2) Start React dashboard

From `client/`:

```powershell
npm install
npm run dev
```

Open the URL shown by Vite (usually `http://localhost:5173`). Sign in (e.g. `admin` / `admin`).

## 3) Bot integration

The bot runs one **multi-account** process: set `TRADING_USERS=admin,user-1,...` or omit it to use all usernames from `users_auth.json`. Each loop posts with `X-Trading-User` for that account and `X-Bot-Token` when `BOT_API_TOKEN` is set.

Endpoints:

- `POST /api/trade/open` on entry
- `POST /api/trade/close` on exit
- `POST /api/trades/update-batch` for live MTM updates (batched each loop)

Weekly P&L and closed trades are derived from `server/data/users/<user>/logs/orders.log` (and that user’s archived day folders under `archive/`).

The UI uses JWT-authenticated `GET /api/dashboard/*` and `WS /ws/trades?token=...` (optional `view_as=` for admin).
