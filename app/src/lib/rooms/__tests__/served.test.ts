/**
 * Whether this instance serves workspaces, asked server-side as the caller:
 * the rooms list answering 200 means yes; a 404, any other refusal, or no
 * answer means no, so nothing about workspaces is said to the agent.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/bridge-auth", () => ({
  bridgeAuthHeaders: async () => ({ Authorization: "Bearer test-token" }),
}));
vi.mock("@/lib/services/registry", () => ({
  getServiceUrl: (_name: string, path = "") => `http://bridge.test${path}`,
}));

import { roomsServed } from "../served";

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("roomsServed", () => {
  it("is served when the rooms list answers, asked with the caller's credentials", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ rooms: [] }), { status: 200 }));
    expect(await roomsServed()).toBe("served");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://bridge.test/api/rooms");
    expect((init as RequestInit).headers).toMatchObject({ Authorization: "Bearer test-token" });
  });

  it("is not served when the rooms list answers 404", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 404 }));
    expect(await roomsServed()).toBe("not_served");
  });

  it("is unknown when the bridge cannot be reached or times out", async () => {
    fetchMock.mockRejectedValue(new TypeError("refused"));
    expect(await roomsServed()).toBe("unknown");
    fetchMock.mockRejectedValue(new DOMException("timed out", "TimeoutError"));
    expect(await roomsServed()).toBe("unknown");
  });

  it("is unknown for any other refusal, which says nothing about workspaces", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 502 }));
    expect(await roomsServed()).toBe("unknown");
    fetchMock.mockResolvedValue(new Response("{}", { status: 401 }));
    expect(await roomsServed()).toBe("unknown");
  });
});
