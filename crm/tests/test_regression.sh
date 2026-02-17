#!/bin/bash
# Regression suite: Run after ANY change to verify nothing is broken.
PASS=0; FAIL=0

check() {
  echo -n "  $1... "
  if eval "$2" 2>/dev/null; then echo "PASS" && ((PASS++)) || true; else echo "FAIL" && ((FAIL++)) || true; fi
}

echo "=== Existing Services ==="
check "PostgreSQL" "psql -d robothor_memory -c 'SELECT 1' -t | grep -q 1"
check "Memory orchestrator (:9099)" "curl -sf http://localhost:9099/health | grep -q ok"
check "OpenClaw gateway (:18789)" "ss -tlnp | grep -q ':18789 '"
check "Ollama (:11434)" "curl -sf http://localhost:11434/api/tags | grep -q models"
check "Status server (:3000)" "curl -sf -o /dev/null http://localhost:3000"
check "Status dashboard (:3001)" "curl -sf -o /dev/null http://localhost:3001"
check "Cloudflare tunnel" "systemctl is-active cloudflared -q"

echo ""
echo "=== New CRM Services ==="
check "Redis (:6379)" "redis-cli ping | grep -q PONG"
check "Twenty CRM (:3030)" "curl -sf -o /dev/null http://localhost:3030"
check "Chatwoot (:3100)" "curl -sf -o /dev/null http://localhost:3100"
check "Bridge (:9100)" "curl -sf http://localhost:9100/health | grep -q status"
check "Docker containers (4+)" "[ \$(sudo docker ps -q 2>/dev/null | wc -l) -ge 4 ]"

echo ""
echo "=== Database ==="
check "robothor_memory DB" "psql -d robothor_memory -c 'SELECT count(*) FROM memory_facts' -t | grep -q '[0-9]'"
check "twenty_crm DB" "psql -d twenty_crm -c 'SELECT 1' -t | grep -q 1"
check "chatwoot DB" "psql -d chatwoot -c 'SELECT 1' -t | grep -q 1"
check "contact_identifiers table" "psql -d robothor_memory -c 'SELECT count(*) FROM contact_identifiers' -t | grep -q '[0-9]'"
check "agent_memory_blocks table" "psql -d robothor_memory -c 'SELECT count(*) FROM agent_memory_blocks' -t | grep -q '[0-9]'"

echo ""
echo "=== MCP Servers ==="
check "robothor-memory MCP config" "grep -q 'robothor-memory' /home/philip/.claude.json"
check "twenty-crm MCP config" "grep -q 'twenty-crm' /home/philip/.claude.json"
check "chatwoot MCP config" "grep -q '\"chatwoot\"' /home/philip/.claude.json"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && echo "ALL SYSTEMS OPERATIONAL" || echo "ISSUES DETECTED"
exit $FAIL
