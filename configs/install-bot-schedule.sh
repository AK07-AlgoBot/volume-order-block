#!/usr/bin/env bash
# Install cron jobs on EC2 to start/stop the bot container daily (IST wall-clock times).
#
# Many cloud images use UTC as the system timezone. Some cron builds ignore CRON_TZ, so
# "38 7" runs at 07:38 UTC (= 13:08 IST) instead of 07:38 IST. This script converts IST
# to explicit UTC minute/hour for the crontab lines so triggers match India time reliably.
#
# Default schedule:
# - START at 07:38 IST
# - STOP  at 23:21 IST (aligned with bot daily_shutdown_time / TRADING_DAILY_SHUTDOWN_TIME)
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
STOP_MIN="${STOP_MIN:-21}"

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

# Use `up -d`, not `start`: `start` only wakes an existing stopped container; after down/deploy
# there is no bot container, so cron would silently fail until someone runs `up` manually.
# Cron jobs get a minimal PATH; docker is often missing — set PATH explicitly and log each run.
# Log timestamps in IST for human verification (TZ=Asia/Kolkata).
_CRON_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
START_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_BOT_START (IST) ====\"; PATH=${_CRON_PATH} docker compose -f configs/docker-compose.yml up -d bot; echo \"exit=\$?\"; } >> ${REPO_ROOT}/bot-scheduler.log 2>&1"
STOP_CMD="cd ${REPO_ROOT} && { echo \"==== \$(TZ=Asia/Kolkata date -Is) AK07_BOT_STOP (IST) ====\"; PATH=${_CRON_PATH} docker compose -f configs/docker-compose.yml stop bot; echo \"exit=\$?\"; } >> ${REPO_ROOT}/bot-scheduler.log 2>&1"

# Schedule uses UTC hour/minute so it fires at the intended IST times on UTC-root servers.
START_LINE="${START_MIN_UTC} ${START_HOUR_UTC} * * * ${START_CMD} # AK07_BOT_START IST ${START_HOUR}:${START_MIN} = UTC ${START_HOUR_UTC}:${START_MIN_UTC}"
STOP_LINE="${STOP_MIN_UTC} ${STOP_HOUR_UTC} * * * ${STOP_CMD} # AK07_BOT_STOP IST ${STOP_HOUR}:${STOP_MIN} = UTC ${STOP_HOUR_UTC}:${STOP_MIN_UTC}"

TMP_CRON="$(mktemp)"
# Strip all prior AK07 bot schedule lines (including duplicates). Use grep -E (not rg) so minimal
# Ubuntu images behave the same. Match compose+bot commands and CRON_TZ so nothing is left behind.
crontab -l 2>/dev/null | grep -Ev \
  '(AK07_BOT_(START|STOP)|^[[:space:]]*CRON_TZ=|configs/docker-compose\.yml.*(up -d bot|stop bot)|bot-scheduler)' \
  > "$TMP_CRON" || true

{
  cat "$TMP_CRON"
  echo "$START_LINE"
  echo "$STOP_LINE"
  # Collapse exact duplicate lines (e.g. double-install or partial strip); cron order preserved for unique lines.
} | awk 'NF' | awk '!seen[$0]++' | crontab -

rm -f "$TMP_CRON"

echo "Installed bot schedule:"
echo "  IST START: ${START_HOUR}:${START_MIN}  ->  UTC cron: ${START_HOUR_UTC}:${START_MIN_UTC}"
echo "  IST STOP : ${STOP_HOUR}:${STOP_MIN}  ->  UTC cron: ${STOP_HOUR_UTC}:${STOP_MIN_UTC}"
echo
echo "Current crontab entries:"
crontab -l | grep -E "AK07_BOT_(START|STOP)" || true
