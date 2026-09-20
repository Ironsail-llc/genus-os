import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const managed = process.env.RUNTIME_UAT_MANAGED === "1";
const apiPort = process.env.RUNTIME_UAT_API_PORT || "5324";
const uiPort = process.env.RUNTIME_UAT_UI_PORT || "5323";

export default defineConfig({
  testDir: ".", testMatch: "acceptance.spec.ts", workers: 1, retries: 0,
  outputDir: "../../test-results/runtime-uat",
  use: {
    baseURL: process.env.RUNTIME_UAT_BASE_URL || `http://127.0.0.1:${uiPort}`,
    browserName: "chromium", headless: true,
    viewport: { width: 1440, height: 1000 }, screenshot: "only-on-failure",
  },
  webServer: managed ? [
    {
      command: `${process.env.RUNTIME_UAT_PYTHON || ".venv/bin/python"} -m bench.runtime.uat_server --port ${apiPort} --ui-port ${uiPort}`,
      cwd: resolve(__dirname, "../../.."),
      url: `http://127.0.0.1:${apiPort}/uat/status`,
      reuseExistingServer: false,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
    },
    {
      command: "pnpm exec vite --config uat/runtime/vite.config.ts",
      cwd: resolve(__dirname, "../.."),
      env: { RUNTIME_UAT_API_PORT: apiPort, RUNTIME_UAT_UI_PORT: uiPort },
      url: `http://127.0.0.1:${uiPort}`,
      reuseExistingServer: false,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
    },
  ] : undefined,
});
