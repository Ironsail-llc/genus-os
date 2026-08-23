#!/usr/bin/env bash
# Render a systemd unit template from infra/systemd/ into an installable unit.
#
# Why: the repo templates were hand-copied and hand-edited into
# /etc/systemd/system, so fixes in the repo never reached the box — and the
# templates carry placeholders systemd cannot expand (`${ROBOTHOR_WORKSPACE}`
# fails `systemd-analyze verify`; `%h` expands to /root in system units, a
# documented past incident). This script is the one place placeholders become
# real paths; scripts/install-units.sh drives it for the whole unit set.
#
# Template convention (see infra/systemd/README.md) — canonical spellings:
#   /opt/robothor    the workspace           -> $ROBOTHOR_WORKSPACE
#   /home/robothor   the service user's home -> $ROBOTHOR_SERVICE_HOME
#   User=robothor    the service account     -> User=$ROBOTHOR_SERVICE_USER
#   Group=robothor   (exact lines only)      -> Group=$ROBOTHOR_SERVICE_USER
# Legacy spellings still rendered (but rejected in new templates by
# tests/test_install_units.py): ${ROBOTHOR_WORKSPACE} and %h.
#
# Usage: render-unit.sh [--tmpfiles] SRC [DEST]   (DEST defaults to stdout)
#
# Environment:
#   ROBOTHOR_WORKSPACE     required — workspace root (repo checkout)
#   ROBOTHOR_SERVICE_USER  required — account the services run as. There is
#                          deliberately NO default from the invoking user: the
#                          installer typically runs under sudo, and silently
#                          rendering User=root would be the %h incident again.
#   ROBOTHOR_SERVICE_HOME  optional — service user's home; derived from the
#                          user's passwd entry when unset
#   ROBOTHOR_ENV_FILE      optional — file to read unset vars from
#                          (default /etc/robothor/robothor.env)
#
# Fails loudly when a required variable is unresolvable, or when any
# placeholder survives rendering (the structural gate used in CI, where
# systemd-analyze verify cannot run against the target box's binaries).
set -euo pipefail

die() { echo "render-unit: $*" >&2; exit 1; }

# --tmpfiles: SRC is a systemd-tmpfiles.d(5) conf, whose USER and GROUP are
# POSITIONAL columns (TYPE PATH MODE USER GROUP AGE ARGUMENT) rather than
# `User=`/`Group=` directives. The unit rules below are exact-line anchored and
# cannot see them, so a tmpfiles conf run through the plain renderer emits the
# placeholder verbatim — which looks fine and chowns the runtime directory to
# an account that may not exist. The mode is EXPLICIT, never inferred from the
# path: magic that cannot be tested in isolation is how this class of bug
# survives.
TMPFILES=0
if [[ "${1:-}" == "--tmpfiles" ]]; then
    TMPFILES=1
    shift
fi

SRC="${1:-}"
DEST="${2:-}"
[[ -n "$SRC" ]] || die "usage: render-unit.sh [--tmpfiles] SRC [DEST]"
[[ -r "$SRC" ]] || die "cannot read template: $SRC"

ENV_FILE="${ROBOTHOR_ENV_FILE:-/etc/robothor/robothor.env}"

# Read KEY=VALUE from the env file (last assignment wins), for vars not
# already set in the environment. Explicit env always takes precedence.
env_file_lookup() {
    local key="$1" line
    [[ -r "$ENV_FILE" ]] || return 1
    line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1)" || true
    [[ -n "$line" ]] || return 1
    printf '%s' "${line#*=}"
}

WORKSPACE="${ROBOTHOR_WORKSPACE:-$(env_file_lookup ROBOTHOR_WORKSPACE || true)}"
SERVICE_USER="${ROBOTHOR_SERVICE_USER:-$(env_file_lookup ROBOTHOR_SERVICE_USER || true)}"
SERVICE_HOME="${ROBOTHOR_SERVICE_HOME:-$(env_file_lookup ROBOTHOR_SERVICE_HOME || true)}"

[[ -n "$WORKSPACE" ]] || die "ROBOTHOR_WORKSPACE is not set and not found in ${ENV_FILE}"
[[ -n "$SERVICE_USER" ]] || die "ROBOTHOR_SERVICE_USER is not set and not found in ${ENV_FILE}"

# The service home is only needed when the template references it.
if grep -q -e '%h' -e '/home/robothor' "$SRC"; then
    if [[ -z "$SERVICE_HOME" ]]; then
        SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6 || true)"
    fi
    [[ -n "$SERVICE_HOME" ]] || die \
        "$(basename "$SRC") references the service home (%h or /home/robothor) but it is unresolvable: set ROBOTHOR_SERVICE_HOME or ensure user '${SERVICE_USER}' has a passwd entry"
fi

# ── Substitution ──────────────────────────────────────────────────────────────
# Literal (non-regex) substitution, two passes via sentinels: placeholders are
# first swapped for \001x\002 markers, then markers for values. Values are
# never rescanned, so a workspace like /home/robothor/repo cannot be mangled
# by the home substitution, and awk regex/'&' semantics never touch the data.
export RENDER_WS="$WORKSPACE" RENDER_USER="$SERVICE_USER" RENDER_HOME="$SERVICE_HOME"
export RENDER_TMPFILES="$TMPFILES"
rendered="$(awk '
function lsub(s, pat, rep,    out, i, n) {
    n = length(pat); out = ""
    while ((i = index(s, pat)) > 0) {
        out = out substr(s, 1, i - 1) rep
        s = substr(s, i + n)
    }
    return out s
}
BEGIN {
    WSENT = "\001W\002"; USENT = "\001U\002"; HSENT = "\001H\002"
    ws = ENVIRON["RENDER_WS"]; su = ENVIRON["RENDER_USER"]; home = ENVIRON["RENDER_HOME"]
    tmpf = (ENVIRON["RENDER_TMPFILES"] == "1")
}
{
    s = $0
    # Comment lines pass through untouched — they may legitimately DISCUSS a
    # placeholder (e.g. "never write %h here"), and mangling docs helps nobody.
    if (s ~ /^[ \t]*[#;]/) { print; next }
    # tmpfiles.d row: TYPE PATH MODE USER GROUP AGE ARGUMENT. Only columns 4
    # and 5 are accounts; the PATH column legitimately contains "robothor"
    # (/run/robothor/...) and must never be touched. Assigning a field makes
    # awk rebuild $0 with OFS, which normalises runs of whitespace on that line
    # only — semantically identical for tmpfiles.d.
    if (tmpf && NF >= 5 && $2 ~ /^\//) {
        n = 0
        if ($4 == "robothor") { $4 = USENT; n++ }
        if ($5 == "robothor") { $5 = USENT; n++ }
        if (n) s = $0
    }
    s = lsub(s, "${ROBOTHOR_WORKSPACE}", WSENT)
    s = lsub(s, "/opt/robothor", WSENT)
    if (s == "User=robothor")  s = "User=" USENT
    if (s == "Group=robothor") s = "Group=" USENT
    s = lsub(s, "/home/robothor", HSENT)
    s = lsub(s, "%h", HSENT)
    s = lsub(s, WSENT, ws)
    s = lsub(s, USENT, su)
    s = lsub(s, HSENT, home)
    print s
}' "$SRC")"

# ── Structural gate ───────────────────────────────────────────────────────────
# Nothing unexpanded may survive: systemd will not expand ${...} (verify fails
# on it), %h is /root in system units, and a leftover placeholder account or
# path means the unit silently runs as/against the wrong identity. The
# equality guards keep the gate honest when a real deployment legitimately
# uses a placeholder spelling as its actual value (e.g. workspace IS
# /opt/robothor).
fail_lines() { die "$(basename "$SRC"): unexpanded placeholder after render — $1:
$2"; }

# Gate the directives only — comment lines are documentation and pass through
# unrendered by design. Blank them (preserving line numbers) before grepping.
directives="$(sed 's/^[[:space:]]*[#;].*//' <<<"$rendered")"

if leftover="$(grep -n '\${ROBOTHOR_' <<<"$directives")"; then
    fail_lines 'unknown ${ROBOTHOR_*} variable' "$leftover"
fi
if leftover="$(grep -n '%h' <<<"$directives")"; then
    fail_lines '%h (== /root in system units)' "$leftover"
fi
if [[ "$SERVICE_USER" != "robothor" ]] \
    && leftover="$(grep -nE '^(User|Group)=robothor$' <<<"$directives")"; then
    fail_lines 'placeholder service account' "$leftover"
fi
# The tmpfiles account columns get their own gate for the same reason they get
# their own substitution: the ^(User|Group)= grep above cannot see them. awk
# always exits 0, so test emptiness explicitly rather than using `if leftover=`.
if [[ "$TMPFILES" == 1 && "$SERVICE_USER" != "robothor" ]]; then
    leftover="$(awk 'NF >= 5 && $2 ~ /^\// && ($4 == "robothor" || $5 == "robothor") \
        { print NR ": " $0 }' <<<"$directives")"
    [[ -z "$leftover" ]] || fail_lines \
        'placeholder account in a tmpfiles user/group column' "$leftover"
fi
if [[ "$WORKSPACE" != "/opt/robothor" ]] \
    && leftover="$(grep -nF '/opt/robothor' <<<"$directives")"; then
    fail_lines 'workspace placeholder' "$leftover"
fi
if [[ "${SERVICE_HOME:-}" != "/home/robothor" ]] \
    && leftover="$(grep -nF '/home/robothor' <<<"$directives")"; then
    fail_lines 'home placeholder' "$leftover"
fi

if [[ -n "$DEST" ]]; then
    printf '%s\n' "$rendered" > "$DEST"
else
    printf '%s\n' "$rendered"
fi
