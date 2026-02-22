#!/usr/bin/env bash
# ============================================================================
# Robothor Setup Script
# ============================================================================
# Initializes the Robothor infrastructure: database, models, config, services.
# Safe to run multiple times (idempotent).
#
# Usage:
#   ./setup.sh                    # Core setup (DB + models + config)
#   ./setup.sh --systemd          # Also install systemd services
#   ./setup.sh --docker           # Use Docker Compose for infra
#   ./setup.sh --help             # Show help
# ============================================================================

set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Defaults ────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="/etc/robothor/robothor.env"
INSTALL_SYSTEMD=false
USE_DOCKER=false

DB_HOST="${ROBOTHOR_DB_HOST:-127.0.0.1}"
DB_PORT="${ROBOTHOR_DB_PORT:-5432}"
DB_NAME="${ROBOTHOR_DB_NAME:-robothor_memory}"
DB_USER="${ROBOTHOR_DB_USER:-robothor}"
OLLAMA_HOST="${ROBOTHOR_OLLAMA_HOST:-http://127.0.0.1}"
OLLAMA_PORT="${ROBOTHOR_OLLAMA_PORT:-11434}"

# ── Parse Arguments ────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Robothor Infrastructure Setup

Options:
  --systemd        Install and enable systemd service files
  --docker         Start infrastructure via Docker Compose
  --env FILE       Path to environment file (default: /etc/robothor/robothor.env)
  --skip-models    Skip pulling Ollama models
  --skip-db        Skip database migration
  --help           Show this help message

Environment variables:
  ROBOTHOR_DB_HOST          Database host (default: 127.0.0.1)
  ROBOTHOR_DB_PORT          Database port (default: 5432)
  ROBOTHOR_DB_NAME          Database name (default: robothor_memory)
  ROBOTHOR_DB_USER          Database user (default: robothor)
  ROBOTHOR_DB_PASSWORD      Database password (required for migration)
  ROBOTHOR_OLLAMA_HOST      Ollama host (default: http://127.0.0.1)
  ROBOTHOR_OLLAMA_PORT      Ollama port (default: 11434)

Examples:
  # Basic setup with defaults
  ./setup.sh

  # Full setup with Docker and systemd
  ./setup.sh --docker --systemd

  # Skip model download (if Ollama models already pulled)
  ./setup.sh --skip-models
EOF
    exit 0
}

SKIP_MODELS=false
SKIP_DB=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --systemd)   INSTALL_SYSTEMD=true; shift ;;
        --docker)    USE_DOCKER=true; shift ;;
        --env)       ENV_FILE="$2"; shift 2 ;;
        --skip-models) SKIP_MODELS=true; shift ;;
        --skip-db)   SKIP_DB=true; shift ;;
        --help|-h)   usage ;;
        *)           err "Unknown option: $1"; usage ;;
    esac
done

# ── Source environment if available ─────────────────────────────────────────

if [[ -f "$ENV_FILE" ]]; then
    info "Loading environment from $ENV_FILE"
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
fi

# ── Check Prerequisites ────────────────────────────────────────────────────

check_tool() {
    if command -v "$1" &>/dev/null; then
        ok "$1 found: $(command -v "$1")"
        return 0
    else
        warn "$1 not found"
        return 1
    fi
}

info "Checking prerequisites..."
echo

MISSING=0

if ! check_tool psql; then
    err "psql is required for database setup. Install: apt install postgresql-client"
    MISSING=1
fi

if ! check_tool redis-cli; then
    warn "redis-cli not found. Install: apt install redis-tools"
    warn "Redis health checks will be unavailable, but setup can continue."
fi

if [[ "$SKIP_MODELS" == false ]]; then
    if ! check_tool ollama; then
        if ! check_tool curl; then
            err "Either ollama or curl is required for model setup."
            MISSING=1
        else
            warn "ollama CLI not found. Will use Ollama HTTP API via curl."
        fi
    fi
fi

if [[ "$USE_DOCKER" == true ]]; then
    if ! check_tool docker; then
        err "docker is required for --docker mode. Install: https://docs.docker.com/engine/install/"
        MISSING=1
    fi
fi

if [[ "$INSTALL_SYSTEMD" == true ]]; then
    if ! check_tool systemctl; then
        err "systemctl is required for --systemd mode."
        MISSING=1
    fi
fi

if [[ $MISSING -eq 1 ]]; then
    err "Missing required tools. Install them and re-run."
    exit 1
fi

echo
info "All required tools available."
echo

# ── Create Directories ─────────────────────────────────────────────────────

info "Creating directories..."

DIRS=(
    "/etc/robothor"
    "${ROBOTHOR_WORKSPACE:-/var/lib/robothor}"
    "${ROBOTHOR_WORKSPACE:-/var/lib/robothor}/faces"
    "${ROBOTHOR_WORKSPACE:-/var/lib/robothor}/memory"
    "${ROBOTHOR_LOG_DIR:-/var/log/robothor}"
)

for dir in "${DIRS[@]}"; do
    if [[ ! -d "$dir" ]]; then
        sudo mkdir -p "$dir"
        ok "Created $dir"
    else
        ok "Exists: $dir"
    fi
done

# Set ownership if running as root
if [[ $EUID -eq 0 ]]; then
    ROBOTHOR_USER="${ROBOTHOR_USER:-robothor}"
    if id "$ROBOTHOR_USER" &>/dev/null; then
        sudo chown -R "$ROBOTHOR_USER:$ROBOTHOR_USER" "${ROBOTHOR_WORKSPACE:-/var/lib/robothor}"
        sudo chown -R "$ROBOTHOR_USER:$ROBOTHOR_USER" "${ROBOTHOR_LOG_DIR:-/var/log/robothor}"
    fi
fi

echo

# ── Create Default Config ──────────────────────────────────────────────────

if [[ ! -f "$ENV_FILE" ]]; then
    info "Creating default config at $ENV_FILE"
    sudo cp "$SCRIPT_DIR/robothor.env.example" "$ENV_FILE"
    sudo chmod 640 "$ENV_FILE"
    ok "Config created. Edit $ENV_FILE to set ROBOTHOR_DB_PASSWORD and other values."
    warn "IMPORTANT: Set ROBOTHOR_DB_PASSWORD in $ENV_FILE before running migrations."
else
    ok "Config exists: $ENV_FILE"
fi

echo

# ── Docker Compose (optional) ──────────────────────────────────────────────

if [[ "$USE_DOCKER" == true ]]; then
    info "Starting Docker Compose infrastructure..."
    cd "$SCRIPT_DIR"

    # Create .env for Docker Compose if it doesn't exist
    if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
        if [[ -f "$ENV_FILE" ]]; then
            info "Linking $ENV_FILE for Docker Compose"
            cp "$ENV_FILE" "$SCRIPT_DIR/.env"
        else
            warn "No .env file found. Docker Compose will use defaults."
        fi
    fi

    docker compose up -d

    info "Waiting for PostgreSQL to be ready..."
    for i in $(seq 1 30); do
        if docker compose exec -T postgres pg_isready -U "${DB_USER}" -d "${DB_NAME}" &>/dev/null; then
            ok "PostgreSQL is ready"
            break
        fi
        if [[ $i -eq 30 ]]; then
            err "PostgreSQL did not become ready in 30s"
            exit 1
        fi
        sleep 1
    done

    info "Waiting for Redis to be ready..."
    for i in $(seq 1 15); do
        if docker compose exec -T redis redis-cli ping &>/dev/null; then
            ok "Redis is ready"
            break
        fi
        if [[ $i -eq 15 ]]; then
            err "Redis did not become ready in 15s"
            exit 1
        fi
        sleep 1
    done

    echo
fi

# ── Database Migration ─────────────────────────────────────────────────────

if [[ "$SKIP_DB" == false ]]; then
    info "Running database migration..."

    # Determine password source
    DB_PASSWORD="${ROBOTHOR_DB_PASSWORD:-}"
    if [[ -z "$DB_PASSWORD" && "$USE_DOCKER" == false ]]; then
        warn "ROBOTHOR_DB_PASSWORD not set. Trying peer authentication."
    fi

    MIGRATION_FILE="$SCRIPT_DIR/migrations/001_init.sql"

    if [[ ! -f "$MIGRATION_FILE" ]]; then
        err "Migration file not found: $MIGRATION_FILE"
        exit 1
    fi

    if [[ "$USE_DOCKER" == true ]]; then
        # Run migration inside the Docker container
        docker compose exec -T postgres \
            psql -U "$DB_USER" -d "$DB_NAME" -f /docker-entrypoint-initdb.d/001_init.sql
    else
        # Run migration against a local or remote PostgreSQL
        export PGPASSWORD="$DB_PASSWORD"
        psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$MIGRATION_FILE"
        unset PGPASSWORD
    fi

    ok "Database migration complete"
    echo
fi

# ── Pull Ollama Models ──────────────────────────────────────────────────────

if [[ "$SKIP_MODELS" == false ]]; then
    info "Pulling Ollama models..."

    OLLAMA_URL="${OLLAMA_HOST}:${OLLAMA_PORT}"

    pull_model() {
        local model="$1"
        info "Pulling $model ..."

        if command -v ollama &>/dev/null; then
            ollama pull "$model"
        else
            # Fall back to HTTP API
            curl -s -X POST "${OLLAMA_URL}/api/pull" \
                -d "{\"name\": \"$model\"}" \
                --max-time 600 | while IFS= read -r line; do
                    status=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))" 2>/dev/null || echo "")
                    if [[ -n "$status" ]]; then
                        printf "\r  %s" "$status"
                    fi
                done
            echo
        fi

        ok "Pulled $model"
    }

    MODELS=(
        "${ROBOTHOR_EMBEDDING_MODEL:-qwen3-embedding:0.6b}"
        "${ROBOTHOR_RERANKER_MODEL:-Qwen3-Reranker-0.6B:F16}"
    )

    for model in "${MODELS[@]}"; do
        pull_model "$model"
    done

    echo
    info "Note: The generation model (${ROBOTHOR_GENERATION_MODEL:-qwen3:8b}) is loaded on demand."
    info "Pull it now with: ollama pull ${ROBOTHOR_GENERATION_MODEL:-qwen3:8b}"
    echo
fi

# ── Install Systemd Services (optional) ────────────────────────────────────

if [[ "$INSTALL_SYSTEMD" == true ]]; then
    info "Installing systemd service files..."

    SERVICES=(
        "robothor-api.service"
        "robothor-vision.service"
        "robothor-bridge.service"
    )

    for svc in "${SERVICES[@]}"; do
        src="$SCRIPT_DIR/systemd/$svc"
        dst="/etc/systemd/system/$svc"

        if [[ ! -f "$src" ]]; then
            warn "Service file not found: $src — skipping"
            continue
        fi

        sudo cp "$src" "$dst"
        ok "Installed $dst"
    done

    sudo systemctl daemon-reload
    ok "systemd daemon reloaded"

    echo
    info "Services installed but NOT started. To enable and start:"
    for svc in "${SERVICES[@]}"; do
        echo "  sudo systemctl enable --now $svc"
    done
    echo
    warn "Before starting services, edit /etc/systemd/system/robothor-*.service"
    warn "to set the correct User= for your system."
fi

# ── Verify ──────────────────────────────────────────────────────────────────

echo
info "============================================"
info "  Robothor Setup Complete"
info "============================================"
echo

# Check what's running
if [[ "$USE_DOCKER" == true ]]; then
    info "Docker services:"
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || true
    echo
fi

if [[ "$SKIP_DB" == false ]]; then
    ok "Database: $DB_NAME on $DB_HOST:$DB_PORT"
fi

if [[ "$SKIP_MODELS" == false ]]; then
    ok "Ollama models: embedding + reranker pulled"
fi

ok "Config: $ENV_FILE"
ok "Workspace: ${ROBOTHOR_WORKSPACE:-/var/lib/robothor}"
ok "Logs: ${ROBOTHOR_LOG_DIR:-/var/log/robothor}"

echo
info "Next steps:"
echo "  1. Edit $ENV_FILE with your database password and settings"
echo "  2. Start the API server: robothor serve"
echo "  3. Test the memory system: robothor store 'Hello world'"
echo "  4. Search: robothor search 'hello'"
echo
