#!/usr/bin/env bash
# Install the systemd unit templates from infra/systemd/ into their live
# location, replacing the hand-copy-and-edit workflow.
#
# Sibling of scripts/install-host-scripts.sh, same failure mode: installed
# units were hand-edited copies, so template fixes in the repo never reached
# the box and the templates themselves drifted until they no longer rendered
# (`systemd-analyze verify` fails outright on robothor-engine.service's raw
# `${ROBOTHOR_WORKSPACE}` ExecStart lines).
#
# Each robothor-*.{service,timer,path} template and each
# robothor-*.service.d/*.conf drop-in is rendered via scripts/render-unit.sh
# (which enforces the structural gate: no unexpanded placeholders, no %h),
# .service files are then gated on `systemd-analyze verify`, and the results
# installed idempotently to <root>/etc/systemd/system/.
#
# Only robothor-* units are installed. delphi-* units are instance-land —
# some deliberately tombstoned — and must never be resurrected by a platform
# installer.
#
# Usage: install-units.sh [--root DIR] [--env-file FILE] [--restart]
#   --root DIR      filesystem root to install under (default /, so units land
#                   at /etc/systemd/system; override for tests). Under --root,
#                   `systemd-analyze verify` is skipped: verify checks that
#                   ExecStart binaries exist, which only means something on the
#                   target box. The renderer's structural gate still runs.
#   --env-file FILE file to resolve unset ROBOTHOR_* vars from
#                   (default /etc/robothor/robothor.env)
#   --restart       after installing: take the restart broker's lock,
#                   `systemctl daemon-reload`, then ONE `systemctl restart`
#                   naming the secrets oneshot and every unit that Requires=
#                   it, leaving out any unit that already has a job queued.
#                   One command is one transaction; see "Restart each unit
#                   once" below. Exits 1 if a unit is not active afterwards.
#
# Environment: ROBOTHOR_WORKSPACE, ROBOTHOR_SERVICE_USER (required),
# ROBOTHOR_SERVICE_HOME (optional) — see scripts/render-unit.sh.
#
# Idempotent: re-running reports "unchanged" for units that already match,
# and only rewrites the ones that don't. Without --restart it does not
# daemon-reload or restart anything — it prints the follow-up command instead.
#
# RESTART EACH UNIT ONCE. `systemctl restart robothor-secrets` propagates a
# restart to every unit that Requires= it (engine, bridge, app, orchestrator).
# A second `systemctl restart` of those units arriving while that first one is
# still starting them SUPERSEDES the running start job, and systemd kills
# whatever the job was doing — on the engine and the orchestrator that is
# ExecStartPre=load-secrets.sh, a control process, whose death by SIGTERM is
# not a clean exit: `Control process exited, code=killed, status=15/TERM`,
# `Failed with result 'signal'`, OnFailure= pages (2026-09-17 08:55, twice,
# 1.65 s between the two commands). Named together in ONE command the
# propagated and the explicit restart jobs merge into a single transaction and
# each unit restarts once. So the follow-up is one command, printed below or
# run by --restart — never a secrets restart followed by a consumers restart.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER="${REPO_ROOT}/scripts/render-unit.sh"
SRC_DIR="${REPO_ROOT}/infra/systemd"
ROOT=""
DO_RESTART=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)
            ROOT="${2:?--root requires a directory}"
            shift 2
            ;;
        --env-file)
            export ROBOTHOR_ENV_FILE="${2:?--env-file requires a file}"
            shift 2
            ;;
        --restart)
            DO_RESTART=1
            shift
            ;;
        *)
            echo "usage: install-units.sh [--root DIR] [--env-file FILE] [--restart]" >&2
            exit 1
            ;;
    esac
done

SYSTEM_DIR="${ROOT}/etc/systemd/system"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

log() { echo "[install-units] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ── Render every template first ──────────────────────────────────────────────
# All-or-nothing: a render failure aborts before anything touches the target,
# so a half-updated unit set cannot exist.
shopt -s nullglob
templates=(
    "$SRC_DIR"/robothor-*.service
    "$SRC_DIR"/robothor-*.timer
    "$SRC_DIR"/robothor-*.path
)
[[ ${#templates[@]} -gt 0 ]] || die "no robothor-* unit templates found in ${SRC_DIR}"

rendered_rel=()
for src in "${templates[@]}"; do
    name="$(basename "$src")"
    bash "$RENDER" "$src" "${TMP_DIR}/${name}" || die "render failed for ${name}"
    rendered_rel+=("$name")
done
for dropin in "$SRC_DIR"/robothor-*.service.d/*.conf; do
    rel="$(basename "$(dirname "$dropin")")/$(basename "$dropin")"
    mkdir -p "${TMP_DIR}/$(dirname "$rel")"
    bash "$RENDER" "$dropin" "${TMP_DIR}/${rel}" || die "render failed for ${rel}"
    rendered_rel+=("$rel")
done

# ── Verify gate ───────────────────────────────────────────────────────────────
# Skipped under --root (test mode) and when systemd-analyze is absent: verify
# resolves ExecStart binaries and referenced units against THIS box, so the
# full check only means something at real install time. The renderer's
# structural gate (no unexpanded placeholders, no %h, parseable content) has
# already run on every file above.
if [[ -z "$ROOT" ]] && command -v systemd-analyze >/dev/null 2>&1; then
    services=( "$TMP_DIR"/robothor-*.service )
    verify=( systemd-analyze verify )
    # Template units (robothor-alert@.service) need an instance to verify;
    # --instance exists since systemd 253. On older systemd, verify the
    # non-template services only.
    if systemd-analyze --help 2>&1 | grep -q -- '--instance'; then
        verify+=( --instance=verify )
    else
        log "systemd-analyze lacks --instance; skipping verify for template units"
        filtered=()
        for s in "${services[@]}"; do
            [[ "$(basename "$s")" == *@.service ]] || filtered+=("$s")
        done
        services=( "${filtered[@]}" )
    fi
    "${verify[@]}" "${services[@]}" || die "systemd-analyze verify failed — nothing installed"
    log "systemd-analyze verify passed (${#services[@]} services)"
else
    log "skipping systemd-analyze verify (--root test mode or systemd-analyze absent)"
fi

# ── Install ───────────────────────────────────────────────────────────────────
install_one() {
    local rel="$1"
    local src="${TMP_DIR}/${rel}" dest="${SYSTEM_DIR}/${rel}"
    mkdir -p "$(dirname "$dest")"
    if [[ ! -f "$dest" ]]; then
        install -m 0644 "$src" "$dest"
        log "installed ${dest}"
    elif ! cmp -s "$src" "$dest"; then
        install -m 0644 "$src" "$dest"
        log "updated ${dest}"
    else
        chmod 0644 "$dest"
        log "unchanged ${dest}"
    fi
}

# Idempotent install of a single file OUTSIDE SYSTEM_DIR, reporting
# installed / updated / unchanged exactly like install_one above.
#
# Both callers below hand-rolled this instead of sharing it, and the tmpfiles
# one logged "installed:" unconditionally — so a second run always claimed to
# have installed something and broke the idempotency contract
# (tests/test_install_units.py::test_installer_second_run_reports_unchanged).
install_file() {
    local src="$1" dest="$2" mode="$3"
    install -d -m 0755 "$(dirname "$dest")"
    if [[ ! -f "$dest" ]]; then
        install -m "$mode" "$src" "$dest"
        log "installed: ${dest#"$ROOT"}"
    elif ! cmp -s "$src" "$dest"; then
        install -m "$mode" "$src" "$dest"
        log "updated: ${dest#"$ROOT"}"
    else
        chmod "$mode" "$dest"
        log "unchanged: ${dest#"$ROOT"}"
    fi
}

for rel in "${rendered_rel[@]}"; do
    install_one "$rel"
done

# ── Privileged helpers ────────────────────────────────────────────────────────
# robothor-restart.service executes this as ROOT. It must live OUTSIDE the repo:
# the engine runs with ReadWritePaths=<workspace>, so a root handler executed
# from inside the repo could be rewritten by an injected agent — exactly the
# escalation #205 closed. Install it root-owned, not group- or world-writable.
HELPER_SRC="${REPO_ROOT}/infra/bin/robothor-restart-handler.sh"
HELPER_DST="${ROOT}/usr/local/lib/robothor/robothor-restart-handler.sh"
if [[ -f "$HELPER_SRC" ]]; then
    install_file "$HELPER_SRC" "$HELPER_DST" 0755
else
    log "WARNING: ${HELPER_SRC} missing — robothor-restart.service will fail."
fi

# ── tmpfiles.d templates ─────────────────────────────────────────────────────
# The runtime and state directories the units need before they first run: the
# restart broker's request directory (0700 — the FILENAME is the
# authorization), and the backup guard's last-good marker directory.
#
# Rendered like every other template. A tmpfiles.d line's user/group columns
# are POSITIONAL — no `User=` prefix — so render-unit.sh needs --tmpfiles to
# see them. Copying it raw (what this used to do) installs whatever account the
# template happens to name, which on any instance but the author's does not
# exist: systemd-tmpfiles then chowns the request directory away from the
# engine and the broker silently stops working, with no error, because the
# failure is a permission denial inside the engine.
#
# The whole directory, not one hardcoded filename. This block named
# robothor-restart.conf literally, so the second template to arrive would have
# been gated by every tmpfiles test in tests/test_install_units.py and still
# never installed on any box — a control that is correct, tested, and inert.
tmpfiles_confs=( "${REPO_ROOT}"/infra/tmpfiles/*.conf )
if [[ ${#tmpfiles_confs[@]} -eq 0 ]]; then
    log "WARNING: no tmpfiles templates in ${REPO_ROOT}/infra/tmpfiles — the"
    log "WARNING: restart request directory will not be created and the broker"
    log "WARNING: will never fire."
fi
for tmpfiles_src in "${tmpfiles_confs[@]}"; do
    tmpfiles_name="$(basename "$tmpfiles_src")"
    tmpfiles_dst="${ROOT}/etc/tmpfiles.d/${tmpfiles_name}"
    bash "$RENDER" --tmpfiles "$tmpfiles_src" "${TMP_DIR}/${tmpfiles_name}" \
        || die "render failed for ${tmpfiles_name}"
    install_file "${TMP_DIR}/${tmpfiles_name}" "$tmpfiles_dst" 0644
    # Unconditional, not gated on change: /run is a tmpfs, so the directory is
    # gone after every reboot even when the conf itself is unchanged.
    if [[ -z "$ROOT" ]]; then
        systemd-tmpfiles --create "$tmpfiles_dst" || true
    fi
done

# ── Post-install invariants ───────────────────────────────────────────────────
# Every service sources /etc/robothor/robothor.env; a missing file means
# nothing starts. Warn here, once, rather than letting each unit fail at boot.
if [[ ! -r "${ROOT}/etc/robothor/robothor.env" ]]; then
    log "WARNING: ${ROOT}/etc/robothor/robothor.env does not exist — the units"
    log "WARNING: reference it via EnvironmentFile= and will fail to start."
    log "WARNING: Copy infra/systemd/robothor.env.example there and fill it in."
fi

# ── Restart each unit once ────────────────────────────────────────────────────
# The set is DERIVED from the rendered units, not hardcoded: the secrets
# oneshot plus every unit that Requires= it. Adding a consumer to the fleet
# adds it here without anyone remembering to.
SECRETS_UNIT="robothor-secrets.service"
restart_set=()
if [[ -f "${TMP_DIR}/${SECRETS_UNIT}" ]]; then
    restart_set+=("$SECRETS_UNIT")
    for svc in "$TMP_DIR"/robothor-*.service; do
        name="$(basename "$svc")"
        [[ "$name" == "$SECRETS_UNIT" ]] && continue
        if grep -qxF "Requires=${SECRETS_UNIT}" "$svc"; then
            restart_set+=("$name")
        fi
    done
fi

if [[ ${#restart_set[@]} -eq 0 ]]; then
    log "WARNING: ${SECRETS_UNIT} is not among the templates — nothing to coalesce"
elif [[ "$DO_RESTART" -eq 1 ]]; then
    # The SAME lock the restart broker (infra/bin/robothor-restart-handler.sh)
    # takes, spelled identically — tests/test_install_units.py keeps the two
    # lines equal. Held from here to exit: neither side can enqueue a restart
    # while the other is deciding its transaction.
    LOCK_FILE="${ROBOTHOR_RESTART_LOCK:-/run/lock/robothor-restart.lock}"
    exec 9>"$LOCK_FILE"
    flock 9

    systemctl daemon-reload

    # A unit that already has a job queued or running (`Job=` non-empty) is
    # left to that job: it starts the unit with the code now on disk, and a
    # restart on top of it is exactly the superseding job that kills
    # ExecStartPre. The broker applies the same rule.
    targets=()
    for unit in "${restart_set[@]}"; do
        job="$(systemctl show -p Job --value "$unit" 2>/dev/null || true)"
        if [[ -n "$job" ]]; then
            log "skipping ${unit} — a job is already queued for it (${job}); it will come up on that job"
            continue
        fi
        targets+=("$unit")
    done

    if [[ ${#targets[@]} -eq 0 ]]; then
        log "every unit already has a job queued — nothing to restart"
    else
        log "one restart transaction: ${targets[*]}"
        # Not under set -e: a unit that fails to start after the transaction
        # is enqueued must not abort this script mid-report. Every unit's
        # state is printed, and the exit code says whether any failed.
        restart_rc=0
        systemctl restart "${targets[@]}" || restart_rc=$?
        failed_units=0
        for unit in "${targets[@]}"; do
            state="$(systemctl is-active "$unit" 2>/dev/null || true)"
            log "  ${unit}: ${state:-unknown}"
            [[ "$state" == "active" || "$state" == "activating" ]] || failed_units=$((failed_units + 1))
        done
        if [[ "$restart_rc" -ne 0 || "$failed_units" -gt 0 ]]; then
            log "ERROR: restart transaction exited ${restart_rc}; ${failed_units} unit(s) not active —"
            log "ERROR: journalctl -u <unit> -n 50 for each one above"
            log "done (${#rendered_rel[@]} units)"
            exit 1
        fi
        log "restarted (once each): ${targets[*]}"
    fi
else
    log "next, as ONE command — do not restart ${SECRETS_UNIT} separately or restart"
    log "the consumers again afterwards; a second restart job supersedes the first"
    log "and kills its ExecStartPre (Failed with result 'signal' — a page):"
    log "  sudo systemctl daemon-reload && sudo systemctl restart ${restart_set[*]}"
    log "or re-run with --restart to have this script do exactly that."
fi
log "done (${#rendered_rel[@]} units)"
