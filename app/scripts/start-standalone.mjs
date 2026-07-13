#!/usr/bin/env node

import { cpSync, existsSync, mkdirSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";

const root = process.cwd();
const standalone = path.join(root, ".next", "standalone");
const server = path.join(standalone, "server.js");
if (!existsSync(server)) {
  throw new Error(`Standalone server is missing: ${server}. Run pnpm build first.`);
}

const staticSource = path.join(root, ".next", "static");
const staticTarget = path.join(standalone, ".next", "static");
mkdirSync(path.dirname(staticTarget), { recursive: true });
cpSync(staticSource, staticTarget, { recursive: true, force: true });

const publicSource = path.join(root, "public");
if (existsSync(publicSource)) {
  cpSync(publicSource, path.join(standalone, "public"), {
    recursive: true,
    force: true,
  });
}

const child = spawn(process.execPath, [server], {
  cwd: standalone,
  env: process.env,
  stdio: "inherit",
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("exit", (code, signal) => {
  process.exit(code ?? (signal ? 1 : 0));
});
