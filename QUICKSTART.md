# Quick start (AK07)

1. **Dependencies:** `pip install -r requirements.txt` and `pip install -r server/requirements.txt`; in `client/`, run `npm install`.

2. **Server env:** Copy `server/.env.example` to `server/.env`. Set `JWT_SECRET` and `AK07_PASSWORD` (used when `users_auth.json` is first created).

3. **Run:** `start.bat` / `start.ps1`, or manually start the API (`PYTHONPATH=server\src`, `uvicorn app.main:app --host 127.0.0.1 --port 8000`), Vite in `client/`, and `python trading_bot.py`.

4. **Login:** Open the UI, sign in as **AK07** with your password, then save **Upstox credentials** in the dashboard.

5. **Docker:** Copy root `.env.example` to `.env`, then `docker compose up -d --build`. TLS for https://ak07.in is documented in `README.md` and `deploy/host-nginx-ak07.conf.example`.

For full detail, see **`README.md`**.
