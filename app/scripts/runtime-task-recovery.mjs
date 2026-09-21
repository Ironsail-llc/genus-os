/** Native task creation, lost browser response, and original-request receipt recovery. */
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
  let page = await browser.newPage();
  const freshContext = process.env.TASK_RECOVERY_FRESH_CONTEXT === "1";
  let sends = 0, reads = 0, original;
  let commitObserved = false;
  const routing = async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/chat/send") {
      sends++;
      original = route.request().postDataJSON().request_id;
      const response = await route.fetch();
      const body = await response.text();
      expect(response.status(), body).toBe(200);
      expect(body).toContain("Task creation recorded.");
      await route.abort("connectionreset");
      commitObserved = true;
      return;
    }
    if (path === "/api/chat/outcome") {
      reads++;
      expect(new URL(route.request().url()).searchParams.get("request_id")).toBe(original);
    }
    if (path.startsWith("/api/chat/")) return route.continue();
    if (path === "/api/dashboard/welcome") return route.fulfill({ json: { html: "<div>Local acceptance test</div>", type: "html" } });
    if (path === "/api/dashboard/generate") return route.fulfill({ status: 204 });
    if (path === "/api/events/stream") return route.fulfill({ contentType: "text/event-stream", body: "event: ping\ndata: {}\n\n" });
    return route.fulfill({ json: { agents: [], messages: [], status: "healthy" } });
  };
  await page.route("**/api/**", routing);
  await page.goto(base, { waitUntil: "networkidle" });
  await page.getByTestId("chat-input").fill("Create the synthetic browser task");
  await page.getByTestId("send-button").click();
  if (freshContext) {
    await expect.poll(() => commitObserved, { timeout: 30000 }).toBe(true);
    await page.context().close();
    const readsBeforeFresh = reads;
    const restored = await fetch(`${process.env.ROBOTHOR_ENGINE_URL}/fixture/reload-chat`, { method: "POST" });
    expect(restored.status).toBe(200);
    expect(await restored.json()).toEqual({ restored: true });
    page = await browser.newPage();
    await page.route("**/api/**", routing);
    await page.goto(base, { waitUntil: "networkidle" });
    expect(await page.evaluate(() => Object.keys(sessionStorage).filter(key => key.startsWith("helm.chat.pending.v1.")))).toEqual([]);
    const saved = page.getByText("Task creation recorded.", { exact: true });
    await expect(saved).toBeVisible({ timeout: 30000 });
    expect(sends).toBe(1);
    await delay(1500);
    expect(reads).toBe(readsBeforeFresh);
    expect(sends).toBe(1);
    console.log("TASK_BROWSER " + JSON.stringify({ sends, reads, fresh_context: true, restored_from_database: true, text: await saved.innerText(), request_id: original }));
  } else {
  const receipt = page.getByText(/The task was created\. Robothor checked the stored task/);
  await expect(receipt).toBeVisible({ timeout: 30000 });
  expect(await receipt.innerText()).toContain("without creating another task");
  expect(sends).toBe(1);
  expect(reads).toBeGreaterThan(0);
  const readsAtCompletion = reads;
  await delay(1500);
  expect(reads).toBe(readsAtCompletion);
  expect(sends).toBe(1);
  console.log("TASK_BROWSER " + JSON.stringify({ sends, reads, text: await receipt.innerText(), request_id: original }));
  }
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once("exit", resolve); });
}
