#!/bin/bash
# Updates helm/genus-os/values-{production,staging}.yaml with the version
# computed by semantic-release. Invoked from .releaserc.js via
# @semantic-release/exec.
#
# Usage: ./scripts/update-helm-values.sh <version> <branch>

set -euo pipefail

VERSION="${1:?usage: $0 <version> <branch>}"
BRANCH="${2:?usage: $0 <version> <branch>}"

# Strip leading "v" if semantic-release passes it that way.
HELM_VERSION="v${VERSION#v}"

case "$BRANCH" in
  main)    VALUES_FILE="helm/genus-os/values-production.yaml" ;;
  staging) VALUES_FILE="helm/genus-os/values-staging.yaml"   ;;
  *)
    echo "Unknown branch: $BRANCH — skipping helm values bump."
    exit 0
    ;;
esac

if [ ! -f "$VALUES_FILE" ]; then
  echo "Values file not found: $VALUES_FILE"
  exit 1
fi

echo "→ Updating $VALUES_FILE with imageTag=$HELM_VERSION"

if command -v yq >/dev/null 2>&1; then
  yq eval ".global.imageTag = \"$HELM_VERSION\"" -i "$VALUES_FILE"
else
  # POSIX sed fallback — matches `  imageTag: "..."` under any nesting.
  sed -i.bak "s/^\([[:space:]]*imageTag:[[:space:]]*\).*/\1\"$HELM_VERSION\"/" "$VALUES_FILE"
  rm -f "${VALUES_FILE}.bak"
fi

echo "✓ $VALUES_FILE updated"
