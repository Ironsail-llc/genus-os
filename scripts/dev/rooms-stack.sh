#!/usr/bin/env bash
# The workspaces dry run: the real engine, bridge and dashboard, on this machine.
#
#   scripts/dev/rooms-stack.sh up       # database, seed, then start all three
#   scripts/dev/rooms-stack.sh seed     # (re)seed the companies and people only
#   scripts/dev/rooms-stack.sh status   # what is running, and where
#   scripts/dev/rooms-stack.sh env      # the environment the end-to-end specs read
#   scripts/dev/rooms-stack.sh down     # stop all three
#
# Development only. Needs Postgres and Redis reachable (the QA containers
# genus-qa-pg on :5432 and genus-qa-redis on :6379 by default), a Python venv
# with the repo installed (ROOMS_STACK_PYTHON) and a built dashboard
# (`pnpm build` in app/, or `up --build`).
#
# What is real: every process, the database (its own *_test database, migrated
# by `robothor migrate`), row-level security (the services sign in as a login
# role that is a member of robothor_app), local sign-in, tenant switching,
# events over Redis. What is not: the model, a scripted fake
# (scripts/dev/rooms_stack/fake_llm.py), and the data, one fixture tool
# (sample_sales_figures). No external service is called.
#
# Companies: dryrun-holding (platform tenant; Hal, admin) and its child
# dryrun-company (Alice, Bob: members; Vera: viewer; Dana: admin; Carol: member
# with nothing shared). Sign in as <name>@example.com with ROOMS_STACK_PASSWORD.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STACK_DIR="${ROOMS_STACK_DIR:-${TMPDIR:-/tmp}/genus-rooms-stack}"
PYTHON="${ROOMS_STACK_PYTHON:-python3}"
PG_HOST="${ROOMS_STACK_PG_HOST:-localhost}"
PG_PORT="${ROOMS_STACK_PG_PORT:-5432}"
PG_ADMIN_USER="${ROOMS_STACK_PG_ADMIN_USER:-robothor}"
PG_ADMIN_PASSWORD="${ROOMS_STACK_PG_ADMIN_PASSWORD:-robothor}"
DB_NAME="${ROOMS_STACK_DB:-robothor_rooms_stack_test}"
APP_ROLE="${ROOMS_STACK_APP_ROLE:-rooms_stack_app}"
APP_ROLE_PASSWORD="${ROOMS_STACK_APP_ROLE_PASSWORD:-rooms-stack-local-only}"
REDIS_URL="${ROOMS_STACK_REDIS_URL:-redis://localhost:6379/7}"
ENGINE_PORT="${ROOMS_STACK_ENGINE_PORT:-18871}"
BRIDGE_PORT="${ROOMS_STACK_BRIDGE_PORT:-9171}"
APP_PORT="${ROOMS_STACK_APP_PORT:-3071}"
PASSWORD="${ROOMS_STACK_PASSWORD:-dry-run-password-2026}"
HOLDING="dryrun-holding"
COMPANY="dryrun-company"

case "$DB_NAME" in
  *_test) ;;
  *) echo "rooms stack: ROOMS_STACK_DB must end in _test" >&2; exit 2 ;;
esac

admin_dsn() { echo "dbname=$1 user=$PG_ADMIN_USER password=$PG_ADMIN_PASSWORD host=$PG_HOST port=$PG_PORT"; }

psql_admin() {
  local db="$1"; shift
  "$PYTHON" -I - "$(admin_dsn "$db")" "$@" <<'PY'
import sys
import psycopg2
conn = psycopg2.connect(sys.argv[1])
conn.autocommit = True
cur = conn.cursor()
for statement in sys.argv[2:]:
    cur.execute(statement)
    if cur.description:
        for row in cur.fetchall():
            print("|".join(str(v) for v in row))
PY
}

common_env() {
  cat <<EOF
PYTHONPATH=$ROOT
HOME=$STACK_DIR/home
GENUS_ENVIRONMENT=development
ROBOTHOR_DB_HOST=$PG_HOST
ROBOTHOR_DB_PORT=$PG_PORT
ROBOTHOR_DB_NAME=$DB_NAME
ROBOTHOR_DB_USER=$APP_ROLE
ROBOTHOR_DB_PASSWORD=$APP_ROLE_PASSWORD
ROBOTHOR_RLS_ENABLED=1
REDIS_URL=$REDIS_URL
ROBOTHOR_PLATFORM_TENANT=$HOLDING
GENUS_AUTH_SIGNING_KEY=rooms-stack-signing-key-for-local-use-only
GENUS_LOCAL_LOGIN=true
GENUS_TENANT_SWITCH_MODE=enforce
ROBOTHOR_ENGINE_URL=http://127.0.0.1:1
GENUS_TENANT_ENGINE_URLS='{"$COMPANY": "http://127.0.0.1:$ENGINE_PORT"}'
ROBOTHOR_WORKSPACE=$STACK_DIR/workspace
ROBOTHOR_MANIFEST_DIR=$STACK_DIR/workspace/docs/agents
EOF
}

write_env() {
  mkdir -p "$STACK_DIR/home" "$STACK_DIR/logs" "$STACK_DIR/workspace/docs/agents" "$STACK_DIR/workspace/brain/agents"
  common_env > "$STACK_DIR/common.env"
  {
    common_env
    echo "ROBOTHOR_TENANT_ID=$COMPANY"
    echo "ROBOTHOR_ENGINE_PORT=$ENGINE_PORT"
    echo "GENUS_EVENT_CONSUMER_GROUP=engine-$COMPANY"
    echo "OPENAI_API_KEY=rooms-stack-fake"
    # Approved workspace email is sent "from" here: into the outbox file only.
    echo "ROBOTHOR_AI_EMAIL=assistant@example.com"
  } > "$STACK_DIR/engine.env"
  {
    common_env
    echo "ROBOTHOR_BRIDGE_PORT=$BRIDGE_PORT"
    echo "ROBOTHOR_BRIDGE_HOST=127.0.0.1"
  } > "$STACK_DIR/bridge.env"
  {
    common_env
    echo "PORT=$APP_PORT"
    echo "HOSTNAME=127.0.0.1"
    echo "BRIDGE_URL=http://127.0.0.1:$BRIDGE_PORT"
    echo "AUTH_SECRET=rooms-stack-auth-secret-for-local-use-only"
    echo "AUTH_TRUST_HOST=true"
    echo "AUTH_URL=http://127.0.0.1:$APP_PORT"
    echo "AUTH_OIDC_ISSUER=https://idp.rooms-stack.invalid"
    echo "AUTH_OIDC_CLIENT_ID=rooms-stack"
    echo "AUTH_OIDC_CLIENT_SECRET=rooms-stack"
    echo "GENUS_BRIDGE_SSO_SECRET=rooms-stack-sso-secret"
  } > "$STACK_DIR/app.env"
  cp "$ROOT/scripts/dev/rooms_stack/agents/"*.yaml "$STACK_DIR/workspace/docs/agents/"
  cp "$ROOT/scripts/dev/rooms_stack/agents/"*.md "$STACK_DIR/workspace/brain/agents/"
}

# Run a command with only PATH, TMPDIR and the variables of one env file.
with_env() {
  local file="$1"; shift
  exec env -i PATH="$PATH" TMPDIR="${TMPDIR:-/tmp}" bash -c 'set -a; . "$0"; set +a; exec "$@"' "$file" "$@"
}

database() {
  local exists
  exists="$(psql_admin postgres "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")"
  [ -n "$exists" ] || psql_admin postgres "CREATE DATABASE $DB_NAME"
  ROBOTHOR_DB_HOST="$PG_HOST" ROBOTHOR_DB_PORT="$PG_PORT" ROBOTHOR_DB_NAME="$DB_NAME" \
    ROBOTHOR_DB_USER="$PG_ADMIN_USER" ROBOTHOR_DB_PASSWORD="$PG_ADMIN_PASSWORD" PYTHONPATH="$ROOT" \
    "$PYTHON" -m robothor.cli migrate > "$STACK_DIR/logs/migrate.log" 2>&1 \
    || { tail -20 "$STACK_DIR/logs/migrate.log" >&2; exit 1; }
  local role
  role="$(psql_admin postgres "SELECT 1 FROM pg_roles WHERE rolname = '$APP_ROLE'")"
  [ -n "$role" ] || psql_admin postgres \
    "CREATE ROLE $APP_ROLE LOGIN PASSWORD '$APP_ROLE_PASSWORD' NOSUPERUSER NOBYPASSRLS INHERIT"
  psql_admin postgres "GRANT robothor_app TO $APP_ROLE" > /dev/null
}

seed() {
  # The seed writes across tenants, as the operator's CLI does: as the owner.
  env PYTHONPATH="$ROOT" HOME="$STACK_DIR/home" GENUS_ENVIRONMENT=development \
    ROBOTHOR_DB_HOST="$PG_HOST" ROBOTHOR_DB_PORT="$PG_PORT" ROBOTHOR_DB_NAME="$DB_NAME" \
    ROBOTHOR_DB_USER="$PG_ADMIN_USER" ROBOTHOR_DB_PASSWORD="$PG_ADMIN_PASSWORD" \
    "$PYTHON" "$ROOT/scripts/dev/rooms_stack/seed.py" "$PASSWORD" "$HOLDING" "$COMPANY" \
    > "$STACK_DIR/people.json"
  echo "seeded: $STACK_DIR/people.json"
}

start() {
  local name="$1" file="$2" dir="$3"; shift 3
  if [ -f "$STACK_DIR/$name.pid" ] && kill -0 "$(cat "$STACK_DIR/$name.pid")" 2>/dev/null; then
    echo "$name already running"; return
  fi
  local port="${1:-}"
  shift
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN > /dev/null 2>&1; then
    echo "rooms stack: port $port is already in use; run down first, or set ROOMS_STACK_*_PORT" >&2
    exit 1
  fi
  (cd "$dir" && with_env "$file" "$@" > "$STACK_DIR/logs/$name.log" 2>&1 & echo $! > "$STACK_DIR/$name.pid")
}

# Up = answering HTTP at all. The bridge's /ready is 503 here by design: it
# also checks a root engine and a memory service this stack does not run.
wait_for() {
  local name="$1" url="$2" code
  for _ in $(seq 1 90); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    if [ -n "$code" ] && [ "$code" != "000" ]; then echo "$name up: $url ($code)"; return; fi
    sleep 1
  done
  echo "$name did not come up; last lines of $STACK_DIR/logs/$name.log:" >&2
  tail -30 "$STACK_DIR/logs/$name.log" >&2
  exit 1
}

up() {
  if [ "${1:-}" = "--build" ] || [ ! -f "$ROOT/app/.next/standalone/server.js" ]; then
    # A post-build advisory check can exit non-zero after a good build; the
    # server bundle existing is what counts.
    (cd "$ROOT/app" && pnpm build > "$STACK_DIR/logs/build.log" 2>&1) || true
    [ -f "$ROOT/app/.next/standalone/server.js" ] || { tail -30 "$STACK_DIR/logs/build.log" >&2; exit 1; }
  fi
  write_env
  database
  seed
  start engine "$STACK_DIR/engine.env" "$ROOT" "$ENGINE_PORT" "$PYTHON" "$ROOT/scripts/dev/rooms_stack/engine.py"
  start bridge "$STACK_DIR/bridge.env" "$ROOT/crm/bridge" "$BRIDGE_PORT" "$PYTHON" bridge_service.py
  start app "$STACK_DIR/app.env" "$ROOT/app" "$APP_PORT" node scripts/start-standalone.mjs
  wait_for engine "http://127.0.0.1:$ENGINE_PORT/ready"
  wait_for bridge "http://127.0.0.1:$BRIDGE_PORT/ready"
  wait_for app "http://127.0.0.1:$APP_PORT/login"
  status
}

down() {
  for name in app bridge engine; do
    if [ -f "$STACK_DIR/$name.pid" ]; then
      kill "$(cat "$STACK_DIR/$name.pid")" 2>/dev/null || true
      rm -f "$STACK_DIR/$name.pid"
      echo "stopped $name"
    fi
  done
}

status() {
  for name in engine bridge app; do
    if [ -f "$STACK_DIR/$name.pid" ] && kill -0 "$(cat "$STACK_DIR/$name.pid")" 2>/dev/null; then
      echo "$name running (pid $(cat "$STACK_DIR/$name.pid")), log $STACK_DIR/logs/$name.log"
    else
      echo "$name not running"
    fi
  done
  echo "dashboard: http://127.0.0.1:$APP_PORT  (sign in as alice@example.com, password ROOMS_STACK_PASSWORD)"
}

print_env() {
  cat <<EOF
ROOMS_STACK_URL=http://127.0.0.1:$APP_PORT
ROOMS_STACK_BRIDGE_URL=http://127.0.0.1:$BRIDGE_PORT
ROOMS_STACK_ENGINE_URL=http://127.0.0.1:$ENGINE_PORT
ROOMS_STACK_PEOPLE=$STACK_DIR/people.json
ROOMS_STACK_PASSWORD=$PASSWORD
ROOMS_STACK_OUTBOX=$STACK_DIR/workspace/outbox.jsonl
EOF
}

case "${1:-}" in
  up) shift; up "$@" ;;
  seed) write_env; database; seed ;;
  status) status ;;
  env) print_env ;;
  down) down ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
