#!/bin/bash
# Phase 1 Tests — Run AFTER Phase 1 deployment. All must pass before Phase 2.
set -uo pipefail
PASS=0; FAIL=0

pass() { echo "PASS"; ((PASS++)) || true; }
fail() { echo "FAIL${1:+ ($1)}"; ((FAIL++)) || true; }

# T1.1: Twenty CRM is running and healthy
echo -n "T1.1 Twenty CRM health... "
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3030/healthz 2>/dev/null)
[ "$HTTP" = "200" ] && pass || fail "$HTTP"

# T1.2: Chatwoot is running and healthy
echo -n "T1.2 Chatwoot health... "
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3100/api 2>/dev/null)
[ "$HTTP" = "200" ] && pass || fail "$HTTP"

# T1.3: Twenty CRM has tables in its database
echo -n "T1.3 Twenty DB populated... "
COUNT=$(psql -d twenty_crm -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('information_schema','pg_catalog');" 2>/dev/null | tr -d ' ')
[ "$COUNT" -gt 10 ] && pass || fail "$COUNT tables"

# T1.4: Chatwoot has tables in its database
echo -n "T1.4 Chatwoot DB populated... "
COUNT=$(psql -d chatwoot -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d ' ')
[ "$COUNT" -gt 10 ] && pass || fail "$COUNT tables"

# T1.5: Docker containers are running (at least 4 CRM containers)
echo -n "T1.5 Docker containers... "
RUNNING=$(sudo docker ps --format '{{.Names}}' | grep -c -E 'twenty|chatwoot')
[ "$RUNNING" -ge 4 ] && pass || fail "$RUNNING containers, expected 4"

# T1.6: Redis has keys from services
echo -n "T1.6 Redis in use... "
KEYS=$(redis-cli dbsize 2>/dev/null | grep -oP '\d+')
[ "$KEYS" -gt 0 ] && pass || fail "empty"

# T1.7-T1.8: Cloudflare DNS resolves
echo -n "T1.7 crm.robothor.ai DNS... "
dig +short crm.robothor.ai 2>/dev/null | grep -q "." && pass || fail "no DNS"

echo -n "T1.8 inbox.robothor.ai DNS... "
dig +short inbox.robothor.ai 2>/dev/null | grep -q "." && pass || fail "no DNS"

# T1.9: Twenty has migrated contacts
echo -n "T1.9 Twenty has contacts... "
SCHEMA=$(psql -d twenty_crm -t -c "SELECT schemaname FROM pg_tables WHERE tablename='person' AND schemaname LIKE 'workspace_%' LIMIT 1;" 2>/dev/null | tr -d ' ')
COUNT=$(psql -d twenty_crm -t -c "SELECT count(*) FROM ${SCHEMA}.person;" 2>/dev/null | tr -d ' ')
[ "$COUNT" -ge 20 ] && pass || fail "$COUNT contacts, expected 20+"

# T1.10: Memory system still healthy
echo -n "T1.10 Memory system unaffected... "
HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9099/health 2>/dev/null)
[ "$HTTP" = "200" ] && pass || fail "$HTTP"

# T1.11: OpenClaw gateway alive
echo -n "T1.11 OpenClaw gateway alive... "
ss -tlnp | grep -q ":18789 " && pass || fail

# T1.12: PG connections healthy
echo -n "T1.12 PG connections healthy... "
CONNS=$(psql -d robothor_memory -t -c "SELECT count(*) FROM pg_stat_activity;" 2>/dev/null | tr -d ' ')
MAX=$(psql -d robothor_memory -t -c "SHOW max_connections;" 2>/dev/null | tr -d ' ')
RATIO=$((CONNS * 100 / MAX))
[ "$RATIO" -lt 80 ] && pass || fail "$CONNS/$MAX = ${RATIO}%"

# T1.13: Chatwoot API inbox exists
echo -n "T1.13 Chatwoot API inbox... "
INBOX=$(curl -s "http://localhost:3100/api/v1/accounts/1/inboxes" -H "api_access_token: X9PstchkkPW4ViY8rTPh8vkt" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(any(i['channel_type']=='Channel::Api' for i in d.get('payload',[])))" 2>/dev/null)
[ "$INBOX" = "True" ] && pass || fail

echo ""
echo "=== Phase 1: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && echo "READY FOR PHASE 2" || echo "FIX FAILURES BEFORE PROCEEDING"
exit $FAIL
