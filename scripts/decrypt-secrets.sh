#!/bin/bash
# Decrypt SOPS secrets to a temporary environment file for systemd EnvironmentFile.
# Called by ExecStartPre in systemd services.
# Output: /run/robothor/secrets.env (tmpfs, not persisted across reboots)
#
# Usage in systemd service:
#   [Service]
#   ExecStartPre=/home/philip/robothor/scripts/decrypt-secrets.sh
#   EnvironmentFile=/run/robothor/secrets.env

set -euo pipefail

SOPS_FILE="/etc/robothor/secrets.enc.json"
AGE_KEY="/etc/robothor/age.key"
OUTPUT_DIR="/run/robothor"
OUTPUT_FILE="${OUTPUT_DIR}/secrets.env"

mkdir -p "$OUTPUT_DIR" 2>/dev/null || true

export SOPS_AGE_KEY_FILE="$AGE_KEY"

# Decrypt JSON and convert to KEY=VALUE format for systemd EnvironmentFile
# Double-quoted values: systemd treats # as comment inside single quotes but not double quotes.
# Double quotes also work with bash source (no $ chars in secret values).
sops -d "$SOPS_FILE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for k, v in data.items():
    escaped = v.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"')
    print(f'{k}=\"{escaped}\"')
" > "$OUTPUT_FILE"

chmod 600 "$OUTPUT_FILE"

# ── Validate required keys ──────────────────────────────────────────
REQUIRED_KEYS=(
    "OPENROUTER_API_KEY"
    "ROBOTHOR_TELEGRAM_BOT_TOKEN"
    "ROBOTHOR_TELEGRAM_CHAT_ID"
)

missing=()
for key in "${REQUIRED_KEYS[@]}"; do
    if ! grep -q "^${key}=" "$OUTPUT_FILE"; then
        missing+=("$key")
    fi
done

if [ ${#missing[@]} -gt 0 ]; then
    echo "ERROR: Required secrets missing from $SOPS_FILE:" >&2
    for key in "${missing[@]}"; do
        echo "  - $key" >&2
    done
    echo "Add missing keys with: sops $SOPS_FILE" >&2
    exit 1
fi
