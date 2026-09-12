#!/bin/bash
# Populate /run/robothor/secrets.env from whichever secrets backend this
# instance uses. Called by ExecStartPre in every service that loads secrets,
# and by robothor-secrets.service, which is the ordering point the others
# Requires=.
#
# Usage in systemd service:
#   [Service]
#   ExecStartPre=$ROBOTHOR_WORKSPACE/scripts/load-secrets.sh
#   EnvironmentFile=-/run/robothor/secrets.env
#
# WHY THIS SCRIPT EXISTS
#   Until it did, the ExecStartPre was scripts/decrypt-secrets.sh and
#   robothor-secrets.service carried
#   ConditionPathExists=/etc/robothor/secrets.enc.json. SOPS was therefore not
#   an option but a precondition: an operator with a plaintext secrets file, or
#   with credentials already in the unit environment, could not start the
#   platform on systemd at all. SOPS is now one backend of three.
#
#   ROBOTHOR_SECRETS_BACKEND selects it:
#
#     sops  delegate to decrypt-secrets.sh (age key + /etc/robothor/secrets.enc.json)
#     file  validate and copy a plaintext file the operator manages
#     env   write an EMPTY file; the credentials are in the unit environment
#           (/etc/robothor/robothor.env, a drop-in, or the container env)
#
#   Unset means AUTO, and auto exists so that no configured instance has to
#   change anything: an encrypted file present means sops, else a plaintext
#   file present means file, else env. Every previously-working SOPS box takes
#   the first branch.
#
#   The `env` backend writes an empty file rather than no file on purpose. The
#   consumers load `EnvironmentFile=-/run/robothor/secrets.env` — optional, so
#   a missing file does not fail them — but the file is also the handshake
#   between this script and four long-running services, and "absent" and
#   "empty" must not be two different states for them to get wrong.
#
# This script never prints a secret value. Both streams are journaled, and a
# journal outlives the tmpfs file the values came from.

set -euo pipefail

# ── PATH: fixed, and NOT inherited ───────────────────────────────────────────
# The unit that starts this loads EnvironmentFile=, and the instance file there
# carries the OPERATOR's PATH: user-writable directories first (~/.local/bin,
# ~/.npm-global/bin) and no /usr/sbin or /sbin at all. Both halves are bugs for
# something running as root. So the PATH is SET, not extended, and it is the
# same line in every root script. ROBOTHOR_EXTRA_PATH is a TEST-ONLY leading
# directory — it is never set in a unit or in /etc/robothor/robothor.env.
# See infra/systemd/README.md and tests/test_root_scripts_set_path.py.
export PATH="${ROBOTHOR_EXTRA_PATH:+$ROBOTHOR_EXTRA_PATH:}/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

# ROBOTHOR_SECRETS_ROOT prefixes /etc/robothor and /run/robothor. It is the
# same `--root` seam scripts/install-units.sh has, and for the same reason: the
# suite exercises every branch of this dispatcher for real — real files, real
# modes, a real subprocess — without root and without going anywhere near the
# instance's own credentials. Empty (the default) means the real paths.
ROOT="${ROBOTHOR_SECRETS_ROOT:-}"
ROOT="${ROOT%/}"

ETC_DIR="${ROOT}/etc/robothor"
OUTPUT_DIR="${ROOT}/run/robothor"
OUTPUT_FILE="${OUTPUT_DIR}/secrets.env"
SOPS_FILE="${ETC_DIR}/secrets.enc.json"
PLAIN_FILE="${ROBOTHOR_SECRETS_BACKEND_FILE:-${ETC_DIR}/secrets.env}"

log() { echo "load-secrets: $*"; }
die() {
    echo "load-secrets: ERROR: $*" >&2
    exit 1
}

# ── Which backend ────────────────────────────────────────────────────────────
BACKEND="${ROBOTHOR_SECRETS_BACKEND:-}"
AUTO=0
if [[ -z "$BACKEND" ]]; then
    AUTO=1
    if [[ -f "$SOPS_FILE" ]]; then
        BACKEND="sops"
    elif [[ -f "$PLAIN_FILE" ]]; then
        BACKEND="file"
    else
        BACKEND="env"
    fi
    log "backend ${BACKEND} (auto-detected; set ROBOTHOR_SECRETS_BACKEND to pin it)"
else
    log "backend ${BACKEND} (ROBOTHOR_SECRETS_BACKEND)"
fi

case "$BACKEND" in
    sops | file | env) ;;
    *) die "unknown secrets backend '${BACKEND}' — expected one of: sops, file, env" ;;
esac

# ── sops ─────────────────────────────────────────────────────────────────────
# exec, not call: decrypt-secrets.sh owns the whole job from here, including
# its REQUIRED_KEYS validation and its exit status, and there is nothing for
# this script to add afterwards. Its behaviour is unchanged — it is the
# implementation of this backend, not a legacy path.
if [[ "$BACKEND" == "sops" ]]; then
    DECRYPT="${SCRIPT_DIR}/decrypt-secrets.sh"
    [[ -x "$DECRYPT" ]] || die "${DECRYPT} is missing or not executable"
    exec "$DECRYPT"
fi

mkdir -p "$OUTPUT_DIR" 2>/dev/null || true
[[ -d "$OUTPUT_DIR" ]] || die "${OUTPUT_DIR} does not exist and could not be created"

# ── env ──────────────────────────────────────────────────────────────────────
# Truncate rather than leave alone: a file another backend wrote earlier (or
# before a backend change) would keep supplying stale credentials that nothing
# in the instance's configuration claims exist any more.
if [[ "$BACKEND" == "env" ]]; then
    # Auto-detected env means "no secrets file was found". If the tmpfs copy is
    # populated, that is an encrypted or plaintext file that has gone missing
    # since the last boot — not an instance that keeps its credentials in the
    # unit environment. Blanking it and exiting 0 would start four services
    # with no credentials behind an `active (exited)` unit; refuse instead, and
    # name the override for the operator who really did move to env.
    # `-L` first: `-s` follows a symlink, and a planted link to any non-empty
    # file must not be able to wedge this branch. A link is never "populated".
    if [[ "$AUTO" == "1" && ! -L "$OUTPUT_FILE" && -f "$OUTPUT_FILE" && -s "$OUTPUT_FILE" ]]; then
        die "no secrets file found (${SOPS_FILE} or ${PLAIN_FILE}) but ${OUTPUT_FILE} is populated from an earlier boot — refusing to blank it; restore the file, or set ROBOTHOR_SECRETS_BACKEND=env if the credentials really are in the unit environment"
    fi
    # Write a fresh 0600 temp file (mktemp creates it that way, so there is no
    # umask window) and move it over the path with -T, which replaces a
    # planted symlink instead of following it. ${OUTPUT_DIR} is writable by
    # the service account, so the destination is never trusted.
    TMP="$(mktemp "${OUTPUT_DIR}/.secrets.env.XXXXXX")"
    chmod 600 "$TMP"
    mv -T -f -- "$TMP" "$OUTPUT_FILE"
    log "wrote an empty ${OUTPUT_FILE}; secrets are expected in the unit environment"
    exit 0
fi

# ── file ─────────────────────────────────────────────────────────────────────
# Every refusal below names the path and what is wrong with it, and none of
# them reads the contents. A secrets file this script would not accept is an
# outage the operator has to be able to fix from the journal line alone.
[[ -e "$PLAIN_FILE" ]] || die "secrets file ${PLAIN_FILE} does not exist (set ROBOTHOR_SECRETS_BACKEND_FILE, or use the env backend)"
[[ -f "$PLAIN_FILE" ]] || die "secrets file ${PLAIN_FILE} is not a regular file"
[[ -r "$PLAIN_FILE" ]] || die "secrets file ${PLAIN_FILE} is not readable by $(id -un) — this service's own account"

MODE="$(stat -c '%a' "$PLAIN_FILE")"
# stat prints three digits for 0600 and four when a setuid/sticky bit is on, so
# compare on the normalised four-digit form rather than on the raw string.
case "$(printf '%04d' "$MODE")" in
    0600 | 0400) ;;
    *)
        die "secrets file ${PLAIN_FILE} is mode ${MODE}: it must be 0600 or 0400, so that only its owner can read the instance's credentials"
        ;;
esac

# Owned by root, or by the account this service runs as. Anything else means a
# third party can rewrite what the platform is about to load into every
# service's environment — a credential-substitution path, not a hygiene nit.
OWNER_UID="$(stat -c '%u' "$PLAIN_FILE")"
SELF_UID="$(id -u)"
if [[ "$OWNER_UID" != "0" && "$OWNER_UID" != "$SELF_UID" ]]; then
    die "secrets file ${PLAIN_FILE} is owned by uid ${OWNER_UID}: it must be owned by root or by $(id -un) (uid ${SELF_UID}), the account this service runs as"
fi

# install(1) rather than cp: it sets the destination mode in the same call, so
# there is no window in which the copy exists at the umask's mode. The owner is
# whoever runs this — i.e. the service account, exactly as with the sops
# backend, which also leaves the file owned by the process that decrypted it.
install -m 0600 "$PLAIN_FILE" "$OUTPUT_FILE"
log "loaded ${PLAIN_FILE} into ${OUTPUT_FILE}"
