# AK07 Trading stack

EMA / Upstox trading bot, FastAPI backend, and React dashboard. **Single user: AK07.**

## Project layout

```text
volume-order-block/
├── src/
│   ├── client/          # React + Vite UI
│   ├── server/          # FastAPI app + runtime data (src/server/data/)
│   ├── bot/               # trading_bot.py, archive_day.py, bot_process_control.py
│   ├── lib/               # Shared Python: credentials, preferences, script constants
│   └── scripts/           # Analysis & snapshot helpers
├── configs/               # Docker, compose, env template, nginx examples
├── docs/                  # QUICKSTART, setup, changelog, strategy notes
├── .github/workflows/     # CI / deploy (must stay at repo root for GitHub)
├── requirements.txt       # Bot + scripts (pandas, …)
├── README.md
└── start.ps1 / start.bat  # Local Windows launcher
```

`.github/` cannot be moved under `src/` — GitHub only runs workflows from the repository root.

If you still have an old **`server/data/`** tree from before this layout, move it to **`src/server/data/`** so auth and logs keep working.

## Quick commands

**Python / API**

```bash
pip install -r requirements.txt
pip install -r src/server/requirements.txt
set PYTHONPATH=src\server\src
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

**UI**

```bash
cd src/client && npm install && npm run dev
```

**Bot**

```bash
python src/bot/trading_bot.py
```

**Docker** (from repo root)

```bash
copy configs\.env.example .env   # then edit .env
docker compose -f configs/docker-compose.yml up -d --build
```

- **API** image: `src/server`, `src/lib`, `src/bot` under `/app`; data volume → `/app/src/server/data`.
- **Web** image: static UI + nginx proxy to the API (`web` publishes **8080**).
- TLS for **https://ak07.in**: use host nginx → `127.0.0.1:8080`; see `configs/host-nginx-ak07.conf.example`.

## Auth & secrets

- Dashboard login: **AK07**; password seed **`AK07_PASSWORD`** (see `src/server/src/app/services/users_store.py`).
- **`JWT_SECRET`**, **`BOT_API_TOKEN`**: set in `src/server/.env` (local API) or repo root **`.env`** (Docker).

## Deploy (GitHub Actions)

Push branch **`AK07`** runs `.github/workflows/deploy-ec2.yml`. Secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `DEPLOY_PATH`. On the server, use repo root **`.env`** and:

`docker compose -f configs/docker-compose.yml up -d`

## Deploy (Option B — SSH from your PC)

**Do not commit private keys** (`.pem`, `id_rsa`, etc.) to git.

1. On EC2 (once): install Docker + Compose + Git; clone this repo to e.g. `/home/ubuntu/volume-order-block`; checkout **`AK07`**; copy **`configs/.env.example`** to repo root **`.env`** and set secrets.
2. From Windows (repo root), after pushing your latest commits to `origin`:

```powershell
.\configs\deploy-manual-ec2.ps1 -Ec2Host "YOUR_PUBLIC_IP_OR_DNS" -KeyPath "C:\Users\pavan\arun\id_rsa"
```

Adjust **`-Ec2User`** and **`-RemotePath`** if your server layout differs.

## Runtime data persistence (EC2 + Docker)

- Runtime trading data (`orders.log`, `paper_orders.log`, archives, state) is stored in the Docker volume mounted at `/app/src/server/data` inside containers.
- On this server, that volume maps to host path: `/var/lib/docker/volumes/configs_ak07_server_data/_data`.
- Data written there is persistent across container restarts/rebuilds (it is not lost unless the volume is deleted).

### Sync old archive data into the live Docker volume

If you have old archive folders in `/root/AK07-archive`, sync them into the active API data volume:

```bash
mkdir -p /var/lib/docker/volumes/configs_ak07_server_data/_data/users/AK07/archive
rsync -a /root/AK07-archive/ /var/lib/docker/volumes/configs_ak07_server_data/_data/users/AK07/archive/
chmod -R a+rX /var/lib/docker/volumes/configs_ak07_server_data/_data/users/AK07/archive
```

### Restart commands

Quick restart (pick up new runtime data/log files):

```bash
cd /root/volume-order-block
docker compose -f configs/docker-compose.yml restart api bot
```

Rebuild and restart after code changes:

```bash
cd /root/volume-order-block
git checkout AK07
git pull origin AK07
docker compose -f configs/docker-compose.yml up -d --build web api bot
```

## More docs

- `docs/QUICKSTART.md` — short checklist  
- `docs/DASHBOARD_SETUP.md` — local dashboard setup  
- `docs/STRATEGY_LOGIC.md`, `docs/CHANGELOG.md`

## License

MIT — use at your own risk. Trading involves financial risk.
