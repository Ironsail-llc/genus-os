import { describe, expect, it } from "vitest";
import { OUTCOME_UNKNOWN, requestFailure } from "../terminal-outcome";

describe("request admission evidence", () => {
  it("accepts an explicit refusal before execution", async () => {
    expect(await requestFailure(Response.json({ error: "Not authorized", request_admitted: false }, { status: 403 })))
      .toEqual({ text: "Not authorized", rejected: true });
  });
  it.each([
    [409, { error: "Approval already received", request_admitted: true }],
    [400, { error: "Unknown origin" }],
    [502, { error: "Upstream disconnected" }],
    [403, { error: "Denied", request_admitted: "false" }],
    [200, { error: "Not an admission refusal", request_admitted: false }],
    [403, { request_admitted: false }],
  ])("does not infer rejection from status or ambiguous data (%s)", async (status, body) => {
    expect((await requestFailure(Response.json(body, { status }))).rejected).toBe(false);
  });
  it("keeps malformed error responses unresolved", async () => {
    expect(await requestFailure(new Response("unreadable", { status: 502 })))
      .toEqual({ text: OUTCOME_UNKNOWN, rejected: false });
  });
  it.each([
    ["an HTML page", new Response("<html><body>413 Request Entity Too Large</body></html>", { status: 413, headers: { "content-type": "text/html" } })],
    ["no body at all", new Response(null, { status: 413 })],
  ])("treats a 413 with %s as refused before it reached the dashboard, naming the limit", async (_label, response) => {
    const failure = await requestFailure(response);
    expect(failure.rejected).toBe(true);
    expect(failure.text).toMatch(/too large/);
    expect(failure.text).toContain("5 MB");
    expect(failure.text).not.toBe(OUTCOME_UNKNOWN);
  });
  it("keeps a JSON 413 without the admission flag unresolved", async () => {
    expect((await requestFailure(Response.json({ error: "big" }, { status: 413 }))).rejected).toBe(false);
  });
});
