# Quick start (AK07)

1. **Dependencies:** `pip install -r requirements.txt` and `pip install -r src/server/requirements.txt`; in `src/client/`, run `npm install`.

2. **Server env:** Copy `src/server/.env.example` to `src/server/.env`. Set `JWT_SECRET` and `AK07_PASSWORD`.

3. **Run:** `start.bat` / `start.ps1`, or start API (`PYTHONPATH=src\server\src`, `uvicorn app.main:app`), Vite in `src/client/`, and `python src/bot/trading_bot.py`.

4. **Login:** Open the UI as **AK07**, save **Upstox credentials**.

5. **Docker:** Copy `configs/.env.example` to repo root `.env`, then `docker compose -f configs/docker-compose.yml up -d --build`.

Full detail: **`README.md`** at the repo root.
