#!/bin/bash
# Quick test script for SMS webhook
# Usage: ./test-sms.sh "Your message here"

MESSAGE="${1:-Test message from Robothor}"

curl -X POST http://localhost:8766/sms \
  -d "MessageSid=SM$(date +%s)" \
  -d "From=+13479061511" \
  -d "To=+14134086025" \
  -d "Body=$MESSAGE" \
  -d "NumMedia=0"

echo ""
echo "Message logged. Check:"
echo "  - Log file: /home/philip/clawd/memory/sms-log.json"
echo "  - API: curl http://localhost:8766/messages"
