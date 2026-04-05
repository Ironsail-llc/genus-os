#!/bin/bash
# Cron wrapper: ensure secrets are available, then exec the command.
# Usage: cron-wrapper.sh <command> [args...]
#
# Sources /run/robothor/secrets.env (decrypted by systemd or previous cron).
# If the file doesn't exist yet (e.g., after a reboot before services start),
# runs decrypt-secrets.sh to create it.

set -uo pipefail

SECRETS_ENV="/run/robothor/secrets.env"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DECRYPT_SCRIPT="${SCRIPT_DIR}/decrypt-secrets.sh"

if [ ! -f "$SECRETS_ENV" ]; then
    "$DECRYPT_SCRIPT" 2>/dev/null || true
fi

if [ -f "$SECRETS_ENV" ]; then
    set -a
    source "$SECRETS_ENV"
    set +a
fi

# Cron does not set USER — set it so robothor.config uses the correct DB user.
# pg_hba.conf uses peer auth on Unix sockets, requiring OS user = PG role.
export USER="${USER:-robothor}"
export ROBOTHOR_DB_USER="${ROBOTHOR_DB_USER:-robothor}"

exec "$@"
