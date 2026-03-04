#!/bin/bash
# Cloudflare Tunnel Management Script
# Usage: cf-tunnel.sh [get|set|add]

ACCOUNT_ID="431855ad5f7426525cfd675de298f426"
TUNNEL_ID="2c15ab71-d540-4308-840d-0b3a564c3e7a"
API_TOKEN="zrQwDH8UWL9l4TTAKYms1sOku10kbmNcIexIFhj0"

API_BASE="https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}"

case "$1" in
  get)
    curl -s "${API_BASE}/configurations" \
      -H "Authorization: Bearer ${API_TOKEN}" | jq '.result.config.ingress'
    ;;
  set)
    # Usage: cf-tunnel.sh set '{"config":{"ingress":[...]}}'
    curl -s -X PUT "${API_BASE}/configurations" \
      -H "Authorization: Bearer ${API_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$2" | jq .
    ;;
  add)
    # Usage: cf-tunnel.sh add hostname service
    # Example: cf-tunnel.sh add api.robothor.ai http://localhost:8080
    HOSTNAME="$2"
    SERVICE="$3"
    if [[ -z "$HOSTNAME" || -z "$SERVICE" ]]; then
      echo "Usage: cf-tunnel.sh add <hostname> <service>"
      exit 1
    fi
    
    # Get current config and add new route
    CURRENT=$(curl -s "${API_BASE}/configurations" \
      -H "Authorization: Bearer ${API_TOKEN}" | jq '.result.config')
    
    # Insert new route before the catch-all 404
    NEW_CONFIG=$(echo "$CURRENT" | jq --arg h "$HOSTNAME" --arg s "$SERVICE" \
      '.ingress = [.ingress[:-1][], {"hostname": $h, "service": $s, "originRequest": {}}, .ingress[-1]]')
    
    curl -s -X PUT "${API_BASE}/configurations" \
      -H "Authorization: Bearer ${API_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"config\": $NEW_CONFIG}" | jq .
    ;;
  *)
    echo "Cloudflare Tunnel Management"
    echo ""
    echo "Usage:"
    echo "  cf-tunnel.sh get                     - Show current routes"
    echo "  cf-tunnel.sh add <hostname> <service> - Add a new route"
    echo "  cf-tunnel.sh set '<json>'            - Set full config"
    echo ""
    echo "Current tunnel: robothor-gateway (${TUNNEL_ID})"
    ;;
esac
