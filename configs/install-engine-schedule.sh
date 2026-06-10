#!/usr/bin/env bash
# Install cron jobs on EC2 to start/stop the AK07 engine container daily.
#
# Defaults:
# - START at 09:10 IST (engine runs token/session init at 09:15 IST)
# - STOP  at 15:45 IST (after the 15:30 performance archive hook)
#
# Usage:
#   chmod +x configs/install-engine-schedule.sh
#   ./configs/install-engine-schedule.sh
#   START_HOUR=9 START_MIN=10 STOP_HOUR=15 STOP_MIN=45 ./configs/install-engine-schedule.sh

set -euo pipefail

REPO_ROOT="${1:-${HOME}/volume-order-block}"
COMPOSE_FILE="${REPO_ROOT}/configs/docker-compose.yml"
START_HOUR="${START_HOUR:-9}"
START_MIN="${START_MIN:-10}"
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
read -r STOP_MIN_UTC STOP_HOUR_UTC <<< "$(ist_to_utc_cron "${STOP_HOUR}" "${STOP_MIN}")"

_CRON_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
START_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_ENGINE_START (IST) ====\"; PATH=${_CRON_PATH} docker compose -f configs/docker-compose.yml up -d redis engine; echo \"exit=\$?\"; } >> ${REPO_ROOT}/engine-scheduler.log 2>&1"
STOP_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_ENGINE_STOP (IST) ====\"; PATH=${_CRON_PATH} docker compose -f configs/docker-compose.yml stop engine; echo \"exit=\$?\"; } >> ${REPO_ROOT}/engine-scheduler.log 2>&1"

START_LINE="${START_MIN_UTC} ${START_HOUR_UTC} * * * ${START_CMD} # AK07_ENGINE_START IST ${START_HOUR}:${START_MIN} = UTC ${START_HOUR_UTC}:${START_MIN_UTC}"
STOP_LINE="${STOP_MIN_UTC} ${STOP_HOUR_UTC} * * * ${STOP_CMD} # AK07_ENGINE_STOP IST ${STOP_HOUR}:${STOP_MIN} = UTC ${STOP_HOUR_UTC}:${STOP_MIN_UTC}"

TMP_CRON="$(mktemp)"
crontab -l 2>/dev/null | grep -Ev \
  '(AK07_(BOT|ENGINE)_(START|STOP)|configs/docker-compose\.yml.*(up -d (bot|redis engine)|stop (bot|engine))|(bot|engine)-scheduler)' \
  > "$TMP_CRON" || true

{
  cat "$TMP_CRON"
  echo "$START_LINE"
  echo "$STOP_LINE"
} | awk 'NF' | awk '!seen[$0]++' | crontab -

rm -f "$TMP_CRON"

echo "Installed AK07 engine schedule:"
echo "  IST START: ${START_HOUR}:${START_MIN} -> UTC cron: ${START_HOUR_UTC}:${START_MIN_UTC}"
echo "  IST STOP : ${STOP_HOUR}:${STOP_MIN} -> UTC cron: ${STOP_HOUR_UTC}:${STOP_MIN_UTC}"
echo
echo "Current crontab entries:"
crontab -l | grep -E "AK07_ENGINE_(START|STOP)" || true
