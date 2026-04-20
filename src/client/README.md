# AK07 Dashboard (client)

React + Vite app under `src/client/`.

## Run

1. Start the API on **8080** (see repo root `README.md`: `uvicorn` with `PYTHONPATH=src\server\src`).
2. From this folder: `npm install` then `npm run dev`.
3. Open **http://localhost:5173** (or **http://127.0.0.1:5173**).

In development, the app calls `/api` on the same host as the Vite server; `vite.config.js` proxies `/api` and `/ws` to `127.0.0.1:8080`, so you avoid CORS problems. Set `VITE_DASHBOARD_API_BASE` only if you want to talk to the API directly (no proxy).

Production/Docker: the UI is usually served behind nginx, which proxies `/api` and `/ws` the same way.

Sign in at `/login` as **AK07** (see root `README.md`).
