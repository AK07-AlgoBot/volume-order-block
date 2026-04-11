# Dashboard setup (AK07)

See the root **`README.md`** for the folder layout and environment variables.

Local path:

1. `pip install -r requirements.txt` and `pip install -r src/server/requirements.txt`
2. Copy `src/server/.env.example` → `src/server/.env` (`JWT_SECRET`, `AK07_PASSWORD`)
3. `cd src/client && npm install && npm run dev`
4. From repo root: `set PYTHONPATH=src\server\src` then `python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8080` (match your Kite redirect URL port)
5. Open the Vite URL and sign in as **AK07**

The bot uses `X-Trading-User: AK07` and `X-Bot-Token` when `BOT_API_TOKEN` is set on the API.
