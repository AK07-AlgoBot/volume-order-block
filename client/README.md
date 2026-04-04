# AK07 Dashboard (client)

React + Vite app under `client/`.

## Run

1. `npm install`
2. `npm run dev`

The UI talks to the API using `VITE_DASHBOARD_API_BASE` if set; otherwise it uses the current browser origin (works with the Docker nginx bundle that proxies `/api` and `/ws`).

Sign in at `/login` with username **AK07** and the password configured on the server (`AK07_PASSWORD` on first seed — see root `README.md`).
