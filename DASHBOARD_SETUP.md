# AK07 Dashboard Setup

## 1) Start FastAPI backend

Install dependencies:

- `python -m pip install -r dashboard_requirements.txt`

Run API:

- `uvicorn dashboard_api:app --host 0.0.0.0 --port 8000 --reload`

## 2) Start React dashboard

From `dashboard-ui`:

- `npm install`
- `npm run dev`

Open the URL shown by Vite (usually `http://localhost:5173`).

## 3) Bot integration behavior

The trading bot now pushes events in this format:

```json
{
  "id": "trade-id",
  "symbol": "NIFTY",
  "side": "BUY",
  "quantity": 65,
  "entry_price": 23200.0,
  "exit_price": null,
  "last_price": 23210.0,
  "unrealized_pnl": 650.0,
  "realized_pnl": null,
  "opened_at": "2026-03-15T09:30:00",
  "closed_at": null
}
```

Endpoints used by bot:

- `POST /api/trade/open` on entry
- `POST /api/trade/close` on exit
- `POST /api/trades/update-batch` for live MTM updates (batched each loop)

Weekly P&L source:

- `GET /api/dashboard/weekly-pnl` computes last 5 days directly from `orders.log`

The UI also listens to:

- `GET /api/dashboard/initial`
- `GET /api/dashboard/weekly-pnl`
- `WS /ws/trades`
