#!/usr/bin/env node

'use strict';

const fs = require('node:fs');

function read(path) {
  return fs.readFileSync(path, 'utf8');
}

function capture(path, pattern, label) {
  const match = read(path).match(pattern);
  if (!match) {
    throw new Error(`${path}: could not find ${label}`);
  }
  return match[1];
}

function json(path) {
  return JSON.parse(read(path));
}

const authoritative = capture(
  'pyproject.toml',
  /\[project\][\s\S]*?\nversion\s*=\s*"([^"]+)"/,
  '[project].version',
);
const allowDeploymentLag = process.env.GENUS_ALLOW_DEPLOYMENT_LAG === 'true';

if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:[+-][0-9A-Za-z.-]+)?$/.test(authoritative)) {
  throw new Error(`pyproject.toml: invalid semantic version ${authoritative}`);
}

const rootPackage = json('package.json');
const rootLock = json('package-lock.json');
const appPackage = json('app/package.json');
const stagingValues = read('helm/genus-os/values-staging.yaml');
const stagingHolder = stagingValues.match(/^\s*deployedFromPR:\s*"?([^"\n]*)"?\s*$/m);
if (!stagingHolder) {
  throw new Error('helm/genus-os/values-staging.yaml: global.deployedFromPR is missing');
}

const versions = new Map([
  [
    'Python runtime',
    capture('robothor/__init__.py', /__version__\s*=\s*"([^"]+)"/, '__version__'),
  ],
  [
    'uv.lock genusos package',
    capture(
      'uv.lock',
      /\[\[package\]\]\nname = "genusos"\nversion = "([^"]+)"/,
      'genusos package version',
    ),
  ],
  ['Helm chart', capture('helm/genus-os/Chart.yaml', /^version:\s*"?([^"\s]+)"?\s*$/m, 'version')],
  [
    'Helm appVersion',
    capture('helm/genus-os/Chart.yaml', /^appVersion:\s*"?([^"\s]+)"?\s*$/m, 'appVersion'),
  ],
  ['dashboard package', appPackage.version],
  ['release package', rootPackage.version],
  ['release package lock', rootLock.version],
  ['release lock root package', rootLock.packages?.['']?.version],
]);

if (!allowDeploymentLag) {
  versions.set(
    'production image tag',
    capture(
      'helm/genus-os/values-production.yaml',
      /^\s*imageTag:\s*"?v([^"\s]+)"?\s*$/m,
      'global.imageTag',
    ),
  );
  if (stagingHolder[1].trim() === '') {
    versions.set(
      'idle staging image tag',
      capture(
        'helm/genus-os/values-staging.yaml',
        /^\s*imageTag:\s*"?v([^"\s]+)"?\s*$/m,
        'global.imageTag',
      ),
    );
  }
}

const mismatches = [...versions].filter(([, version]) => version !== authoritative);
if (mismatches.length > 0) {
  const detail = mismatches.map(([name, version]) => `  - ${name}: ${String(version)}`).join('\n');
  // A lagging deployment tag means a promotion was lost, and this gate runs
  // first in the release workflow — so it also skips the promotion job that
  // is the only thing able to clear the lag. Say how to break that cycle:
  // the failure alone reads as "someone forgot to bump a version".
  const lagging = mismatches.some(([name]) => name.endsWith('image tag'));
  const recovery = lagging
    ? '\n\nA lost promotion blocks every PR until it is cleared, including the fix.' +
      `\nRecover with: node scripts/promote-release-values.js ${authoritative}` +
      '\nthen commit the helm/ values to main as a chore(deploy) commit.'
    : '';
  throw new Error(`product version drift (expected ${authoritative}):\n${detail}${recovery}`);
}

console.log(
  allowDeploymentLag
    ? `Release metadata is consistent: ${authoritative} (deployment tag promotion pending)`
    : `Product version metadata is consistent: ${authoritative}`,
);
