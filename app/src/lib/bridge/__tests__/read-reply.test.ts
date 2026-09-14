import { describe, it, expect } from "vitest";

import { readBridgeReply } from "../read-reply";

/**
 * This file carries the merged coverage of the three byte-near readers that
 * used to live in `lib/agents/manifests.ts` (`readError`),
 * `views/settings/providers-page.tsx` (`readError`) and
 * `lib/inbox/pending.ts` (`readAnswerMessage`). Each former reader's behaviour
 * is still asserted here, once.
 */

function res(body: unknown, status = 400): Response {
  return { status, json: () => Promise.resolve(body) } as unknown as Response;
}

describe("readBridgeReply", () => {
  it("prefers `message`, which is what a refusal from the approvals router carries", () => {
    // `_refuse` in crm/bridge/routers/approvals.py answers
    // `{"settled": false, "message": …}`. The two older readers knew only
    // `detail`, so they printed "HTTP 400" over the server's own sentence.
    return expect(readBridgeReply(res({ settled: false, message: "answer is required" }))).resolves.toBe(
      "answer is required"
    );
  });

  it("prefers `message` over a `detail` sent alongside it", async () => {
    expect(
      await readBridgeReply(res({ message: "already decided", detail: "conflict" }))
    ).toBe("already decided");
  });

  it("reads a FastAPI string detail — the agents and providers pages' usual refusal", async () => {
    expect(await readBridgeReply(res({ detail: "the engine did not answer" }, 502))).toBe(
      "the engine did not answer"
    );
  });

  it("reads a FastAPI validation detail list, joined", async () => {
    expect(
      await readBridgeReply(
        res({ detail: [{ msg: "field required" }, { msg: "cron is not a schedule" }] }, 422)
      )
    ).toBe("field required; cron is not a schedule");
  });

  it("ignores a detail list that carries no messages", async () => {
    expect(await readBridgeReply(res({ detail: [{ loc: ["body"] }] }, 422))).toContain("422");
  });

  it("falls back to `error`, which the app's own proxy uses", async () => {
    expect(await readBridgeReply(res({ error: "Bridge unreachable" }, 502))).toBe(
      "Bridge unreachable"
    );
  });

  it("ignores blank strings rather than rendering an empty error", async () => {
    expect(await readBridgeReply(res({ message: "   ", detail: "", error: "" }, 409))).toContain(
      "409"
    );
  });

  it("names the status when the body says nothing usable", async () => {
    expect(await readBridgeReply(res({}, 503))).toContain("503");
    expect(await readBridgeReply(res(null, 418))).toContain("418");
  });

  it("names the status when the body is not JSON at all", async () => {
    const message = await readBridgeReply({
      status: 500,
      json: () => Promise.reject(new Error("not json")),
    } as unknown as Response);
    expect(message).toContain("500");
  });
});
