#!/usr/bin/env bash
# Page the operator via Telegram when a systemd unit fails.
# Invoked by robothor-alert@<unit>.service (OnFailure= hook), so this must
# stay dependency-free: bash + curl only, credentials from the environment.
#
# Usage: send_failure_alert.sh <unit-name>
set -u

UNIT="${1:?usage: send_failure_alert.sh <unit-name>}"

# The credentials live ONLY in /run/robothor/secrets.env, and /run is tmpfs — so
# on a cold boot the file does not exist until some service's ExecStartPre
# decrypts it. That is exactly when services fail to start, which is exactly when
# this pager is supposed to fire. Observed on the 2026-07-14 reboot: five units
# failed and every alert died with "ROBOTHOR_TELEGRAM_BOT_TOKEN is not set".
#
# An alert that is silent during a boot failure is worse than no alert. This unit
# runs as root and the age key is root-readable, so recover the secrets rather
# than give up. Best-effort: if it still cannot get a token, it exits non-zero
# below, loudly.
if [[ -z "${ROBOTHOR_TELEGRAM_BOT_TOKEN:-}" ]]; then
    # Overridable so the suite can point this at a fixture. Without that, a test
    # run on the live box would source the REAL secrets and page the operator for
    # real — the same class of accident as the benchmark runner that sent actual
    # emails.
    SECRETS="${ROBOTHOR_SECRETS_FILE:-/run/robothor/secrets.env}"
    if [[ ! -r "$SECRETS" ]]; then
        DECRYPT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/decrypt-secrets.sh"
        [[ -x "$DECRYPT" ]] && "$DECRYPT" >/dev/null 2>&1 || true
    fi
    if [[ -r "$SECRETS" ]]; then
        # shellcheck disable=SC1090
        set -a; source "$SECRETS"; set +a
    fi
fi

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
