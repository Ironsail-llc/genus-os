#!/usr/bin/env bash
# Build the agent sandbox image.
#
# infra/sandbox/Dockerfile has existed for months and nothing ever built it —
# so ROBOTHOR_SANDBOX_DEFAULT_MODE=enforce would have fail-closed every
# sandboxed agent on an image that does not exist. This is the missing step.
#
# Uses the same runtime the engine will: rootless podman by default (no daemon,
# no root-equivalent socket), overridable to docker.
set -euo pipefail

BINARY="${ROBOTHOR_SANDBOX_BINARY:-docker}"
IMAGE="${ROBOTHOR_SANDBOX_IMAGE:-robothor-sandbox:latest}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  echo "error: container runtime '$BINARY' not found." >&2
  echo "       install it, or set ROBOTHOR_SANDBOX_BINARY (docker|podman)." >&2
  exit 1
fi

echo "Building $IMAGE with $BINARY..."
"$BINARY" build -t "$IMAGE" "$ROOT/infra/sandbox"

# Prove the image can actually serve `exec` — the tools the agents need must be
# present, or the sandbox fails closed on its first command. Don't trust a
# successful build; probe it.
echo "Verifying the image can run what agents run..."
"$BINARY" run --rm --entrypoint sh "$IMAGE" -c '
  set -e
  for tool in git python3 sh curl jq; do
    command -v "$tool" >/dev/null || { echo "MISSING: $tool"; exit 1; }
  done
  echo "ok: $(python3 --version), $(git --version)"
'
echo "Built and verified $IMAGE"
