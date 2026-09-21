/** Actual chat + Next proxy recovery; only unrelated dashboard APIs are fixtures. */
import { chromium, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

const base = `http://127.0.0.1:${process.env.PORT}`;
const server = spawn(process.execPath, ["scripts/start-standalone.mjs"], { stdio: "inherit", env: process.env });
let browser;
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error("Isolated Next server exited");
    try { if ((await fetch(`${base}/api/live`)).ok) { ready = true; break; } } catch {}
    await delay(200);
  }
  if (!ready) throw new Error("Isolated Next server did not become ready");
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  let starts = 0, reads = 0, approvals = 0, original;
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/chat/plan/start") {
      starts++;
      original = route.request().postDataJSON().request_id;
      // Forward the real request all the way through Next and the native runner,
      // wait for its saved draft, then lose its response at the browser boundary.
      const response = await route.fetch();
      if (response.status() !== 200 || !(await response.text()).includes("event: plan")) {
        throw new Error(`Native plan did not finish: ${response.status()} ${await response.text()}`);
      }
      return route.abort("connectionreset");
    }
    if (path === "/api/chat/outcome") {
      reads++;
      expect(new URL(route.request().url()).searchParams.get("request_id")).toBe(original);
    }
    if (path === "/api/chat/plan/approve") approvals++;
    if (path.startsWith("/api/chat/")) return route.continue();
    if (path === "/api/dashboard/welcome") return route.fulfill({ json: { html: "<div>Local acceptance test</div>", type: "html" } });
    if (path === "/api/dashboard/generate") return route.fulfill({ status: 204 });
    if (path === "/api/events/stream") return route.fulfill({ contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    return route.fulfill({ json: { agents: [], messages: [], status: "healthy" } });
  });
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByTestId("plan-toggle").click();
  await page.getByTestId("chat-input").fill("Prepare the synthetic task review plan");
  await page.getByTestId("send-button").click();
  await expect(page.getByTestId("plan-approve")).toBeVisible({ timeout: 30000 });
  await expect(page.getByText("Inspect the synthetic task record", { exact: true }).first()).toBeVisible();
  expect(starts).toBe(1);
  expect(reads).toBeGreaterThan(0);
  expect(approvals).toBe(0);
  await expect(page.getByText(/\[PLAN_READY\]/)).toHaveCount(0);
  console.log("PLAN_BROWSER " + JSON.stringify({ starts, reads, approvals, request_id: original, restored: true }));
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once("exit", resolve); });
}
