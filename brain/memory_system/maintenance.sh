#!/bin/bash
# Robothor Memory Maintenance Script
# Runs nightly at 3 AM via system crontab.
# Calls lifecycle maintenance directly (importance scoring, decay, consolidation, pruning).
# Stats logged after maintenance completes.
set -euo pipefail

SCRIPT_DIR="/home/philip/robothor/brain/memory_system"
LOG_FILE="$SCRIPT_DIR/maintenance.log"
VENV="$SCRIPT_DIR/venv/bin/python3"
STATUS_FILE="$SCRIPT_DIR/../memory/maintenance-status.json"

# Rotate log if >1MB
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$LOG_SIZE" -gt 1048576 ]; then
        mv "$LOG_FILE" "$LOG_FILE.old"
    fi
fi

# Write error status on any failure
trap 'echo "{\"status\":\"error\",\"timestamp\":\"$(date -Iseconds)\",\"line\":$LINENO}" > "$STATUS_FILE"; echo "ERROR at line $LINENO" >> "$LOG_FILE"' ERR

echo "========================================" >> "$LOG_FILE"
echo "Memory Maintenance - $(date)" >> "$LOG_FILE"
echo "========================================" >> "$LOG_FILE"

cd "$SCRIPT_DIR"

# Step 1: Lifecycle maintenance (importance scoring, decay, consolidation, pruning)
echo "--- Lifecycle maintenance ---" >> "$LOG_FILE"
timeout 900 $VENV -c "
import asyncio, json
from robothor.memory.lifecycle import run_lifecycle_maintenance
result = asyncio.run(run_lifecycle_maintenance())
print(json.dumps(result, default=str))
" >> "$LOG_FILE" 2>&1

# Step 2: Stats
echo "--- Stats after maintenance ---" >> "$LOG_FILE"
timeout 120 $VENV -c "
from robothor.memory.facts import get_memory_stats
import json
print(json.dumps(get_memory_stats(), default=str))
" >> "$LOG_FILE" 2>&1

echo "Maintenance complete: $(date)" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# Write detailed status
$VENV -c "
import json
from datetime import datetime
from robothor.memory.facts import get_memory_stats
stats = get_memory_stats()
status = {
    'status': 'ok',
    'timestamp': datetime.now().isoformat(),
    **stats,
}
print(json.dumps(status, default=str))
" > "$STATUS_FILE" 2>> "$LOG_FILE"
