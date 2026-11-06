/**
 * The canvas keeps its last view in a memory block, so a reload shows it at
 * once instead of an empty skeleton while a new one is generated.
 *
 * The bridge serves memory blocks under `/api/memory/blocks/<name>`. This route
 * asked for `/api/memory-blocks/<name>`, which the bridge has no route for: a
 * save answered 502 every time, and a restore read the bridge's 404 as "nothing
 * saved yet", so nothing ever said the canvas was not being kept.
 */
import { createHash } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const session = vi.fn();
vi.mock("@/lib/auth", () => ({ auth: () => session() }));
vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-token" }),
}));
vi.mock("@/lib/services/registry", () => ({
  getServiceUrl: () => "http://bridge.test:9100",
}));

import { GET, POST } from "../route";

// The shape a real session has: the session callback copies the person's
// name, email and role, and no id. Keying the block on `user.id` refused
// every signed-in person.
const ALICE = { user: { name: "Alice Example", email: "alice@example.com", role: "admin" } };
const BOB = { user: { name: "Bob Example", email: "bob@example.com", role: "admin" } };

function blockUrl(identity: string): string {
  const key = createHash("sha256").update(identity).digest("hex").slice(0, 32);
  return `http://bridge.test:9100/api/memory/blocks/helm_state.${key}`;
}
const BLOCK_URL = blockUrl("alice@example.com");
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
    session.mockResolvedValue(ALICE);
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

  it("keeps each person's view in a block of their own", async () => {
    // A canvas is drawn from one person's conversation. Blocks are keyed by
    // tenant, so one shared name would hand it to everyone in the tenant.
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ success: true }), { status: 200 }),
    );
    session.mockResolvedValue(BOB);

    await save(HTML);
    await GET();

    const urls = calls().map((call) => call.url);
    expect(urls).toEqual([blockUrl("bob@example.com"), blockUrl("bob@example.com")]);
    expect(urls[0]).not.toBe(BLOCK_URL);
  });

  it("never puts the person's address in the block name", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ success: true }), { status: 200 }),
    );

    await save(HTML);

    const { pathname } = new URL(calls()[0].url);
    expect(pathname).toMatch(/^\/api\/memory\/blocks\/helm_state\.[0-9a-f]{32}$/);
    expect(pathname).not.toContain("alice");
  });

  it("finds the same view however the address is capitalised", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ success: true }), { status: 200 }),
    );
    session.mockResolvedValue({ user: { email: " Alice@Example.COM ", role: "owner" } });

    await save(HTML);

    expect(calls()[0].url).toBe(BLOCK_URL);
  });

  it("uses a user id instead when a session carries one, exactly as given", async () => {
    // An id can be case-sensitive where an address is not: `Bob` and `bob`
    // are two people.
    vi.mocked(global.fetch).mockResolvedValue(
      new Response(JSON.stringify({ success: true }), { status: 200 }),
    );
    session.mockResolvedValue({ user: { id: "User-42", email: "alice@example.com", role: "admin" } });

    await save(HTML);

    expect(calls()[0].url).toBe(blockUrl("User-42"));
    expect(calls()[0].url).not.toBe(blockUrl("user-42"));
  });

  it.each(["member", "user", "viewer", "auditor", undefined])(
    "does not ask the bridge on behalf of a role it would refuse (%s)",
    async (role) => {
      // The bridge keeps memory blocks for owners and admins. Asking for
      // anyone else is a refusal and an auth.denied audit event on every
      // canvas load, for a view that was never going to be kept.
      session.mockResolvedValue({ user: { email: "bob@example.com", role } });

      const restored = await GET();
      const saved = await save(HTML);

      expect(restored.status).toBe(200);
      expect(await restored.json()).toEqual({ html: null });
      expect(saved.status).toBe(200);
      expect(await saved.json()).toEqual({ success: false, kept: false });
      expect(calls()).toHaveLength(0);
    },
  );

  it.each([null, {}, { user: {} }, { user: { name: "Alice Example" } }])(
    "neither saves nor restores for a session that identifies nobody (%j)",
    async (nobody) => {
      session.mockResolvedValue(nobody);

      expect((await save(HTML)).status).toBe(401);
      expect((await GET()).status).toBe(401);
      expect(calls()).toHaveLength(0);
    },
  );

  it("neither saves nor restores for a request with no signed-in person", async () => {
    session.mockResolvedValue(null);

    expect((await save(HTML)).status).toBe(401);
    expect((await GET()).status).toBe(401);
    expect(calls()).toHaveLength(0);
  });

  it("says so when the bridge refuses a save", async () => {
    vi.mocked(global.fetch).mockResolvedValue(new Response("nope", { status: 500 }));

    expect((await save(HTML)).status).toBe(502);
  });
});
