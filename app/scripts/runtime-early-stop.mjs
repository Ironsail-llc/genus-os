/** Browser-originated approval/stop requests through the actual Next proxies. */
import { expect } from "@playwright/test";

export async function earlyStop(page, base) {
  const saved = await (await page.request.get(`${base}/api/chat/plan/status`)).json();
  const requestId = crypto.randomUUID();
  await page.evaluate(({ planId, requestId }) => {
    window.pendingTestApproval = fetch("/api/chat/plan/approve", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: planId, request_id: requestId }),
    }).then(async response => ({ status: response.status, body: await response.text() }));
  }, { planId: saved.plan.plan_id, requestId });
  const outcome = async () => (await page.request.get(`${base}/api/chat/outcome?request_id=${requestId}`)).json();
  await expect.poll(async () => (await outcome()).state).toBe("accepted");
  const stopped = await page.request.post(`${base}/api/chat/abort`, { data: { request_id: requestId } });
  expect(stopped.status()).toBe(200);
  expect((await stopped.json()).durable_stopped).toBe(true);
  const pending = await outcome();
  expect(pending.state).toBe("stopping");
  expect(pending.terminal).toBe(false);
  expect(pending.stop_requested).toBe(true);
  const approval = await page.evaluate(() => window.pendingTestApproval);
  expect(approval.status).toBe(200);
  await expect.poll(async () => (await outcome()).state).toBe("cancelled");
  const final = await outcome();
  expect(final.terminal).toBe(true);
  expect(final.text).toContain("Stopped as requested.");
  return { accepted: true, durable_stop: true, final_state: final.state };
}
