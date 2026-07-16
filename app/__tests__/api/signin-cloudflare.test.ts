import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const signIn = vi.fn();
vi.mock("@/lib/auth", () => ({ signIn }));

const { GET } = await import("@/app/signin/cloudflare/route");

const CF_HEADER = "cf-access-jwt-assertion";

function request(path: string, withHeader = true) {
  return new NextRequest(`https://genus.example${path}`, {
    headers: withHeader ? { [CF_HEADER]: "edge-injected-jwt" } : {},
  });
}

describe("GET /signin/cloudflare", () => {
  beforeEach(() => {
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", "https://team.example.com");
    vi.stubEnv("CF_ACCESS_AUD", "aud-tag-1");
    signIn.mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("establishes the session and redirects to the signIn result", async () => {
    signIn.mockResolvedValue("https://genus.example/fleet");
    const response = await GET(request("/signin/cloudflare?callbackUrl=%2Ffleet"));
    expect(signIn).toHaveBeenCalledWith("cloudflare-access", {
      redirectTo: "/fleet",
      redirect: false,
    });
    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("https://genus.example/fleet");
  });

  it("redirects to the signin error page when the exchange fails", async () => {
    signIn.mockRejectedValue(new Error("CallbackRouteError"));
    const response = await GET(request("/signin/cloudflare"));
    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/signin");
    expect(location.searchParams.get("error")).toBe("CloudflareAccessFailed");
  });

  it("reports unavailable when the assertion header is missing", async () => {
    const response = await GET(request("/signin/cloudflare", false));
    const location = new URL(response.headers.get("location")!);
    expect(location.searchParams.get("error")).toBe("CloudflareAccessUnavailable");
    expect(signIn).not.toHaveBeenCalled();
  });

  it("reports unavailable when the env gate is off", async () => {
    vi.stubEnv("CF_ACCESS_TEAM_DOMAIN", "");
    const response = await GET(request("/signin/cloudflare"));
    const location = new URL(response.headers.get("location")!);
    expect(location.searchParams.get("error")).toBe("CloudflareAccessUnavailable");
    expect(signIn).not.toHaveBeenCalled();
  });

  it.each(["https://evil.example/phish", "//evil.example/phish"])(
    "refuses an absolute callbackUrl (%s) — open redirect guard",
    async (target) => {
      signIn.mockResolvedValue("https://genus.example/");
      await GET(request(`/signin/cloudflare?callbackUrl=${encodeURIComponent(target)}`));
      expect(signIn).toHaveBeenCalledWith("cloudflare-access", {
        redirectTo: "/",
        redirect: false,
      });
    },
  );
});
