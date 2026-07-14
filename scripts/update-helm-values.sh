#!/bin/bash
# Synchronizes Genus OS release metadata with the version computed by
# semantic-release. Production image tags are deliberately not changed here:
# the release workflow promotes them only after both images are built and pass
# the blocking vulnerability scan.
#
# Usage: ./scripts/update-helm-values.sh <version> <branch>

set -euo pipefail

VERSION="${1:?usage: $0 <version> <branch>}"
BRANCH="${2:?usage: $0 <version> <branch>}"

# Strip a leading "v" if semantic-release passes it that way.
PRODUCT_VERSION="${VERSION#v}"
HELM_VERSION="v${PRODUCT_VERSION}"

case "$BRANCH" in
  main)    VALUES_FILE="" ;;
  staging) VALUES_FILE="helm/genus-os/values-staging.yaml"   ;;
  *)
    echo "Unknown branch: $BRANCH — skipping helm values bump."
    exit 0
    ;;
esac

if [ -n "$VALUES_FILE" ] && [ ! -f "$VALUES_FILE" ]; then
  echo "Values file not found: $VALUES_FILE"
  exit 1
fi

if [ "$BRANCH" = "main" ]; then
  if ! [[ "$PRODUCT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]]; then
    echo "Invalid semantic version: $VERSION" >&2
    exit 1
  fi

  echo "→ Synchronizing product metadata to $PRODUCT_VERSION"
  PRODUCT_VERSION="$PRODUCT_VERSION" node <<'NODE'
const fs = require('node:fs');

const version = process.env.PRODUCT_VERSION;
const updates = new Map();

function read(path) {
  return updates.has(path) ? updates.get(path) : fs.readFileSync(path, 'utf8');
}

function write(path, value) {
  updates.set(path, value);
}

function replaceOnce(path, pattern, replacement, label) {
  const original = read(path);
  const flags = pattern.flags.includes('g') ? pattern.flags : `${pattern.flags}g`;
  const matches = original.match(new RegExp(pattern.source, flags));
  if (!matches || matches.length !== 1) {
    throw new Error(`${path}: expected exactly one ${label}, found ${matches ? matches.length : 0}`);
  }
  write(path, original.replace(pattern, replacement));
}

replaceOnce(
  'pyproject.toml',
  /(\[project\][\s\S]*?\nversion\s*=\s*")[^"]+("\s*\n)/,
  `$1${version}$2`,
  '[project].version',
);
replaceOnce(
  'uv.lock',
  /(\[\[package\]\]\nname = "genusos"\nversion = ")[^"]+("\n)/,
  `$1${version}$2`,
  'genusos package version',
);
replaceOnce(
  'robothor/__init__.py',
  /(__version__\s*=\s*")[^"]+("\s*\n)/,
  `$1${version}$2`,
  'runtime version',
);
replaceOnce('helm/genus-os/Chart.yaml', /^version:\s*[^\n]+$/m, `version: ${version}`, 'chart version');
replaceOnce(
  'helm/genus-os/Chart.yaml',
  /^appVersion:\s*[^\n]+$/m,
  `appVersion: "${version}"`,
  'chart appVersion',
);
for (const path of ['package.json', 'app/package.json']) {
  const data = JSON.parse(read(path));
  data.version = version;
  write(path, `${JSON.stringify(data, null, 2)}\n`);
}

const lockPath = 'package-lock.json';
const lock = JSON.parse(read(lockPath));
lock.version = version;
if (!lock.packages || !lock.packages['']) {
  throw new Error(`${lockPath}: root package entry is missing`);
}
lock.packages[''].version = version;
write(lockPath, `${JSON.stringify(lock, null, 2)}\n`);

for (const [path, value] of updates) {
  fs.writeFileSync(path, value);
}
NODE
  echo "✓ Product release metadata updated; deployment promotion remains pending"
  GENUS_ALLOW_DEPLOYMENT_LAG=true node scripts/check-version-consistency.js
  exit 0
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
