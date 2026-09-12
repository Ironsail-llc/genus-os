#!/bin/bash
# Decrypt SOPS secrets to a temporary environment file for systemd EnvironmentFile.
# Output: /run/robothor/secrets.env (tmpfs, not persisted across reboots)
#
# This is the implementation of the `sops` secrets backend. Units do not run it
# directly any more — scripts/load-secrets.sh dispatches to it — because SOPS
# is one backend of three rather than a precondition for starting the platform.
# Invoked by hand it still works exactly as it always did.

set -euo pipefail

# ── PATH: fixed, and NOT inherited ───────────────────────────────────────────
# The unit that starts this loads EnvironmentFile=, and the instance file there
# carries the OPERATOR's PATH: user-writable directories first (~/.local/bin,
# ~/.npm-global/bin) and no /usr/sbin or /sbin at all. Both halves are bugs for
# something running as root — it must not execute a user-writable binary, and
# dmsetup, cryptsetup, fsck.ext4, smartctl and runuser all live in /usr/sbin,
# where "not found" reaches a script that reads output as an empty ANSWER
# rather than as an error (2026-09-02, scripts/backup-volume-guard.sh).
#
# So the PATH is SET, not extended, and it is the same line in every root
# script. ROBOTHOR_EXTRA_PATH is a TEST-ONLY leading directory, where the suites
# put their stub binaries — it is never set in a unit or in
# /etc/robothor/robothor.env. Anything from the workspace venv is called by
# absolute path (SCRIPT_DIR), never found on PATH.
# See infra/systemd/README.md.
export PATH="${ROBOTHOR_EXTRA_PATH:+$ROBOTHOR_EXTRA_PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ROBOTHOR_SECRETS_ROOT prefixes /etc/robothor and /run/robothor, the same seam
# scripts/load-secrets.sh honours (and the same idea as install-units.sh
# --root). Empty — the default, and the only value any unit ever supplies —
# means the real paths, so nothing about a live box changes. It is what lets
# tests/test_decrypt_secrets_required_keys.py run the real script against a
# fixture instead of asserting on its source text.
SECRETS_ROOT="${ROBOTHOR_SECRETS_ROOT:-}"
SECRETS_ROOT="${SECRETS_ROOT%/}"

SOPS_FILE="${SECRETS_ROOT}/etc/robothor/secrets.enc.json"
AGE_KEY="${SECRETS_ROOT}/etc/robothor/age.key"
OUTPUT_DIR="${SECRETS_ROOT}/run/robothor"
OUTPUT_FILE="${OUTPUT_DIR}/secrets.env"

mkdir -p "$OUTPUT_DIR" 2>/dev/null || true

export SOPS_AGE_KEY_FILE="$AGE_KEY"

# Decrypt JSON and convert to KEY=VALUE format for systemd EnvironmentFile
# Double-quoted values: systemd treats # as comment inside single quotes but not double quotes.
# Double quotes also work with bash source (no $ chars in secret values).
# Remove before writing: the output directory is writable by the service
# account, and a symlink planted at the output path would send the whole
# decrypted credential set to wherever it points. Unlink the path itself
# first so the redirect below always creates a regular file.
rm -f -- "$OUTPUT_FILE"
sops -d "$SOPS_FILE" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for k, v in data.items():
    escaped = v.replace('\\\\', '\\\\\\\\').replace('\"', '\\\\\"')
    print(f'{k}=\"{escaped}\"')
" > "$OUTPUT_FILE"

chmod 600 "$OUTPUT_FILE"

# ── Validate required keys ──────────────────────────────────────────
# REQUIRED means "the instance cannot function without it", and nothing else.
# ROBOTHOR_TELEGRAM_BOT_TOKEN and ROBOTHOR_TELEGRAM_CHAT_ID were on this list
# until 2026-09-12 and belonged to neither category: Telegram became an optional
# channel in #498, the daemon has been Telegram-optional for longer than that,
# and this script now runs as a backend of robothor-secrets.service, which
# orders four services. So a fresh install with no bot token did not merely
# lack a channel — it failed the boot, and took engine, bridge, app and
# orchestrator into `dependency failed` with no Restart= that could clear it.
REQUIRED_KEYS=(
    "OPENROUTER_API_KEY"
)

# Advisory, never required: a missing spare must warn, not block a boot.
# 2026-08-27 — the slot below was once a duplicate of OPENROUTER_API_KEY, so
# every boot validated the primary twice and nothing checked that the pool
# had a spare at all. robothor/engine/key_pool.py exists precisely so one
# dead key is not an outage; it shipped 2026-08-25 and then ran with a single
# key for two days, because nothing on this path ever said the slot was empty.
ADVISORY_KEYS=(
    "OPENROUTER_API_KEY_2"
)

# Also advisory: the pager's own credentials. Telegram is an optional channel,
# so a boot without them is a valid instance — but this is the only boot-time
# place that can say "nothing will page you", so it says so, by name only.
PAGER_KEYS=(
    "ROBOTHOR_TELEGRAM_BOT_TOKEN"
    "ROBOTHOR_TELEGRAM_CHAT_ID"
)

missing=()
for key in "${REQUIRED_KEYS[@]}"; do
    if ! grep -q "^${key}=" "$OUTPUT_FILE"; then
        missing+=("$key")
    fi
done

for key in "${ADVISORY_KEYS[@]}"; do
    if ! grep -q "^${key}=" "$OUTPUT_FILE"; then
        echo "WARNING: $key is not set — this credential pool has no spare." >&2
        echo "         One capped or revoked key will take the whole fleet down." >&2
        echo "         Add it with: sops $SOPS_FILE" >&2
    fi
done

for key in "${PAGER_KEYS[@]}"; do
    if ! grep -q "^${key}=" "$OUTPUT_FILE"; then
        echo "WARNING: $key is not set — the Telegram pager cannot deliver alerts without it." >&2
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
