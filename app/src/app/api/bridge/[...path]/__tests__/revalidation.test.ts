// @vitest-environment node
/**
 * Conditional requests through the bridge proxy.
 *
 * The settings schema is large and changes only when the bridge restarts, so
 * the bridge tags it (`ETag`) and answers a matching `If-None-Match` with a
 * bodiless 304. That only saves anything if this proxy carries the tag both
 * ways and passes the 304 through as a 304 — rebuilding it as a JSON reply,
 * or as a text reply with an empty body, either fails outright or makes the
 * browser download the whole schema every time.
 */
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer verified-session" }),
}));

import { GET } from "../route";

const TAG = '"sha256-0123456789abcdef"';
const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

function schemaRequest(headers: Record<string, string> = {}) {
  return new NextRequest("http://dashboard.example.invalid/api/bridge/api/settings/schema", {
    headers,
  });
}

afterEach(() => vi.restoreAllMocks());

describe("bridge proxy revalidation", () => {
  it("forwards the browser's If-None-Match to the bridge", async () => {
    const fetcher = vi
      .spyOn(global, "fetch")
      .mockResolvedValue(Response.json({ groups: [] }, { headers: { ETag: TAG } }));
    await GET(schemaRequest({ "If-None-Match": TAG }), context(["api", "settings", "schema"]));
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>)["If-None-Match"]).toBe(TAG);
  });

  it("does not invent an If-None-Match the browser did not send", async () => {
    const fetcher = vi.spyOn(global, "fetch").mockResolvedValue(Response.json({ groups: [] }));
    await GET(schemaRequest(), context(["api", "settings", "schema"]));
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect(Object.keys(init.headers as Record<string, string>)).not.toContain("If-None-Match");
  });

  it("carries the bridge's ETag and Cache-Control back on a JSON reply", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      Response.json(
        { groups: [] },
        { headers: { ETag: TAG, "Cache-Control": "private, max-age=300" } }
      )
    );
    const res = await GET(schemaRequest(), context(["api", "settings", "schema"]));
    expect(res.status).toBe(200);
    expect(res.headers.get("etag")).toBe(TAG);
    expect(res.headers.get("cache-control")).toBe("private, max-age=300");
    expect(await res.json()).toEqual({ groups: [] });
  });

  it("passes a 304 through as a bodiless 304 with the tag", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(null, {
        status: 304,
        headers: { ETag: TAG, "Cache-Control": "private, max-age=300" },
      })
    );
    const res = await GET(schemaRequest({ "If-None-Match": TAG }), context(["api", "settings", "schema"]));
    expect(res.status).toBe(304);
    expect(res.headers.get("etag")).toBe(TAG);
    expect(res.headers.get("cache-control")).toBe("private, max-age=300");
    expect(await res.text()).toBe("");
  });

  it("keeps no-store on a JSON reply the bridge did not tag", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(Response.json({ values: {} }));
    const res = await GET(schemaRequest(), context(["api", "settings"]));
    expect(res.headers.get("cache-control")).toBe("no-store");
    expect(res.headers.get("etag")).toBeNull();
  });
});
