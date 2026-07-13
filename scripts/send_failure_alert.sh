#!/usr/bin/env bash
# Page the operator via Telegram when a systemd unit fails.
# Invoked by robothor-alert@<unit>.service (OnFailure= hook), so this must
# stay dependency-free: bash + curl only, credentials from the environment.
#
# Usage: send_failure_alert.sh <unit-name>
set -u

UNIT="${1:?usage: send_failure_alert.sh <unit-name>}"

if [[ -z "${ROBOTHOR_TELEGRAM_BOT_TOKEN:-}" ]]; then
    echo "send_failure_alert: ROBOTHOR_TELEGRAM_BOT_TOKEN is not set" >&2
    exit 1
fi
if [[ -z "${ROBOTHOR_TELEGRAM_CHAT_ID:-}" ]]; then
    echo "send_failure_alert: ROBOTHOR_TELEGRAM_CHAT_ID is not set" >&2
    exit 1
fi

HOST="$(hostname -s 2>/dev/null || echo unknown-host)"
JOURNAL="$(journalctl -u "$UNIT" -n 5 --no-pager -o cat 2>/dev/null | tail -c 500 || true)"

TEXT="🔴 ${UNIT} FAILED on ${HOST}
$(date -Is)

Last journal lines:
${JOURNAL:-<journal unavailable>}

Check: systemctl status ${UNIT}"

curl -sS --max-time 15 \
    --data-urlencode "chat_id=${ROBOTHOR_TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${TEXT}" \
    "https://api.telegram.org/bot${ROBOTHOR_TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null
