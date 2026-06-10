# AK07 Multi-Index Trading Stack

AK07 is a Python-only, Redis-backed trading cockpit for Nifty 50, BankNifty, and Sensex.

## Production Modules

```text
src/server/src/app/services/cache_manager.py       Redis cache + system bias
src/server/src/mcp_server.py                       FastMCP context bridge
src/server/src/app/services/upstox_engine.py       Upstox strategy/execution engine
src/server/src/app/services/telegram_notifier.py   async Telegram alerts
src/server/src/app/ui/dashboard.py                 Streamlit cockpit
scripts/run_mock_cockpit.py                        local mock launcher
```

Legacy UI, broker, strategy sandbox, and backtest code has been removed.

## Quick Start: Mock Cockpit

No Redis. No broker credentials. No Telegram.

```powershell
pip install -r requirements.txt
python scripts/run_mock_cockpit.py
```

Open `http://localhost:8501`.

## Quick Start: Local Paper Stack

```powershell
pip install -r requirements.txt
docker run -d --name ak07-redis -p 6379:6379 redis:7-alpine
.\start.ps1
```

Endpoints:

- Streamlit cockpit: `http://localhost:8501`
- MCP bridge: `http://127.0.0.1:8765/mcp`
- Minimal API: `http://127.0.0.1:8080/api/health`

Useful launch modes:

```powershell
.\start.ps1 -Mock
.\start.ps1 -EngineOnly
.\start.ps1 -DashboardOnly -Mock
.\restart-api.ps1
```

## Runtime Data

Upstox credentials:

```text
src/server/data/users/AK07/upstox_credentials.json
```

Template:

```text
src/server/templates/upstox_credentials.example.json
```

Performance archives:

```text
src/server/src/app/archive/performance_review_<YYYY-MM-DD>.json
```

## Docker

```bash
cp configs/.env.example .env
docker compose -f configs/docker-compose.yml up -d --build
```

Services:

- `redis`
- `api`
- `engine`
- `mcp`
- `cockpit`

The cockpit is exposed on port `8501`; the host nginx example in `configs/host-nginx-ak07.conf.example` proxies `https://ak07.in` to that port.

## EC2 Deploy

First-time server setup:

```bash
sudo apt-get update
sudo apt-get install -y git docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Log out/in, then:

```bash
git clone <repo-url> ~/volume-order-block
cd ~/volume-order-block
cp configs/.env.example .env
nano .env
chmod +x configs/deploy-ec2.sh
./configs/deploy-ec2.sh
```

Deploy later from Windows:

```powershell
.\configs\deploy-manual-ec2.ps1 -Ec2Host "ak07.in" -KeyPath "C:\Users\pavan\arun\id_rsa"
```

Reset AK07 password on EC2:

```bash
docker compose -p ak07 -f configs/docker-compose.yml exec api \
  python scripts/reset_dashboard_password.py -p "NewPassword"
```

## More

See `docs/AK07_OPERATIONS.md` for day-to-day operation and scheduling.

Trading involves financial risk. Use paper mode until live behavior is verified during market hours.
