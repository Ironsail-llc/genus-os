import { defineConfig } from "@playwright/test";

const port = process.env.PLAYWRIGHT_PORT ?? "3004";
const baseURL = `http://localhost:${port}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 120000,
  retries: 0,
  webServer: {
    command: "node scripts/start-standalone.mjs",
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120000,
    env: {
      AUTH_SECRET:
        process.env.AUTH_SECRET ??
        "playwright-only-secret-that-is-never-used-outside-the-test-server",
      AUTH_TRUST_HOST: "true",
      AUTH_OIDC_ISSUER: "https://idp.playwright.invalid",
      AUTH_OIDC_CLIENT_ID: "playwright-client",
      AUTH_OIDC_CLIENT_SECRET: "playwright-client-secret",
      GENUS_BRIDGE_SSO_SECRET: "playwright-bridge-sso-secret",
      GENUS_ENVIRONMENT: "test",
      GENUS_INSECURE_DEV_MODE: "true",
      PORT: port,
      HOSTNAME: "127.0.0.1",
    },
  },
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
