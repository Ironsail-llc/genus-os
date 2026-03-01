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

for dir in clawd; do
    if [ -d "$HOME/$dir" ]; then
        rsync -a --delete "${EXCLUDES[@]}" \
            "$HOME/$dir/" "$BACKUP_ROOT/latest/$dir/" 2>> "$LOG"
    fi
done

# robothor root (excluding symlinks to avoid duplicating clawd/etc)
rsync -a --delete "${EXCLUDES[@]}" \
    --exclude='brain' \
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
fi

# ── PostgreSQL dumps (30-day retention) ─────────────────────────

for db in robothor_memory vaultwarden; do
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

for vol in crm_vaultwarden-data; do
    VOLPATH=$(sudo docker volume inspect "$vol" --format '{{.Mountpoint}}' 2>/dev/null) || true
    if [ -n "$VOLPATH" ] && [ -d "$VOLPATH" ]; then
        sudo rsync -a --delete "$VOLPATH/" "$BACKUP_ROOT/docker-volumes/$vol/" 2>> "$LOG"
        log "  Docker volume: $vol"
    fi
done

# ── Ollama models ────────────────────────────────────────────────

OLLAMA_DIR="/usr/share/ollama/.ollama/models"
if [ -d "$OLLAMA_DIR" ]; then
    sudo rsync -a --delete "$OLLAMA_DIR/" "$BACKUP_ROOT/ollama/" 2>> "$LOG"
    log "  Ollama models: $(sudo du -sh "$OLLAMA_DIR" | cut -f1)"
fi

# ── Docker images (saved as tarballs) ───────────────────────────

for img in vaultwarden/server:latest; do
    SAFE_NAME=$(echo "$img" | tr '/:' '_')
    TAR_FILE="$BACKUP_ROOT/docker-images/${SAFE_NAME}.tar"
    # Only re-export if image ID changed (check via digest)
    IMG_ID=$(sudo docker image inspect "$img" --format '{{.Id}}' 2>/dev/null) || true
    ID_FILE="$BACKUP_ROOT/docker-images/${SAFE_NAME}.id"
    PREV_ID=""
    [ -f "$ID_FILE" ] && PREV_ID=$(cat "$ID_FILE")
    if [ -n "$IMG_ID" ] && [ "$IMG_ID" != "$PREV_ID" ]; then
        sudo docker save "$img" -o "$TAR_FILE" 2>> "$LOG"
        echo "$IMG_ID" > "$ID_FILE"
        log "  Docker image: $img ($(du -sh "$TAR_FILE" | cut -f1))"
    elif [ -f "$TAR_FILE" ]; then
        log "  Docker image: $img — unchanged, skipping"
    fi
done

# ── Manifests ────────────────────────────────────────────────────

crontab -l > "$BACKUP_ROOT/latest/crontab.bak" 2>> "$LOG"
ollama list > "$BACKUP_ROOT/latest/ollama-models.txt" 2>> "$LOG"

# ── Verification manifest ───────────────────────────────────────

{
    echo "# Robothor Backup Manifest — $TIMESTAMP"
    echo ""
    echo "## Disk Usage"
    du -sh "$BACKUP_ROOT/latest"/* "$BACKUP_ROOT/db" "$BACKUP_ROOT/docker-volumes" "$BACKUP_ROOT/ollama" "$BACKUP_ROOT/docker-images" 2>/dev/null | sort -rh
    echo ""
    echo "## Database Dumps (today)"
    for db in robothor_memory vaultwarden; do
        DUMP_FILE="$BACKUP_ROOT/db/${db}-${DATE}.sql.gz"
        if [ -f "$DUMP_FILE" ]; then
            SIZE=$(du -sh "$DUMP_FILE" | cut -f1)
            MD5=$(md5sum "$DUMP_FILE" | cut -d' ' -f1)
            echo "  $db: $SIZE (md5: $MD5)"
        fi
    done
    echo ""
    echo "## Docker Volumes"
    for vol in crm_vaultwarden-data; do
        if [ -d "$BACKUP_ROOT/docker-volumes/$vol" ]; then
            echo "  $vol: $(du -sh "$BACKUP_ROOT/docker-volumes/$vol" | cut -f1)"
        fi
    done
    echo ""
    echo "## SSD Space"
    df -h "$SSD_MOUNT" | tail -1
} > "$MANIFEST"

TOTAL_SIZE=$(du -sh "$BACKUP_ROOT" | cut -f1)
log "Backup complete. ${TOTAL_SIZE} total on SSD."
