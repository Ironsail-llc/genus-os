#!/usr/bin/env bash
# Sandbox entrypoint — starts Xvfb, Openbox, and Chromium
set -euo pipefail

# Start Xvfb
Xvfb :0 -screen 0 1280x1024x24 -nolisten tcp &
export DISPLAY=:0

# Wait for Xvfb
sleep 0.5

# Start window manager
openbox &

# Start Chromium with remote debugging
chromium-browser \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --remote-debugging-address=0.0.0.0 \
  --remote-debugging-port=9222 \
  --window-size=1280,1024 \
  --no-first-run \
  --disable-background-networking \
  about:blank &

# Keep container running
wait
