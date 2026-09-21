// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
const identity = vi.hoisted(() => vi.fn());
vi.mock("@/lib/bridge-auth", () => ({ bridgeAuthHeaders: identity }));
import { GET, POST } from "../route";

afterEach(() => vi.restoreAllMocks());
const id = "00000000-0000-0000-0000-000000000001";
const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

describe("deployment proxy", () => {
  it("forwards only the verified human session credential and preserves conflicts", async () => {
    identity.mockResolvedValue({ Authorization: "Bearer verified-session" });
    const fetcher = vi.spyOn(global, "fetch").mockResolvedValue(Response.json({ detail: "Settings changed" }, { status: 409 }));
    const request = new Request("http://dashboard/api/sales/deployment/prepare", {
      method: "POST", headers: { Authorization: "Bearer attacker", "Content-Type": "application/json" }, body: JSON.stringify({ expected_revision: 1 }),
    });
    const result = await POST(request, context(["prepare"]));
    expect(result.status).toBe(409);
    expect(await result.json()).toEqual({ detail: "Settings changed" });
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining("/api/admin/sales-deployment/prepare"), expect.objectContaining({
      headers: { Authorization: "Bearer verified-session", "Content-Type": "application/json" }, redirect: "error",
    }));
  });
  it("refuses missing sessions without contacting the engine", async () => {
    identity.mockResolvedValue({});
    const fetcher = vi.spyOn(global, "fetch");
    expect((await GET(new Request("http://dashboard/api/sales/deployment"), context([]))).status).toBe(401);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("allows only the deployment operations and typed identifiers", async () => {
    identity.mockResolvedValue({ Authorization: "Bearer verified-session" });
    const fetcher = vi.spyOn(global, "fetch").mockResolvedValue(Response.json({}));
    for (const path of [["..", "secrets"], ["transitions", id, "enable"], ["releases", "../../etc/passwd"]]) {
      expect((await GET(new Request("http://dashboard/api/sales/deployment"), context(path))).status).toBe(404);
    }
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("does not retry an uncertain mutation", async () => {
    identity.mockResolvedValue({ Authorization: "Bearer verified-session" });
    const fetcher = vi.spyOn(global, "fetch").mockRejectedValue(new Error("timeout"));
    const response = await POST(new Request("http://dashboard/api/sales/deployment", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }), context(["transitions", id, "commit"]));
    expect(response.status).toBe(502);
    expect((await response.json()).error).toMatch(/refresh/i);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
