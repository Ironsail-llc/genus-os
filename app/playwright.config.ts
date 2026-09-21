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
      /*
        A dead port on purpose.

        Every e2e spec intercepts the bridge in the browser, but interception
        is not total — a download started by `<a download>` is not routed by
        Playwright at all, and the request goes to this Next server, which
        resolves the bridge through `lib/services/registry.ts` and falls back
        to `http://localhost:9100` when no service manifest is found. That is
        the LIVE bridge on a developer box, and an e2e run was putting a real
        request on it with the Helm's own dev credentials attached.

        Pinning `BRIDGE_URL` here means the worst an unintercepted request can
        do is fail to connect. Nothing in the suite may depend on a reply from
        it; if a spec needs bridge data, it mocks it.
      */
      BRIDGE_URL: "http://127.0.0.1:59999",
      ROBOTHOR_ENGINE_URL: "http://127.0.0.1:59998",
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
