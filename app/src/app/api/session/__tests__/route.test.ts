/**
 * The canvas keeps its last view in a memory block, so a reload shows it at
 * once instead of an empty skeleton while a new one is generated.
 *
 * The bridge serves memory blocks under `/api/memory/blocks/<name>`. This route
 * asked for `/api/memory-blocks/<name>`, which the bridge has no route for: a
 * save answered 502 every time, and a restore read the bridge's 404 as "nothing
 * saved yet", so nothing ever said the canvas was not being kept.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-token" }),
}));
vi.mock("@/lib/services/registry", () => ({
  getServiceUrl: () => "http://bridge.test:9100",
}));

import { GET, POST } from "../route";

const BLOCK_URL = "http://bridge.test:9100/api/memory/blocks/helm_state";
const HTML = '<section class="genus-dashboard"><p>Alice has 3 open tickets</p></section>';

function calls(): Array<{ url: string; init: RequestInit | undefined }> {
  const mock = global.fetch as unknown as { mock: { calls: unknown[][] } };
  return mock.mock.calls.map(([url, init]) => ({
    url: String(url),
    init: init as RequestInit | undefined,
  }));
}

function save(html: string): Promise<Response> {
  return POST(
    new NextRequest("http://dashboard/api/session", {
      method: "POST",
      body: JSON.stringify({ html }),
    }),
  );
}

describe("/api/session — the canvas's saved view", () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("saves the view to the bridge's memory-block route", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ success: true }), { status: 200 }),
    );

    const response = await save(HTML);

    expect(response.status).toBe(200);
    const [call] = calls();
    expect(call.url).toBe(BLOCK_URL);
    expect(call.init?.method).toBe("PUT");
    const content = JSON.parse(JSON.parse(String(call.init?.body)).content);
    expect(content.html).toBe(HTML);
    expect(typeof content.savedAt).toBe("string");
  });

  it("restores the view from the same route", async () => {
    const savedAt = "2026-11-06T17:00:00.000Z";
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          block_name: "helm_state",
          content: JSON.stringify({ html: HTML, savedAt }),
        }),
        { status: 200 },
      ),
    );

    const response = await GET();

    expect(calls()[0].url).toBe(BLOCK_URL);
    expect(await response.json()).toEqual({ html: HTML, savedAt });
  });

  it("answers an empty view when nothing has been saved yet", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ error: "Block 'helm_state' not found" }), { status: 404 }),
    );

    expect(await (await GET()).json()).toEqual({ html: null });
  });

  it("says so when the bridge refuses a save", async () => {
    vi.mocked(global.fetch).mockResolvedValue(new Response("nope", { status: 500 }));

    expect((await save(HTML)).status).toBe(502);
  });
});
