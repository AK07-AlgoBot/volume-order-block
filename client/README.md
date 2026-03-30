# AK07 Dashboard (client)

React + Vite app under `client/`.

## Run

1. `npm install`
2. `npm run dev`

The UI expects the API at `http://localhost:8000` (override with `VITE_DASHBOARD_API_BASE` in `.env` — see `.env.example`).

Sign in at `/login`. Demo users: `admin`/`admin`, `user-1`/`user-1`, … `user-5`/`user-5`. Admins can use **View as** to load another user’s trade data.
