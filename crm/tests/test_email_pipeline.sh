#!/bin/bash
# Email Pipeline Integration Tests
# Validates the end-to-end email sync, data quality, and CRM wiring.
PASS=0; FAIL=0

check() {
  echo -n "  $1... "
  if eval "$2" 2>/dev/null; then echo "PASS" && ((PASS++)) || true; else echo "FAIL" && ((FAIL++)) || true; fi
}

echo "=== Email Pipeline ==="
check "email-log.json exists" "[ -f ${ROBOTHOR_WORKSPACE:-$HOME/robothor}/brain/memory/email-log.json ]"
check "email-log.json is valid JSON" "python3 -c 'import json; json.load(open(\"${ROBOTHOR_WORKSPACE:-$HOME/robothor}/brain/memory/email-log.json\"))'"
check "email-log has entries" "python3 -c 'import json; d=json.load(open(\"${ROBOTHOR_WORKSPACE:-$HOME/robothor}/brain/memory/email-log.json\")); assert len(d.get(\"entries\",{})) > 0'"

echo ""
echo "=== Data Quality ==="
check "No recent null-content entries (24h)" "python3 -c '
import json
from datetime import datetime, timedelta
d=json.load(open(\"${ROBOTHOR_WORKSPACE:-$HOME/robothor}/brain/memory/email-log.json\"))
cutoff=(datetime.now()-timedelta(days=1)).isoformat()
bad=[e for e in d[\"entries\"].values() if e.get(\"fetchedAt\",\"\")>=cutoff and e.get(\"from\") is None and e.get(\"subject\") is None]
assert len(bad)==0, f\"{len(bad)} null entries found\"
'"

echo ""
echo "=== CRM Integration ==="
check "Bridge /log-interaction responds" "curl -sf -X POST http://localhost:9100/log-interaction -H 'Content-Type: application/json' -d '{\"contact_name\":\"test\",\"channel\":\"api\",\"direction\":\"incoming\",\"content_summary\":\"health check test\"}' | grep -q status"
check "Bridge health" "curl -sf http://localhost:9100/health | grep -q status"

echo ""
echo "=== Gmail Auth ==="
check "gog gmail search works" "GOG_KEYRING_PASSWORD=\"\${GOG_KEYRING_PASSWORD:?GOG_KEYRING_PASSWORD env var required}\" gog gmail search 'is:unread' --account \${ROBOTHOR_AI_EMAIL:?ROBOTHOR_AI_EMAIL env var required} --max 1 --json 2>/dev/null | python3 -c 'import json,sys; json.load(sys.stdin)'"

echo ""
echo "=== Worker Status ==="
check "worker-handoff.json valid" "python3 -c 'import json; json.load(open(\"${ROBOTHOR_WORKSPACE:-$HOME/robothor}/brain/memory/worker-handoff.json\"))'"
check "Triage worker ran recently (<60m)" "python3 -c '
import json
from datetime import datetime, timedelta
d=json.load(open(\"${ROBOTHOR_WORKSPACE:-$HOME/robothor}/brain/memory/worker-handoff.json\"))
lr=d.get(\"lastRunAt\",\"\").replace(\"+00:00\",\"\").replace(\"Z\",\"\")
assert lr, \"No lastRunAt\"
dt=datetime.fromisoformat(lr)
assert (datetime.now()-dt).total_seconds() < 3600, f\"Last ran {lr}\"
'"

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ $FAIL -eq 0 ] && echo "EMAIL PIPELINE HEALTHY" || echo "ISSUES DETECTED"
exit $FAIL
