#!/usr/bin/env node
/**
 * First-load JavaScript budget per page route.
 *
 * Next 16 no longer prints route sizes after `next build`, so this reads the
 * build output itself. A route's first-load JS is the root main files from
 * `.next/build-manifest.json` plus every entry chunk the route's client
 * reference manifest lists (its layouts and its page), each chunk counted once
 * and measured gzipped. Route handlers ship no client JS and are not measured.
 *
 *   node scripts/bundle-budget.mjs            # check against bundle-budget.json
 *   node scripts/bundle-budget.mjs --write    # reset budgets to current + 5%
 *   pnpm bundle:check [--write]               # the same, from app/
 *
 * Exits 1 when a route is over its budget or has none: a new page needs a
 * budget, and a budget is raised on purpose, in review, never by drift.
 */

import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import vm from "node:vm";

const MANIFEST_SUFFIX = "page_client-reference-manifest.js";
const HEADROOM_PERCENT = 5;

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* walk(full);
    else yield full;
  }
}

/** The manifest file sets globalThis.__RSC_MANIFEST[key]; run it in a sandbox to read it. */
function readClientManifest(file) {
  const sandbox = { globalThis: {} };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(readFileSync(file, "utf8"), sandbox, { filename: file });
  const manifests = Object.values(sandbox.__RSC_MANIFEST ?? {});
  return manifests[0] ?? {};
}

/** "/" for server/app/page_…, "/signin" for server/app/signin/page_…. */
function routeOf(appDir, manifestFile) {
  const rel = path.relative(appDir, path.dirname(manifestFile)).split(path.sep).join("/");
  return rel === "" ? "/" : `/${rel}`;
}

/**
 * Gzipped first-load JS bytes per page route.
 * @param {string} nextDir the `.next` directory of a finished build
 * @returns {Record<string, number>}
 */
export function measureRoutes(nextDir) {
  const buildManifest = path.join(nextDir, "build-manifest.json");
  const appDir = path.join(nextDir, "server", "app");
  if (!existsSync(buildManifest) || !existsSync(appDir)) {
    throw new Error(`No Next build at ${nextDir}: run pnpm build first.`);
  }
  const { rootMainFiles = [] } = JSON.parse(readFileSync(buildManifest, "utf8"));
  const gzipped = new Map();
  const sizeOf = (asset) => {
    if (!gzipped.has(asset)) {
      gzipped.set(asset, gzipSync(readFileSync(path.join(nextDir, asset))).length);
    }
    return gzipped.get(asset);
  };

  const routes = {};
  for (const file of walk(appDir)) {
    if (path.basename(file) !== MANIFEST_SUFFIX) continue;
    const { entryJSFiles = {} } = readClientManifest(file);
    const assets = new Set(rootMainFiles);
    for (const chunks of Object.values(entryJSFiles)) {
      for (const chunk of chunks) assets.add(chunk);
    }
    let total = 0;
    for (const asset of assets) total += sizeOf(asset);
    routes[routeOf(appDir, file)] = total;
  }
  return Object.fromEntries(Object.entries(routes).sort(([a], [b]) => a.localeCompare(b)));
}

/**
 * @param {Record<string, number>} sizes measured bytes per route
 * @param {Record<string, number>} budgets allowed bytes per route
 * @returns {{ ok: boolean, failures: string[] }}
 */
export function checkBudgets(sizes, budgets) {
  const failures = [];
  for (const [route, size] of Object.entries(sizes)) {
    const budget = budgets[route];
    if (budget === undefined) {
      failures.push(`${route} has no budget (${size} B); add it to bundle-budget.json`);
    } else if (size > budget) {
      failures.push(`${route} first-load JS ${size} B is over budget ${budget} B (+${size - budget} B)`);
    }
  }
  return { ok: failures.length === 0, failures };
}

/** Current size plus 5%, rounded up to the byte. */
export function budgetsFrom(sizes) {
  return Object.fromEntries(
    Object.entries(sizes).map(([route, size]) => [route, Math.ceil((size * (100 + HEADROOM_PERCENT)) / 100)])
  );
}

function kb(bytes) {
  return `${(bytes / 1024).toFixed(1)} kB`;
}

function main(argv) {
  const arg = (name, fallback) => {
    const i = argv.indexOf(name);
    return i === -1 ? fallback : argv[i + 1];
  };
  const nextDir = path.resolve(arg("--next-dir", ".next"));
  const budgetFile = path.resolve(arg("--budget", "bundle-budget.json"));
  const sizes = measureRoutes(nextDir);

  if (argv.includes("--write")) {
    const budgets = budgetsFrom(sizes);
    const doc = {
      description:
        "Gzipped first-load JS budget per page route, in bytes. Checked in CI by scripts/bundle-budget.mjs. " +
        "Raise a budget on purpose, in the pull request that needs it.",
      routes: budgets,
    };
    writeFileSync(budgetFile, `${JSON.stringify(doc, null, 2)}\n`);
    console.log(`Wrote ${Object.keys(budgets).length} budgets (current + ${HEADROOM_PERCENT}%) to ${budgetFile}`);
    return 0;
  }

  const { routes: budgets = {} } = JSON.parse(readFileSync(budgetFile, "utf8"));
  const width = Math.max(...Object.keys(sizes).map((r) => r.length), 5);
  console.log(`${"route".padEnd(width)}  first-load JS (gzip)  budget`);
  for (const [route, size] of Object.entries(sizes)) {
    const budget = budgets[route];
    console.log(`${route.padEnd(width)}  ${kb(size).padStart(20)}  ${budget === undefined ? "none" : kb(budget)}`);
  }
  const { ok, failures } = checkBudgets(sizes, budgets);
  for (const failure of failures) console.error(failure);
  if (!ok) {
    console.error(
      "\nIf this growth is intended, regenerate the budgets (current size + 5%) and commit them:\n" +
        "  pnpm build && pnpm bundle:check --write   # rewrites app/bundle-budget.json"
    );
  }
  return ok ? 0 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
