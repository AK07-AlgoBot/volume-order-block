# Dashboard setup (AK07 branch)

Single-tenant stack: only **`AK07`** can sign in. See the root **`README.md`** for environment variables, Docker, and EC2 deployment.

Local quick path:

1. `pip install -r requirements.txt` and `pip install -r server/requirements.txt`
2. Copy `server/.env.example` → `server/.env`, set `JWT_SECRET` and `AK07_PASSWORD`
3. `cd client && npm install && npm run dev`
4. From repo root: `set PYTHONPATH=server\src` then `uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`
5. Open the Vite URL (usually `http://localhost:5173`) and log in as **AK07**

The bot posts trades with `X-Trading-User: AK07` and `X-Bot-Token` when `BOT_API_TOKEN` is set on the API.
