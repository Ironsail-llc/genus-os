import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 120000,
  retries: 0,
  use: {
    baseURL: "http://localhost:3004",
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
