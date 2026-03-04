#!/bin/bash
# Wrapper script: inject secrets from SOPS-encrypted file into environment
# Usage: with-secrets.sh <command> [args...]
#
# This script decrypts /etc/robothor/secrets.json.enc using the age key
# and executes the given command with all secrets as environment variables.
#
# Example:
#   with-secrets.sh python3 /home/philip/robothor/brain/scripts/email_sync.py
#   with-secrets.sh /home/philip/robothor/brain/memory_system/venv/bin/python vision_service.py

set -euo pipefail

SOPS_FILE="/etc/robothor/secrets.enc.json"
AGE_KEY="/etc/robothor/age.key"

if [ $# -eq 0 ]; then
    echo "Usage: with-secrets.sh <command> [args...]" >&2
    exit 1
fi

if [ ! -f "$SOPS_FILE" ]; then
    echo "ERROR: SOPS secrets file not found: $SOPS_FILE" >&2
    exit 1
fi

if [ ! -f "$AGE_KEY" ]; then
    echo "ERROR: Age key not found: $AGE_KEY" >&2
    exit 1
fi

export SOPS_AGE_KEY_FILE="$AGE_KEY"
exec sops exec-env "$SOPS_FILE" "$*"
