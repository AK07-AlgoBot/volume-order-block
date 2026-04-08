#!/usr/bin/env bash
# Install cron jobs on EC2 to start/stop the bot container daily (IST).
#
# Default schedule:
# - START at 07:38 IST
# - STOP  at 23:20 IST
#
# Usage:
#   chmod +x configs/install-bot-schedule.sh
#   ./configs/install-bot-schedule.sh
#   START_HOUR=8 START_MIN=0 STOP_HOUR=23 STOP_MIN=0 ./configs/install-bot-schedule.sh

set -euo pipefail

REPO_ROOT="${1:-${HOME}/volume-order-block}"
COMPOSE_FILE="${REPO_ROOT}/configs/docker-compose.yml"
START_HOUR="${START_HOUR:-7}"
START_MIN="${START_MIN:-38}"
STOP_HOUR="${STOP_HOUR:-23}"
STOP_MIN="${STOP_MIN:-20}"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: missing compose file at $COMPOSE_FILE" >&2
  exit 1
fi

# Use `up -d`, not `start`: `start` only wakes an existing stopped container; after down/deploy
# there is no bot container, so cron would silently fail until someone runs `up` manually.
START_CMD="cd ${REPO_ROOT} && docker compose -f configs/docker-compose.yml up -d bot >> ${REPO_ROOT}/bot-scheduler.log 2>&1"
STOP_CMD="cd ${REPO_ROOT} && docker compose -f configs/docker-compose.yml stop bot >> ${REPO_ROOT}/bot-scheduler.log 2>&1"

START_LINE="${START_MIN} ${START_HOUR} * * * ${START_CMD} # AK07_BOT_START"
STOP_LINE="${STOP_MIN} ${STOP_HOUR} * * * ${STOP_CMD} # AK07_BOT_STOP"

TMP_CRON="$(mktemp)"
crontab -l 2>/dev/null | rg -v "AK07_BOT_START|AK07_BOT_STOP" > "$TMP_CRON" || true

{
  echo "CRON_TZ=Asia/Kolkata"
  cat "$TMP_CRON"
  echo "$START_LINE"
  echo "$STOP_LINE"
} | awk 'NF' | crontab -

rm -f "$TMP_CRON"

echo "Installed bot schedule (IST):"
echo "  START: ${START_HOUR}:${START_MIN}"
echo "  STOP : ${STOP_HOUR}:${STOP_MIN}"
echo
echo "Current crontab entries:"
crontab -l | rg "AK07_BOT_(START|STOP)|CRON_TZ=Asia/Kolkata" || true
