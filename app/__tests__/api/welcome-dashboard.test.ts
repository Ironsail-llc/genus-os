import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock config module
vi.mock("@/lib/config", () => ({
  HELM_AGENT_ID: "helm-user",
  OWNER_NAME: "there",
  AI_NAME: "Robothor",
  SESSION_KEY: "agent:main:webchat-user",
}));

const { mockDashboardCompletion } = vi.hoisted(() => ({
  mockDashboardCompletion: vi.fn(),
}));

vi.mock("@/lib/engine/server-client", () => ({
  getEngineClient: () => ({ dashboardCompletion: mockDashboardCompletion }),
}));

vi.mock("@/lib/dashboard/welcome-context", () => ({
  fetchWelcomeContext: vi.fn().mockResolvedValue({
    hour: 9,
    health: { status: "ok", services: [] },
    inbox: { openCount: 0, unreadCount: 0 },
  }),
}));

import { POST } from "@/app/api/dashboard/welcome/route";

describe("POST /api/dashboard/welcome", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("returns JSON with html and type on success", async () => {
    mockDashboardCompletion.mockResolvedValue(
      '<div class="glass">Welcome</div>',
    );

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.html).toContain("Welcome");
    expect(body.type).toBeTruthy();
    expect(mockDashboardCompletion).toHaveBeenCalledWith(
      "render",
      expect.stringContaining("read-only Genus OS dashboard"),
      expect.stringContaining("Real Data"),
    );
  });

  it("returns generic error JSON on Engine completion failure", async () => {
    mockDashboardCompletion.mockRejectedValue(
      new Error("provider response contained sk-sensitive-value"),
    );

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Dashboard service temporarily unavailable");
    expect(body.error).not.toContain("sk-sensitive-value");
  });

  it("returns error JSON if generated code fails validation", async () => {
    mockDashboardCompletion.mockResolvedValue('eval("alert(1)")');

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Generated dashboard failed quality check");
  });

  it("returns error JSON on unexpected error with sanitized message", async () => {
    mockDashboardCompletion.mockRejectedValue(new Error("Network timeout"));

    const res = await POST();
    expect(res.status).toBe(200);

    const text = await res.text();
    const body = JSON.parse(text.trim());
    expect(body.error).toBe("Dashboard service temporarily unavailable");
    expect(body.error).not.toContain("Network timeout");
  });
});
