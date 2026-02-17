#!/bin/bash
# Phase 0 Prerequisites — Run BEFORE Phase 1 deployment. All must pass.
set -uo pipefail
PASS=0; FAIL=0

pass() { echo "PASS"; ((PASS++)) || true; }
fail() { echo "FAIL${1:+ ($1)}"; ((FAIL++)) || true; }

# T0.1: Redis is running and responsive
echo -n "T0.1 Redis ping... "
redis-cli ping 2>/dev/null | grep -q PONG && pass || fail

# T0.2: Redis maxmemory is configured (~2GB)
echo -n "T0.2 Redis maxmemory... "
MM=$(redis-cli config get maxmemory 2>/dev/null | tail -1)
[ "$MM" = "2147483648" ] && pass || fail "got $MM, want 2147483648"

# T0.3: twenty_crm database exists
echo -n "T0.3 twenty_crm DB exists... "
psql -d twenty_crm -c "SELECT 1" -t 2>/dev/null | grep -q 1 && pass || fail

# T0.4: chatwoot database exists
echo -n "T0.4 chatwoot DB exists... "
psql -d chatwoot -c "SELECT 1" -t 2>/dev/null | grep -q 1 && pass || fail

# T0.5: PG listens on Docker bridge IP
echo -n "T0.5 PG listens on 172.17.0.1... "
ss -tlnp | grep -q "172.17.0.1:5432" && pass || fail "PG not listening on Docker bridge"

# T0.6: PG TCP auth works from Docker container
echo -n "T0.6 PG TCP auth from Docker... "
RESULT=$(sudo docker run --rm --add-host=host.docker.internal:host-gateway \
  postgres:16-alpine sh -c \
  "PGPASSWORD='hntG9K2Sct93Z1ei_vARwKPszdSlNJxKq2sjYrbGN8E' psql -h host.docker.internal -U philip -d twenty_crm -c 'SELECT 1' -t" 2>/dev/null | tr -d ' ')
[ "$RESULT" = "1" ] && pass || fail "TCP auth failed"

# T0.7: max_connections >= 200
echo -n "T0.7 PG max_connections >= 200... "
MC=$(psql -d robothor_memory -t -c "SHOW max_connections;" 2>/dev/null | tr -d ' ')
[ "$MC" -ge 200 ] && pass || fail "$MC < 200"

# T0.8: Required extensions in twenty_crm
echo -n "T0.8 twenty_crm extensions... "
EXTS=$(psql -d twenty_crm -t -c "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('uuid-ossp','pgcrypto');" 2>/dev/null | tr -d ' ')
echo "$EXTS" | grep -q "pgcrypto" && echo "$EXTS" | grep -q "uuid-ossp" && pass || fail "got: $EXTS"

# T0.9: Required extensions in chatwoot
echo -n "T0.9 chatwoot extensions... "
EXTS=$(psql -d chatwoot -t -c "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension WHERE extname IN ('uuid-ossp','pgcrypto','pg_trgm');" 2>/dev/null | tr -d ' ')
echo "$EXTS" | grep -q "pg_trgm" && pass || fail "got: $EXTS"

# T0.10: Target ports free
echo -n "T0.10 Ports 3030,3100,9100 free... "
FREE=true
for port in 3030 3100 9100; do
  if ss -tlnp | grep -q ":${port} "; then
    FREE=false
    echo -n "port $port occupied! "
  fi
done
$FREE && pass || fail

echo ""
echo "=== Phase 0: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && echo "READY FOR PHASE 1" || echo "FIX FAILURES BEFORE PROCEEDING"
exit $FAIL
