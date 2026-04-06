#!/usr/bin/env bash
# Run ON the EC2 instance (not from your laptop). Rebuilds images and restarts the stack.
#
# Usage:
#   chmod +x configs/deploy-ec2.sh
#   ./configs/deploy-ec2.sh
#   ./configs/deploy-ec2.sh /path/to/volume-order-block
#   DEPLOY_BRANCH=main ./configs/deploy-ec2.sh
#
# Requires: git, docker compose; repo root must contain `.env` (see configs/.env.example).

set -euo pipefail

REPO_ROOT="${1:-${HOME}/volume-order-block}"
BRANCH="${DEPLOY_BRANCH:-AK07}"
COMPOSE_FILE="configs/docker-compose.yml"

cd "$REPO_ROOT"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: not a project root or missing $COMPOSE_FILE (cwd=$(pwd))" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "error: missing .env in $REPO_ROOT — copy from configs/.env.example and edit secrets." >&2
  exit 1
fi

echo "==> Deploy from $(pwd) branch=$BRANCH"
git fetch origin
git checkout "$BRANCH"
git pull origin "$BRANCH"

echo "==> Docker build (pull base images)"
docker compose -f "$COMPOSE_FILE" build --pull

echo "==> Recreate containers"
docker compose -f "$COMPOSE_FILE" up -d

echo "==> Status"
docker compose -f "$COMPOSE_FILE" ps

echo "==> Done."
