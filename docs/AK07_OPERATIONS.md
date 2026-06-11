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
