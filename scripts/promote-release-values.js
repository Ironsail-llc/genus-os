#!/usr/bin/env node

"use strict";

const fs = require("node:fs");

const version = (process.argv[2] || "").replace(/^v/, "");
if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:[+-][0-9A-Za-z.-]+)?$/.test(version)) {
  throw new Error(`Invalid release version: ${process.argv[2] || "<missing>"}`);
}

const project = fs.readFileSync("pyproject.toml", "utf8");
const authoritative = project.match(/\[project\][\s\S]*?\nversion\s*=\s*"([^"]+)"/)?.[1];
if (authoritative !== version) {
  throw new Error(
    `Refusing to promote v${version}: release metadata is ${authoritative || "missing"}`,
  );
}

function promote(path) {
  const source = fs.readFileSync(path, "utf8");
  const matches = source.match(/^\s*imageTag:\s*[^\n]+$/gm) || [];
  if (matches.length !== 1) {
    throw new Error(`${path}: expected one global.imageTag, found ${matches.length}`);
  }
  fs.writeFileSync(
    path,
    source.replace(/^(\s*imageTag:\s*)[^\n]+$/m, `$1"v${version}"`),
  );
}

promote("helm/genus-os/values-production.yaml");

const stagingPath = "helm/genus-os/values-staging.yaml";
const staging = fs.readFileSync(stagingPath, "utf8");
const holder = staging.match(/^\s*deployedFromPR:\s*"?([^"\n]*)"?\s*$/m);
if (!holder) {
  throw new Error(`${stagingPath}: global.deployedFromPR is missing`);
}
if (holder[1].trim() === "") {
  promote(stagingPath);
}

console.log(`Promoted GitOps image tags to v${version}`);
