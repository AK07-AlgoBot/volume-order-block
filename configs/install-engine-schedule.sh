#!/usr/bin/env bash
# Install cron jobs on EC2 to start/stop the AK07 engine container daily.
#
# Defaults (IST):
# - START api+engine at 05:55 (before 06:00 token refresh)
# - REFRESH Upstox token at 06:00 (optional cron script; engine also refreshes in-loop)
# - STOP  engine at 15:45 (after the 15:30 performance archive hook)
#
# Usage:
#   chmod +x configs/install-engine-schedule.sh
#   ./configs/install-engine-schedule.sh
#   START_HOUR=5 START_MIN=55 TOKEN_HOUR=6 TOKEN_MIN=0 ./configs/install-engine-schedule.sh

set -euo pipefail

REPO_ROOT="${1:-${HOME}/volume-order-block}"
COMPOSE_FILE="${REPO_ROOT}/configs/docker-compose.yml"
COMPOSE_PROJECT="${COMPOSE_PROJECT_NAME:-ak07}"
START_HOUR="${START_HOUR:-5}"
START_MIN="${START_MIN:-55}"
TOKEN_HOUR="${TOKEN_HOUR:-6}"
TOKEN_MIN="${TOKEN_MIN:-0}"
STOP_HOUR="${STOP_HOUR:-15}"
STOP_MIN="${STOP_MIN:-45}"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "error: missing compose file at $COMPOSE_FILE" >&2
  exit 1
fi

# IST -> UTC cron fields (India has no DST; fixed +05:30).
ist_to_utc_cron() {
  python3 -c "
from datetime import datetime, timezone, timedelta
ist = timezone(timedelta(hours=5, minutes=30))
h, m = int('${1}'), int('${2}')
d = datetime(2000, 1, 1, h, m, tzinfo=ist)
u = d.astimezone(timezone.utc)
print(u.minute, u.hour)
"
}

read -r START_MIN_UTC START_HOUR_UTC <<< "$(ist_to_utc_cron "${START_HOUR}" "${START_MIN}")"
read -r TOKEN_MIN_UTC TOKEN_HOUR_UTC <<< "$(ist_to_utc_cron "${TOKEN_HOUR}" "${TOKEN_MIN}")"
read -r STOP_MIN_UTC STOP_HOUR_UTC <<< "$(ist_to_utc_cron "${STOP_HOUR}" "${STOP_MIN}")"

_CRON_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COMPOSE="PATH=${_CRON_PATH} docker compose -p ${COMPOSE_PROJECT} -f configs/docker-compose.yml"

START_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_ENGINE_START (IST) ====\"; ${COMPOSE} up -d redis api engine; echo \"exit=\$?\"; } >> ${REPO_ROOT}/engine-scheduler.log 2>&1"
TOKEN_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_TOKEN_REFRESH (IST) ====\"; ${COMPOSE} exec -T engine python scripts/refresh_upstox_token.py; echo \"exit=\$?\"; } >> ${REPO_ROOT}/engine-scheduler.log 2>&1"
STOP_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_ENGINE_STOP (IST) ====\"; ${COMPOSE} stop engine; echo \"exit=\$?\"; } >> ${REPO_ROOT}/engine-scheduler.log 2>&1"

START_LINE="${START_MIN_UTC} ${START_HOUR_UTC} * * * ${START_CMD} # AK07_ENGINE_START IST ${START_HOUR}:${START_MIN}"
TOKEN_LINE="${TOKEN_MIN_UTC} ${TOKEN_HOUR_UTC} * * * ${TOKEN_CMD} # AK07_TOKEN_REFRESH IST ${TOKEN_HOUR}:${TOKEN_MIN}"
STOP_LINE="${STOP_MIN_UTC} ${STOP_HOUR_UTC} * * * ${STOP_CMD} # AK07_ENGINE_STOP IST ${STOP_HOUR}:${STOP_MIN}"

TMP_CRON="$(mktemp)"
crontab -l 2>/dev/null | grep -Ev \
  '(AK07_(BOT|ENGINE)_(START|STOP|TOKEN_REFRESH)|configs/docker-compose\.yml.*(up -d (bot|redis engine)|stop (bot|engine)|refresh_upstox_token)|(bot|engine)-scheduler)' \
  > "$TMP_CRON" || true

{
  cat "$TMP_CRON"
  echo "$START_LINE"
  echo "$TOKEN_LINE"
  echo "$STOP_LINE"
} | awk 'NF' | awk '!seen[$0]++' | crontab -

rm -f "$TMP_CRON"

echo "Installed AK07 engine schedule (project=${COMPOSE_PROJECT}):"
echo "  IST ENGINE START : ${START_HOUR}:${START_MIN} -> UTC ${START_HOUR_UTC}:${START_MIN_UTC}"
echo "  IST TOKEN REFRESH: ${TOKEN_HOUR}:${TOKEN_MIN} -> UTC ${TOKEN_HOUR_UTC}:${TOKEN_MIN_UTC}"
echo "  IST ENGINE STOP  : ${STOP_HOUR}:${STOP_MIN} -> UTC ${STOP_HOUR_UTC}:${STOP_MIN_UTC}"
echo
echo "Current crontab entries:"
crontab -l | grep -E "AK07_(ENGINE|TOKEN)_" || true
echo
echo "Manual test:"
echo "  ${COMPOSE} exec engine python scripts/refresh_upstox_token.py"
echo "  tail -f ${REPO_ROOT}/engine-scheduler.log"
