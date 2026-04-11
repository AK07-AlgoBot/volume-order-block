# AK07 — Zerodha Kite Connect authentication

Minimal, production-minded service that completes the **Kite Connect login flow** and stores a **signed server-side session** after exchanging `request_token` → `access_token`.

## Architecture

| Layer | Responsibility |
|--------|----------------|
| **`app/main.py`** | FastAPI app, **session middleware** (signed cookie), route mounting. |
| **`app/config.py`** | **Pydantic Settings** — loads `KITE_*` and `SESSION_SECRET` from environment (`.env`). |
| **`app/api/routes/kite_auth.py`** | HTTP: start OAuth, **callback**, `/me` (profile), logout. |
| **`app/services/kite_session.py`** | **KiteConnect** `generate_session` + `profile()` — no secrets in this layer beyond call parameters. |

### Security decisions

1. **Secrets only in environment** — never commit `.env`. Use `.env.example` as a template.
2. **API secret** is used **only** on the server to exchange `request_token`; it is **never** sent to the browser.
3. **Access token** is stored in a **server-side session** (Starlette `SessionMiddleware`, **signed** with `SESSION_SECRET`).
4. **HTTPS** in production: set `https_only=True` on session middleware when behind TLS (code comment in `main.py`).

### OAuth flow (Kite Connect)

1. User opens `/` → clicks **Login with Zerodha** → `GET /kite/start` → redirect to `https://kite.zerodha.com/connect/login?api_key=...&v=3`.
2. User logs in; Zerodha redirects to your **Redirect URL** with `request_token` (and `status`, `action`).
3. `GET /kite/callback` exchanges token via `kite.generate_session(request_token, api_secret)`.
4. Session cookie set; **`GET /me`** calls `kite.profile()` to prove the session works.

Redirect URL registered in the Kite developer console must match **exactly**, e.g.  
`http://127.0.0.1:8080/kite/callback`

## Setup

### 1) Python 3.11+

```bash
cd volume-order-block
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/macOS
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

### 3) Configure environment

```bash
copy .env.example .env   # Windows
# cp .env.example .env   # Linux/macOS
```

Edit `.env`:

- `KITE_API_KEY` / `KITE_API_SECRET` — from [Kite Connect](https://developers.kite.trade/) app.
- `KITE_REDIRECT_URL` — must match the app’s callback URL (default `http://127.0.0.1:8080/kite/callback`).
- `SESSION_SECRET` — long random string (e.g. `openssl rand -hex 32`).

**Never commit real API keys or secrets.** If a secret was ever pasted into chat or committed, **rotate it** in the Zerodha developer console.

### 4) Run the API

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

Open:

- **http://127.0.0.1:8080/** — home  
- **http://127.0.0.1:8080/docs** — OpenAPI  

## Verify login end-to-end

1. Start `uvicorn` on **port 8080** (must match Redirect URL).
2. In the browser go to **http://127.0.0.1:8080/**.
3. Click **Login with Zerodha** and complete Zerodha login.
4. You should land on a success HTML page.
5. Open **http://127.0.0.1:8080/me** — you should see **JSON** from `kite.profile()` (user id, name, email, etc.).
6. **http://127.0.0.1:8080/logout** clears the session; `/me` should then return **401**.

### API checks

```bash
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/me -b cookies.txt -c cookies.txt   # after browser login, copy session cookie or use browser only
```

The session cookie is **HttpOnly**-friendly via Starlette sessions; easiest verification is in the browser for `/me`.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Redirect mismatch | `KITE_REDIRECT_URL` or Zerodha app Redirect URL differs from `http://127.0.0.1:8080/kite/callback` (path/port/host). |
| `Invalid session` / exchange fails | Wrong API secret, or `request_token` already used (single-use). |
| Cannot reach callback | Firewall / wrong host — use `127.0.0.1` not `localhost` if that is what you registered. |

## License

MIT
