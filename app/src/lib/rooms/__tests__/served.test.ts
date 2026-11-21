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
  it("is true when the rooms list answers, asked with the caller's credentials", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ rooms: [] }), { status: 200 }));
    expect(await roomsServed()).toBe(true);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://bridge.test/api/rooms");
    expect((init as RequestInit).headers).toMatchObject({ Authorization: "Bearer test-token" });
  });

  it("is false when the rooms list is not served (404)", async () => {
    fetchMock.mockResolvedValue(new Response("{}", { status: 404 }));
    expect(await roomsServed()).toBe(false);
  });

  it("is false when the bridge cannot be reached", async () => {
    fetchMock.mockRejectedValue(new TypeError("refused"));
    expect(await roomsServed()).toBe(false);
  });
});
