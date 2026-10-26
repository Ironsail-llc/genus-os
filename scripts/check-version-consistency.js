#!/usr/bin/env node

"use strict";

const fs = require("node:fs");
const { execFileSync } = require("node:child_process");

function read(path) {
  return fs.readFileSync(path, "utf8");
}

/** The same file as of another git ref, for comparing a PR against its base. */
function readAtRef(ref, path) {
  // Deliberately unguarded: if the baseline cannot be read the comparison is
  // vacuous, and a check that silently verifies nothing is worse than a loud one.
  return execFileSync("git", ["show", `${ref}:${path}`], { encoding: "utf8" });
}

function captureIn(contents, pattern, path, label) {
  const match = contents.match(pattern);
  if (!match) {
    throw new Error(`${path}: could not find ${label}`);
  }
  return match[1];
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
  "pyproject.toml",
  /\[project\][\s\S]*?\nversion\s*=\s*"([^"]+)"/,
  "[project].version",
);
const allowDeploymentLag = process.env.GENUS_ALLOW_DEPLOYMENT_LAG === "true";
// A git ref to compare the deployment tags against instead of the authoritative
// version. Set on pull-request runs: see the rationale above the deployment-tag
// block below.
const baselineRef = (process.env.GENUS_VERSION_BASELINE_REF || "").trim();
const IMAGE_TAG_PATTERN = /^\s*imageTag:\s*"?v([^"\s]+)"?\s*$/m;

if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:[+-][0-9A-Za-z.-]+)?$/.test(authoritative)) {
  throw new Error(`pyproject.toml: invalid semantic version ${authoritative}`);
}

const rootPackage = json("package.json");
const rootLock = json("package-lock.json");
const appPackage = json("app/package.json");
const stagingValues = read("helm/genus-os/values-staging.yaml");
const stagingHolder = stagingValues.match(
  /^\s*deployedFromPR:\s*"?([^"\n]*)"?\s*$/m,
);
if (!stagingHolder) {
  throw new Error(
    "helm/genus-os/values-staging.yaml: global.deployedFromPR is missing",
  );
}

const versions = new Map([
  [
    "Python runtime",
    capture(
      "robothor/__init__.py",
      /__version__\s*=\s*"([^"]+)"/,
      "__version__",
    ),
  ],
  [
    "uv.lock genusos package",
    capture(
      "uv.lock",
      /\[\[package\]\]\nname = "genusos"\nversion = "([^"]+)"/,
      "genusos package version",
    ),
  ],
  [
    "Helm chart",
    capture(
      "helm/genus-os/Chart.yaml",
      /^version:\s*"?([^"\s]+)"?\s*$/m,
      "version",
    ),
  ],
  [
    "Helm appVersion",
    capture(
      "helm/genus-os/Chart.yaml",
      /^appVersion:\s*"?([^"\s]+)"?\s*$/m,
      "appVersion",
    ),
  ],
  [
    "install.sh default version",
    capture(
      "scripts/install.sh",
      /^INSTALL_SH_DEFAULT_VERSION="v([^"]+)"$/m,
      "INSTALL_SH_DEFAULT_VERSION",
    ),
  ],
  ["dashboard package", appPackage.version],
  ["release package", rootPackage.version],
  ["release package lock", rootLock.version],
  ["release lock root package", rootLock.packages?.[""]?.version],
]);

// Which deployment tags are in scope. Staging only counts while it is idle —
// a PR holding staging legitimately pins its own build there.
const deploymentTags = [
  ["production image tag", "helm/genus-os/values-production.yaml"],
];
if (stagingHolder[1].trim() === "") {
  deploymentTags.push([
    "idle staging image tag",
    "helm/genus-os/values-staging.yaml",
  ]);
}

// Two different questions, and conflating them deadlocked this repository twice
// (v1.57.0 on 2026-08-27, a lost promotion push — see tests/test_promote_release.py;
// and v1.108.2 on 2026-10-09, an image build that failed after the tag was cut).
//
//   On a release run: does the deployed tag match the version we claim to be?
//   That is the no-lag invariant, and the promotion job that can clear it runs
//   in the same workflow, so failing here is actionable.
//
//   On a pull request: nothing in a PR's diff can make production's tag match a
//   version only a release can publish. Asserting it there fails authors for
//   someone else's incident and blocks the very PR that repairs the chain. So a
//   PR is asked the question it can actually answer: did you change the tag?
//   Hand-edits are still caught; the lag is not a PR's business.
if (baselineRef) {
  const edited = deploymentTags
    .map(([name, path]) => {
      const mine = captureIn(
        read(path),
        IMAGE_TAG_PATTERN,
        path,
        "global.imageTag",
      );
      const theirs = captureIn(
        readAtRef(baselineRef, path),
        IMAGE_TAG_PATTERN,
        `${baselineRef}:${path}`,
        "global.imageTag",
      );
      return [name, mine, theirs];
    })
    .filter(([, mine, theirs]) => mine !== theirs);
  if (edited.length > 0) {
    const detail = edited
      .map(([name, mine, theirs]) => `  - ${name}: ${theirs} -> ${mine}`)
      .join("\n");
    throw new Error(
      `a pull request must not change the deployment image tag:\n${detail}\n\n` +
        "These are written by the release pipeline (semantic-release promotes\n" +
        "production; the deploy-staging workflow holds staging). Revert them and\n" +
        "let the pipeline own them.",
    );
  }
} else if (!allowDeploymentLag) {
  for (const [name, path] of deploymentTags) {
    versions.set(
      name,
      captureIn(read(path), IMAGE_TAG_PATTERN, path, "global.imageTag"),
    );
  }
}

const mismatches = [...versions].filter(
  ([, version]) => version !== authoritative,
);
if (mismatches.length > 0) {
  const detail = mismatches
    .map(([name, version]) => `  - ${name}: ${String(version)}`)
    .join("\n");
  // A lagging deployment tag means a promotion was lost, and this gate runs
  // first in the release workflow — so it also skips the promotion job that
  // is the only thing able to clear the lag. Say how to break that cycle:
  // the failure alone reads as "someone forgot to bump a version".
  const lagging = mismatches.some(([name]) => name.endsWith("image tag"));
  // Two causes, opposite recoveries, and promoting on the wrong one writes a tag
  // nothing can pull. The old text assumed a lost promotion unconditionally; on
  // 2026-10-09 the cause was a failed image build, so following it would have
  // pointed production at a v1.108.2 image that was never published.
  const recovery = lagging
    ? `\n\nCheck whether the image for ${authoritative} was actually published:` +
      // Owner read from the environment rather than written in: this is a
      // public platform repo and must not name one deployment's org.
      // GITHUB_REPOSITORY is "owner/name" under Actions.
      `\n  gh api orgs/${(process.env.GITHUB_REPOSITORY || "<owner>/<repo>").split("/")[0]}` +
      "/packages/container/<image>/versions" +
      " --jq '[.[].metadata.container.tags[]?]|unique'" +
      "\n\nIf it IS there, the promotion was lost in transit. Recover with:" +
      `\n  node scripts/promote-release-values.js ${authoritative}` +
      "\nthen commit the helm/ values to main as a chore(deploy) commit." +
      "\n\nIf it is NOT there, that release's image build failed after its tag was" +
      "\ncut, and production lawfully lags. Do NOT promote — it would pin a tag" +
      "\nnothing can pull. Let the next release build and promote instead, with" +
      "\nrepository variable GENUS_ALLOW_DEPLOYMENT_LAG=true for that one run," +
      "\nthen unset it."
    : "";
  throw new Error(
    `product version drift (expected ${authoritative}):\n${detail}${recovery}`,
  );
}

console.log(
  allowDeploymentLag
    ? `Release metadata is consistent: ${authoritative} (deployment tag promotion pending)`
    : `Product version metadata is consistent: ${authoritative}`,
);
