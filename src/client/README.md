# AK07 Dashboard (client)

React + Vite app under `src/client/`.

## Run

1. `npm install`
2. `npm run dev`

The UI uses `VITE_DASHBOARD_API_BASE` when set; otherwise it uses the browser origin (Docker nginx proxies `/api` and `/ws`).

Sign in at `/login` as **AK07** (see root `README.md`).
