#!/bin/bash
# Robothor full system backup to LUKS-encrypted external SSD
# Daily at 4:30 AM via cron
# First run: ~24 GB. Subsequent: incremental (rsync).

set -euo pipefail

SSD_MOUNT="/mnt/robothor-backup"
BACKUP_ROOT="$SSD_MOUNT/robothor"
DATE=$(date +%Y%m%d)
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
LOG="$HOME/robothor/scripts/backup.log"
MANIFEST="$BACKUP_ROOT/backup-manifest.txt"
MIN_FREE_GB=10

log() { echo "[$TIMESTAMP] $1" >> "$LOG"; }

# ── Pre-flight checks ───────────────────────────────────────────

# Check SSD is mounted — fail loudly if not
if ! mountpoint -q "$SSD_MOUNT" 2>/dev/null; then
    log "ERROR: SSD not mounted at $SSD_MOUNT — backup FAILED"
    exit 1
fi

# Check minimum free space (10 GB)
AVAIL_KB=$(df --output=avail "$SSD_MOUNT" | tail -1 | tr -d ' ')
AVAIL_GB=$((AVAIL_KB / 1048576))
if [ "$AVAIL_GB" -lt "$MIN_FREE_GB" ]; then
    log "ERROR: Only ${AVAIL_GB}GB free on SSD (need ${MIN_FREE_GB}GB) — backup FAILED"
    exit 1
fi

mkdir -p "$BACKUP_ROOT/latest" "$BACKUP_ROOT/db" "$BACKUP_ROOT/docker-volumes" "$BACKUP_ROOT/ollama" "$BACKUP_ROOT/docker-images"

log "Starting daily backup... (${AVAIL_GB}GB free on SSD)"

# ── Rsync excludes ───────────────────────────────────────────────

EXCLUDES=(
    --exclude='venv/'
    --exclude='.git/'
    --exclude='node_modules/'
    --exclude='__pycache__/'
    --exclude='.mypy_cache/'
    --exclude='*.pyc'
    --exclude='.pytest_cache/'
    --exclude='.next/'
)

# ── Project directories ─────────────────────────────────────────

# robothor root (brain/ is now in-repo, no longer a separate directory)
rsync -a --delete "${EXCLUDES[@]}" \
    --exclude='tunnel' \
    "$HOME/robothor/" "$BACKUP_ROOT/latest/robothor/" 2>> "$LOG"

# ── Hidden config directories ───────────────────────────────────

rsync -a --delete "$HOME/.config/robothor/" "$BACKUP_ROOT/latest/config-robothor/" 2>> "$LOG"  # includes garmin_tokens/
rsync -a --delete "$HOME/.cloudflared/" "$BACKUP_ROOT/latest/cloudflared/" 2>> "$LOG"

# ── System service files ────────────────────────────────────────

mkdir -p "$BACKUP_ROOT/latest/systemd-services"
sudo cp /etc/systemd/system/robothor-*.service "$BACKUP_ROOT/latest/systemd-services/" 2>> "$LOG"
sudo cp /etc/systemd/system/mediamtx-webcam.service "$BACKUP_ROOT/latest/systemd-services/" 2>> "$LOG" || true

# ── Credentials ─────────────────────────────────────────────────

mkdir -p "$BACKUP_ROOT/latest/credentials"
cp "$HOME/.bashrc" "$BACKUP_ROOT/latest/credentials/bashrc" 2>> "$LOG"
if [ -f "$HOME/robothor/crm/.env" ]; then
    cp "$HOME/robothor/crm/.env" "$BACKUP_ROOT/latest/credentials/crm-env" 2>> "$LOG"
fi
# SOPS+age secrets (encrypted file + private key)
if [ -d /etc/robothor ]; then
    sudo cp /etc/robothor/age.key "$BACKUP_ROOT/latest/credentials/age.key" 2>> "$LOG" || true
    sudo cp /etc/robothor/secrets.enc.json "$BACKUP_ROOT/latest/credentials/secrets.enc.json" 2>> "$LOG" || true
    # robothor.env is the instance's entire operational posture — every flag the
    # engine reads at boot, including ROBOTHOR_LAST_RESORT_MODEL, which is the
    # only reason the fleet keeps answering when the cloud provider is capped.
    # It lives outside $HOME, so the repo rsync above never saw it: this backup
    # covered the KEYS to the instance and not the instance's CONFIGURATION,
    # and a rebuild would have come back silently mis-postured: guardrails off,
    # offline tier unset, contracts disabled.
    sudo cp /etc/robothor/robothor.env "$BACKUP_ROOT/latest/credentials/robothor.env" 2>> "$LOG" || true
fi

# ── PostgreSQL dumps (30-day retention) ─────────────────────────

for db in robothor_memory; do
    DUMP_FILE="$BACKUP_ROOT/db/${db}-${DATE}.sql.gz"
    if [ ! -f "$DUMP_FILE" ]; then
        pg_dump "$db" 2>> "$LOG" | gzip > "$DUMP_FILE"
        log "  DB dump: $db ($(du -sh "$DUMP_FILE" | cut -f1))"
    else
        log "  DB dump: $db — already exists for today, skipping"
    fi
done

# Retention: delete dumps older than 30 days
find "$BACKUP_ROOT/db" -name "*.sql.gz" -mtime +30 -delete 2>> "$LOG"

# ── Docker volumes ──────────────────────────────────────────────

# (Docker volume backups removed — no persistent volumes needing backup)

# ── Ollama models ────────────────────────────────────────────────

OLLAMA_DIR="/usr/share/ollama/.ollama/models"
if [ -d "$OLLAMA_DIR" ]; then
    sudo rsync -a --delete "$OLLAMA_DIR/" "$BACKUP_ROOT/ollama/" 2>> "$LOG"
    log "  Ollama models: $(sudo du -sh "$OLLAMA_DIR" | cut -f1)"
fi

# ── Docker images (saved as tarballs) ───────────────────────────

# (Docker image backups removed — no custom images needing backup)

# ── Manifests ────────────────────────────────────────────────────

crontab -l > "$BACKUP_ROOT/latest/crontab.bak" 2>> "$LOG"
ollama list > "$BACKUP_ROOT/latest/ollama-models.txt" 2>> "$LOG"

# ── Verification manifest ───────────────────────────────────────

# ── Docker volumes ───────────────────────────────────────────────
# Until 2026-08-27 this directory was created and left empty while the manifest
# printed a HARDCODED `echo "  (none)"` -- so 4.3GB across 8 named volumes
# (Impetus One's Postgres, uptime-kuma's config, kokoro's models,
# programmatic-resources' pgdata) had NO backup at all, and the report stated
# that as if there were nothing to protect. A manifest that prints a constant is
# worse than one with a missing section.
#
# Runs BEFORE the manifest block below, which reads the .manifest it writes.
backup_docker_volumes() {
    local out="$BACKUP_ROOT/docker-volumes"
    local man="$out/.manifest"
    : > "$man"

    # `command -v docker` only proves the BINARY exists. This service runs as a
    # user deliberately not in the docker group, so the daemon socket needs
    # sudo. The first version checked the binary, got permission denied from the
    # daemon, and silently wrote an empty manifest. Probe the real capability.
    local DOCKER="docker"
    if ! docker volume ls >/dev/null 2>&1; then
        if sudo -n docker volume ls >/dev/null 2>&1; then
            DOCKER="sudo -n docker"
        else
            echo "  ERROR: cannot reach the docker daemon (tried direct and sudo -n)" >> "$man"
            log "ERROR: docker volumes NOT backed up -- no daemon access"
            return 1
        fi
    fi

    local count=0 failed=0
    while read -r vol; do
        [ -n "$vol" ] || continue
        # Anonymous volumes are 64 hex chars and docker recreates them.
        if printf '%s' "$vol" | grep -qE '^[0-9a-f]{64}$'; then continue; fi
        if $DOCKER run --rm -v "$vol":/src:ro -v "$out":/dst alpine:latest \
             tar czf "/dst/${vol}.tar.gz" -C /src . 2>/dev/null; then
            echo "  $vol: $(du -h "$out/${vol}.tar.gz" 2>/dev/null | cut -f1)" >> "$man"
            count=$((count + 1))
        else
            echo "  $vol: FAILED" >> "$man"
            failed=$((failed + 1))
        fi
    done < <($DOCKER volume ls --format '{{.Name}}')

    log "Docker volumes: $count backed up, $failed failed"
    [ "$failed" -eq 0 ]
}
backup_docker_volumes || log "WARNING: docker volume backup incomplete"

{
    echo "# Robothor Backup Manifest — $TIMESTAMP"
    echo ""
    echo "## Disk Usage"
    du -sh "$BACKUP_ROOT/latest"/* "$BACKUP_ROOT/db" "$BACKUP_ROOT/docker-volumes" "$BACKUP_ROOT/ollama" "$BACKUP_ROOT/docker-images" 2>/dev/null | sort -rh
    echo ""
    echo "## Database Dumps (today)"
    for db in robothor_memory; do
        DUMP_FILE="$BACKUP_ROOT/db/${db}-${DATE}.sql.gz"
        if [ -f "$DUMP_FILE" ]; then
            SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
            MD5=$(md5sum "$DUMP_FILE" | cut -d' ' -f1)
            echo "  $db: $SIZE (md5: $MD5)"
        fi
    done
    echo ""
    echo "## Docker Volumes"
    if [ -s "$BACKUP_ROOT/docker-volumes/.manifest" ]; then
        cat "$BACKUP_ROOT/docker-volumes/.manifest"
    else
        echo "  (none backed up)"
    fi
    echo ""
    echo "## SSD Space"
    df -h "$SSD_MOUNT" | tail -1
} > "$MANIFEST"

TOTAL_SIZE=$(du -sh "$BACKUP_ROOT" | cut -f1)
log "Backup complete. ${TOTAL_SIZE} total on SSD."
