#!/bin/bash
# Phase 4: Verify all MCP servers are functional
set -e
PASS=0; FAIL=0

t() {
  echo -n "  $1... "
  if eval "$2" 2>/dev/null; then echo "PASS" && ((PASS++)) || true; else echo "FAIL" && ((FAIL++)) || true; fi
}

# T4.1: robothor-memory MCP server starts and has all tools
echo "=== Memory MCP Server ==="
t "T4.1 Memory MCP tools" "timeout 5 /home/philip/clawd/memory_system/venv/bin/python -c \"
import sys; sys.path.insert(0, '/home/philip/clawd/memory_system')
from mcp_server import get_tool_definitions
tools = get_tool_definitions()
names = [t['name'] for t in tools]
for n in ['search_memory','store_memory','get_stats','get_entity','look','who_is_here','enroll_face','set_vision_mode','memory_block_read','memory_block_write','memory_block_list','log_interaction']:
    assert n in names, f'{n} missing'
print('OK:', len(names), 'tools')
\""

# T4.2: memory_block_list handler works
t "T4.2 memory_block_list handler" "timeout 5 /home/philip/clawd/memory_system/venv/bin/python -c \"
import asyncio, sys, json; sys.path.insert(0, '/home/philip/clawd/memory_system')
from mcp_server import handle_tool_call
result = asyncio.run(handle_tool_call('memory_block_list', {}))
assert 'blocks' in result, 'No blocks key'
assert len(result['blocks']) >= 5, f'Only {len(result[\"blocks\"])} blocks'
print('OK:', len(result['blocks']), 'blocks')
\""

# T4.3: Twenty CRM MCP server starts
echo ""
echo "=== Twenty CRM MCP Server ==="
t "T4.3 Twenty MCP starts" "export \$(grep -E '^TWENTY_' /home/philip/robothor/crm/.env | xargs) && TWENTY_BASE_URL='http://localhost:3030' timeout 5 node /home/philip/robothor/crm/twenty-mcp/index.js </dev/null 2>&1 | grep -q 'running on stdio'"

# T4.4: Twenty REST API responds with API key
t "T4.4 Twenty API accessible" "export \$(grep -E '^TWENTY_API_KEY' /home/philip/robothor/crm/.env | xargs) && curl -sf -H \"Authorization: Bearer \$TWENTY_API_KEY\" 'http://localhost:3030/api/objects/people?limit=1' | grep -q 'data'"

# T4.5: Chatwoot MCP server starts
echo ""
echo "=== Chatwoot MCP Server ==="
t "T4.5 Chatwoot MCP starts" "CHATWOOT_BASE_URL='http://localhost:3100' CHATWOOT_API_TOKEN='X9PstchkkPW4ViY8rTPh8vkt' timeout 5 node /home/philip/robothor/crm/chatwoot-mcp/dist/index.js </dev/null 2>&1 | grep -q 'running on stdio'"

# T4.6: Chatwoot API responds
t "T4.6 Chatwoot API accessible" "curl -sf -H 'api_access_token: X9PstchkkPW4ViY8rTPh8vkt' 'http://localhost:3100/api/v1/accounts/1/conversations?page=1' | grep -q 'data'"

# T4.7: .claude.json has all 3 MCP servers configured
echo ""
echo "=== Claude Config ==="
t "T4.7a robothor-memory in .claude.json" "grep -q 'robothor-memory' /home/philip/.claude.json"
t "T4.7b twenty-crm in .claude.json" "grep -q 'twenty-crm' /home/philip/.claude.json"
t "T4.7c chatwoot in .claude.json" "grep -q '\"chatwoot\"' /home/philip/.claude.json"

# T4.8: Bridge service still healthy
echo ""
echo "=== Integration ==="
t "T4.8 Bridge healthy" "curl -sf http://localhost:9100/health | grep -q status"

# T4.9: Memory system still healthy
t "T4.9 Memory system healthy" "curl -sf http://localhost:9099/health | grep -q ok"

echo ""
echo "=== Phase 4: $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && echo "ALL PHASES COMPLETE" || echo "FIX FAILURES"
exit $FAIL
