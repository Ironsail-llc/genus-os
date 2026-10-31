import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { gzipSync } from "node:zlib";
import { spawnSync } from "node:child_process";

import { measureRoutes, checkBudgets, budgetsFrom } from "../../scripts/bundle-budget.mjs";

const SCRIPT = path.resolve(__dirname, "../../scripts/bundle-budget.mjs");

/** A pseudo-random, poorly compressible payload of `n` bytes. */
function payload(n: number, seed: number): string {
  let out = "";
  let x = seed;
  while (out.length < n) {
    x = (x * 1103515245 + 12345) & 0x7fffffff;
    out += String.fromCharCode(33 + (x % 90));
  }
  return out;
}

let root: string;
let nextDir: string;

function chunk(name: string, size: number, seed: number): number {
  const file = path.join(nextDir, "static", "chunks", name);
  const body = payload(size, seed);
  writeFileSync(file, body);
  return gzipSync(body).length;
}

function page(route: string, entryJSFiles: Record<string, string[]>) {
  const dir = path.join(nextDir, "server", "app", route);
  mkdirSync(dir, { recursive: true });
  const key = route === "" ? "/page" : `/${route}/page`;
  writeFileSync(
    path.join(dir, "page_client-reference-manifest.js"),
    `globalThis.__RSC_MANIFEST = globalThis.__RSC_MANIFEST || {};\n` +
      `globalThis.__RSC_MANIFEST[${JSON.stringify(key)}] = ${JSON.stringify({ clientModules: {}, entryJSFiles })};\n`,
  );
}

let sizes: Record<string, number>;

beforeEach(() => {
  root = mkdtempSync(path.join(tmpdir(), "bundle-budget-"));
  nextDir = path.join(root, ".next");
  mkdirSync(path.join(nextDir, "static", "chunks"), { recursive: true });
  sizes = {
    main: chunk("main.js", 4000, 1),
    layout: chunk("layout.js", 3000, 2),
    home: chunk("home.js", 6000, 3),
    signin: chunk("signin.js", 1000, 4),
  };
  writeFileSync(
    path.join(nextDir, "build-manifest.json"),
    JSON.stringify({ rootMainFiles: ["static/chunks/main.js"], polyfillFiles: [] }),
  );
  page("", {
    "[project]/src/app/layout": ["static/chunks/layout.js"],
    "[project]/src/app/page": ["static/chunks/layout.js", "static/chunks/home.js"],
  });
  page("signin", {
    "[project]/src/app/layout": ["static/chunks/layout.js"],
    "[project]/src/app/signin/page": ["static/chunks/signin.js"],
  });
  // Route handlers ship no client JS and must not need a budget.
  mkdirSync(path.join(nextDir, "server", "app", "api", "live"), { recursive: true });
  writeFileSync(path.join(nextDir, "server", "app", "api", "live", "route.js"), "");
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

describe("measureRoutes", () => {
  it("sums the gzip size of each page's first-load JS, counting a shared chunk once", () => {
    const routes = measureRoutes(nextDir);

    expect(Object.keys(routes).sort()).toEqual(["/", "/signin"]);
    expect(routes["/"]).toBe(sizes.main + sizes.layout + sizes.home);
    expect(routes["/signin"]).toBe(sizes.main + sizes.layout + sizes.signin);
  });

  it("throws when there is no build to measure", () => {
    expect(() => measureRoutes(path.join(root, "missing"))).toThrow(/pnpm build/);
  });
});

describe("checkBudgets", () => {
  it("passes at or under budget", () => {
    const result = checkBudgets({ "/": 100, "/signin": 50 }, { "/": 100, "/signin": 60 });
    expect(result.ok).toBe(true);
    expect(result.failures).toEqual([]);
  });

  it("fails a route over its budget, naming size and budget", () => {
    const result = checkBudgets({ "/": 101, "/signin": 50 }, { "/": 100, "/signin": 60 });
    expect(result.ok).toBe(false);
    expect(result.failures).toHaveLength(1);
    expect(result.failures[0]).toMatch(/\/ .*101.*100/);
  });

  it("fails a page route that has no budget", () => {
    const result = checkBudgets({ "/": 100, "/new": 10 }, { "/": 100 });
    expect(result.ok).toBe(false);
    expect(result.failures[0]).toMatch(/\/new.*no budget/);
  });
});

describe("budgetsFrom", () => {
  it("sets each budget at the current size plus 5%, rounded up", () => {
    expect(budgetsFrom({ "/": 1000, "/signin": 333 })).toEqual({ "/": 1050, "/signin": 350 });
  });
});

describe("the CLI", () => {
  function run(budgets: Record<string, number>) {
    const file = path.join(root, "bundle-budget.json");
    writeFileSync(file, JSON.stringify({ routes: budgets }));
    return spawnSync(process.execPath, [SCRIPT, "--next-dir", nextDir, "--budget", file], { encoding: "utf8" });
  }

  it("exits 0 and prints every route when within budget", () => {
    const res = run({ "/": 1_000_000, "/signin": 1_000_000 });
    expect(res.status).toBe(0);
    expect(res.stdout).toContain("/signin");
  });

  it("exits 1 naming the route over budget", () => {
    const res = run({ "/": 10, "/signin": 1_000_000 });
    expect(res.status).toBe(1);
    expect(res.stderr).toMatch(/\/ .*over budget/);
  });
});
