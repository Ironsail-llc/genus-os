#!/bin/bash
# Nightwatch cron wrapper — sources secrets and runs a nightwatch script.
# Usage: nightwatch-cron.sh <script-name>
#   e.g. nightwatch-cron.sh nightwatch-heal.py

set -euo pipefail

SCRIPT_NAME="${1:?Usage: nightwatch-cron.sh <script-name>}"

# Source secrets (SOPS-decrypted at boot)
if [[ -f /run/robothor/secrets.env ]]; then
    source /run/robothor/secrets.env
fi

cd /home/philip/robothor
exec /home/philip/robothor/venv/bin/python "brain/scripts/${SCRIPT_NAME}" 2>&1 | logger -t "nightwatch-${SCRIPT_NAME%.py}"
