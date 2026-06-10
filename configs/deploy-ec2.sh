#!/usr/bin/env bash
# Deploy AK07 on the EC2 instance.
#
# Run ON the EC2 instance from any directory:
#   chmod +x configs/deploy-ec2.sh
#   ./configs/deploy-ec2.sh
#   ./configs/deploy-ec2.sh /home/ubuntu/volume-order-block
#
# First server setup:
#   sudo apt-get update
#   sudo apt-get install -y git docker.io docker-compose-plugin
#   sudo usermod -aG docker "$USER"   # log out/in after this
#   git clone <repo-url> ~/volume-order-block
#   cd ~/volume-order-block
#   cp configs/.env.example .env
#   nano .env
#   ./configs/deploy-ec2.sh

set -euo pipefail

REPO_ROOT="${1:-${HOME}/volume-order-block}"
BRANCH="${DEPLOY_BRANCH:-AK07}"
COMPOSE_FILE="configs/docker-compose.yml"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-ak07}"

cd "$REPO_ROOT"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: missing $COMPOSE_FILE (cwd=$(pwd))" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed. Install docker.io and docker-compose-plugin first." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "error: docker compose plugin is not available." >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  cp configs/.env.example .env
  echo "created .env from configs/.env.example"
  echo "edit .env (JWT_SECRET, AK07_PASSWORD, Telegram, paper/live flags) and rerun deploy." >&2
  exit 2
fi

echo "==> Deploy AK07 from $(pwd)"
echo "    branch=$BRANCH project=$PROJECT_NAME compose=$COMPOSE_FILE"

git fetch origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "==> Validate compose"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" config --quiet

echo "==> Build images"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" build --pull

echo "==> Start/recreate stack"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --remove-orphans

echo "==> Wait for API and cockpit health"
for i in {1..30}; do
  if docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T api \
      python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [[ "$i" == "30" ]]; then
    echo "warning: API health check did not pass within 60s" >&2
  fi
done

for i in {1..30}; do
  if docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" exec -T cockpit \
      python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/healthz', timeout=3).read()" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [[ "$i" == "30" ]]; then
    echo "warning: cockpit health check did not pass within 60s" >&2
  fi
done

echo "==> Status"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" ps

cat <<'EOF'

AK07 deploy complete.

Server endpoints:
  Cockpit : http://<EC2_PUBLIC_IP_OR_DOMAIN>:8501
  API     : http://<EC2_PUBLIC_IP_OR_DOMAIN>:8080/api/health
  MCP     : http://<EC2_PUBLIC_IP_OR_DOMAIN>:8765/mcp

Useful commands:
  docker compose -p ak07 -f configs/docker-compose.yml logs -f engine
  docker compose -p ak07 -f configs/docker-compose.yml logs -f cockpit
  docker compose -p ak07 -f configs/docker-compose.yml exec api python scripts/reset_dashboard_password.py -p 'NEW_PASSWORD'
  docker compose -p ak07 -f configs/docker-compose.yml exec api sh -lc 'mkdir -p src/server/data/users/AK07 && cp src/server/templates/upstox_credentials.example.json src/server/data/users/AK07/upstox_credentials.json'

Persistent Docker volumes:
  ak07_ak07_server_data  -> users/auth/upstox credentials
  ak07_ak07_redis_data   -> Redis cache
EOF
