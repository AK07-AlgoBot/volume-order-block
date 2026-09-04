# AK07 Operations

AK07 is now a Python-only multi-index trading stack:

- `src/server/src/app/services/cache_manager.py` - Redis cache and system bias keys.
- `src/server/src/mcp_server.py` - FastMCP bridge for local LLM access.
- `src/server/src/app/services/upstox_engine.py` - Nifty, BankNifty, Sensex engine.
- `src/server/src/app/services/telegram_notifier.py` - async Telegram alerts.
- `src/server/src/app/ui/dashboard.py` - Streamlit cockpit.

## Local visual check

Run this when you want the cockpit without Redis, Upstox, or Telegram:

```powershell
python scripts/run_mock_cockpit.py
```

This sets `AK07_MOCK=1`, uses in-process `fakeredis`, and seeds simulated market data on every dashboard refresh.

## OrderFlowMap (Upstox live heatmap)

**Production:** https://ak07.in/api/ofmap/ → Live → Connect  
WS: `wss://ak07.in/api/ofmap/ws` (uses server Upstox token — no local IP).

Works under existing nginx `/api/` proxy (no extra location required).  
Optional pretty URL `/ofmap/` if you add that block from `configs/host-nginx-ak07.conf.example`.  
Details: `scripts/orderflow/SETUP_OFMAP.md`.

```bash
git pull origin AK07-Model
docker compose -p ak07 -f configs/docker-compose.yml up -d --build api
# update nginx from configs/host-nginx-ak07.conf.example, then:
sudo nginx -t && sudo systemctl reload nginx
```

## Local live-paper stack

```powershell
pip install -r requirements.txt
docker run -d --name ak07-redis -p 6379:6379 redis:7-alpine
.\start.ps1
```

Services:

- Cockpit: `http://localhost:8501`
- MCP: `http://127.0.0.1:8765/mcp`
- Minimal API health: `http://127.0.0.1:8080/api/health`

Use `.\start.ps1 -Mock` to launch the same stack with mock data.

## Credentials

Upstox credentials live outside source code:

```text
src/server/data/users/AK07/upstox_credentials.json
```

Use `src/server/templates/upstox_credentials.example.json` as the shape. The engine also attempts the Upstox V3 token-request flow daily at **08:45 IST** (`AK07_TOKEN_REFRESH_IST`) using `api_key` and `api_secret`.

### Upstox auto token (V3 notifier webhook)

API-only apps need a **Notifier Webhook Endpoint** in the [Upstox My Apps](https://account.upstox.com/developer/apps) form:

```text
https://ak07.in/api/upstox/token-notifier
```

Requirements:

1. **FastAPI `api` container** must be running (port `8080`).
2. **nginx** must proxy `/api/` to `127.0.0.1:8080` (see `configs/host-nginx-ak07.conf.example`).
3. Notifier URL in Upstox portal must match exactly (HTTPS, no trailing slash).
4. After `refresh_upstox_token.py` succeeds, **approve the request** in the Upstox app/WhatsApp; Upstox POSTs the token to the webhook.

Verify from the server:

```bash
curl -s https://ak07.in/api/upstox/token-notifier
curl -s https://ak07.in/api/health
docker compose -p ak07 -f configs/docker-compose.yml exec engine python scripts/refresh_upstox_token.py
```

If you see `UDAPI1123 Invalid notifier url`, the portal URL is wrong or `/api/` is not reaching FastAPI. Until fixed, paste today's token manually into `upstox_credentials.json`.

### Kite Connect (Zerodha) OAuth

Create a **Personal** app on [developers.kite.trade](https://developers.kite.trade/) (login with your Zerodha credentials).

**Redirect URL** (register exactly — HTTPS, no trailing slash):

```text
https://ak07.in/api/brokers/kite/callback
```

Requirements:

1. **`api` container** running and **`ak07-redis`** healthy (OAuth state is stored in Redis).
2. **nginx** proxies `/api/` → `127.0.0.1:8080` (see `configs/host-nginx-ak07.conf.example`).
3. **`.env` on the server** (repo root):

```bash
PRODUCTION_DOMAIN=ak07.in
AK07_COCKPIT_URL=https://ak07.in
AK07_API_PUBLIC_URL=https://ak07.in
# Optional override if auto-detect is wrong:
# KITE_REDIRECT_URL=https://ak07.in/api/brokers/kite/callback
```

4. Per-user credentials file (created after first save from cockpit):

```text
src/server/data/users/AK07/kite_credentials.json
```

Shape: `src/server/templates/kite_credentials.example.json` (`api_key`, `api_secret`, `access_token`, `base_url`).

**Daily flow (browser):**

1. Sign in at `https://ak07.in` → **Token Update**.
2. Select broker **kite** (admin) or use profile default broker `kite`.
3. **Step 1** — paste `api_key` + `api_secret` from [developers.kite.trade](https://developers.kite.trade/) → Save.
4. **Step 2** — click **Login to Zerodha** → complete User ID + password + TOTP in the new tab.
5. Zerodha redirects to `/api/brokers/kite/callback`; AK07 exchanges `request_token` → `access_token` and saves to `kite_credentials.json`.

Verify from the server:

```bash
curl -s https://ak07.in/api/health
docker compose -p ak07 -f configs/docker-compose.yml exec api \
  python -c "from app.services.kite_oauth import kite_redirect_url; print(kite_redirect_url())"
```

Expected output: `https://ak07.in/api/brokers/kite/callback`

If login opens but callback fails, check `docker compose -p ak07 -f configs/docker-compose.yml logs api` and confirm the redirect URL in the Kite app matches exactly.

### SEBI egress proxy (502 Bad Gateway on Kite/Upstox/Groww)

Users with a dedicated **`egress_ip`** in Admin → Users send broker API calls through a host-side CONNECT proxy (`scripts/egress_bind_proxy.py`). If the proxy is down or the secondary IP is missing from the host, you see:

`ProxyError … Tunnel connection failed: 502 Bad Gateway`

**On the server:**

```bash
cd ~/volume-order-block

# 1. Diagnose (from host or api container)
python3 scripts/diagnose_egress_proxy.py
python3 scripts/diagnose_egress_proxy.py --user Kesavulu

# 2. Egress proxy systemd units (one per secondary IP)
systemctl status ak07-egress-proxy ak07-egress-proxy-95
systemctl restart ak07-egress-proxy ak07-egress-proxy-95

# 3. Secondary IPs must exist on eth0
ip -4 addr | grep -E '65.109.255.239|95.216.179.8'

# 4. Test proxy tunnel from host
curl -v -x http://127.0.0.1:18901 https://api.kite.trade/

# 5. Docker bridge gateway must match .env (usually 172.19.0.1)
ip -4 addr | grep br-
grep EGRESS ~/volume-order-block/.env
```

**`.env` on server (example):**

```env
AK07_EGRESS_PROXY=http://172.19.0.1:18901
AK07_EGRESS_PROXY_MAP=65.109.255.239=http://172.19.0.1:18901,95.216.179.8=http://172.19.0.1:18902
```

**Quick workaround** (if secondary IP is not on this VPS): Admin → Users → clear **Egress IP** for that user so they use the primary IP (only if SEBI allows).

### Groww Trade API (order placement)

Groww uses **server-side token exchange** (no browser redirect). Subscribe on [Groww Trade API](https://groww.in/trade-api) and generate **API key + secret** on Groww Cloud.

Credentials file:

```text
src/server/data/users/AK07/groww_credentials.json
```

**Daily flow (browser):**

1. Sign in at `https://ak07.in` → **Token Update** → broker **Groww**.
2. **Step 1** — save `api_key` + `api_secret` (one-time).
3. **Step 2** — click **Generate Groww access token** → approve on the **Groww mobile app** when using approval mode.
4. Optional: use **TOTP mode** with a 6-digit authenticator code instead.
5. **Test Groww connection** — should show your UCC and active segments.

Token expires daily (~6:00 AM IST). AK07 uses Groww for **order credentials**; market data still comes from Upstox unless you wire a Groww engine later.

Verify API route:

```bash
curl -s https://ak07.in/api/health
docker compose -p ak07 -f configs/docker-compose.yml exec api \
  python -c "from groww_credentials_store import read_credentials_file_for_user; print(read_credentials_file_for_user('AK07').get('api_key','')[:8])"
```

## Docker

```bash
cp configs/.env.example .env
docker compose -f configs/docker-compose.yml up -d --build
```

Compose services:

- `redis`
- `api`
- `engine`
- `mcp`
- `cockpit`

Exposed ports:

- `8501` - Streamlit cockpit
- `8080` - minimal FastAPI health/auth/credential API
- `8765` - MCP streamable HTTP endpoint

## EC2 Deployment

First-time setup on a fresh Ubuntu EC2:

```bash
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out and back in so Docker group membership applies, then:

```bash
git clone <repo-url> ~/volume-order-block
cd ~/volume-order-block
cp configs/.env.example .env
nano .env
chmod +x configs/deploy-ec2.sh
./configs/deploy-ec2.sh
```

Manual deploy from Windows:

```powershell
.\configs\deploy-manual-ec2.ps1 -Ec2Host "ak07.in" -Ec2User "ubuntu" -RemotePath "/home/ubuntu/volume-order-block" -KeyPath "C:\Users\pavan\arun\id_rsa"
```

GitHub Actions deploy:

- Configure repository secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `DEPLOY_PATH`.
- Push to branch `AK07`.
- The workflow runs `configs/deploy-ec2.sh` on the server.

Post-deploy checks:

```bash
docker compose -p ak07 -f configs/docker-compose.yml ps
docker compose -p ak07 -f configs/docker-compose.yml logs -f engine
docker compose -p ak07 -f configs/docker-compose.yml logs -f cockpit
```

Reset the AK07 password inside the persistent Docker volume:

```bash
docker compose -p ak07 -f configs/docker-compose.yml exec api \
  python scripts/reset_dashboard_password.py -p "NewPassword"
```

Create the Upstox credential file on the server:

```bash
docker compose -p ak07 -f configs/docker-compose.yml exec api sh -lc \
  'mkdir -p src/server/data/users/AK07 && cp src/server/templates/upstox_credentials.example.json src/server/data/users/AK07/upstox_credentials.json'
```

Then edit it:

```bash
docker compose -p ak07 -f configs/docker-compose.yml exec api \
  python -c "from pathlib import Path; print(Path('src/server/data/users/AK07/upstox_credentials.json').resolve())"
```

Persistent volumes:

- `ak07_ak07_server_data` - dashboard auth and Upstox credentials.
- `ak07_ak07_redis_data` - Redis state.

## Daily lifecycle

- **08:45 IST**: engine requests/refreshes Upstox V3 token and warms Redis (`AK07_TOKEN_REFRESH_IST`).
- 14:55 IST: entries stop and active positions are squared off.
- 15:30 IST: performance JSON archive is written under `src/server/src/app/archive/`.

For EC2 cron scheduling:

```bash
chmod +x configs/install-engine-schedule.sh
./configs/install-engine-schedule.sh
```
