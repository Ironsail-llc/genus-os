import { defineConfig } from "@playwright/test";

/**
 * End-to-end specs against the REAL workspaces stack: engine, bridge and
 * dashboard processes over Postgres and Redis, started by
 * `scripts/dev/rooms-stack.sh up` (only the model and the data source are
 * stand-ins). Nothing here is intercepted.
 *
 * Separate from `playwright.config.ts` (whose specs mock every backend and run
 * in CI's e2e job): this config starts no server, and its specs skip when
 * `ROOMS_STACK_URL` is unset, so they never run by accident.
 *
 *   scripts/dev/rooms-stack.sh up
 *   eval "$(scripts/dev/rooms-stack.sh env | sed 's/^/export /')"
 *   pnpm exec playwright test -c playwright.stack.config.ts
 */
export default defineConfig({
  testDir: "./e2e-stack",
  timeout: 120000,
  retries: 0,
  workers: 1,
  fullyParallel: false,
  use: {
    baseURL: process.env.ROOMS_STACK_URL ?? "http://127.0.0.1:3071",
    headless: true,
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
