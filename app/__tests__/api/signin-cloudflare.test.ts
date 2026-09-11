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
    // Pinned, not inherited. The operator's own shell exports the real
    // AUTH_URL, so without this every "AUTH_URL is unset" case is green in CI
    // and red on their box — and worse, silently asserts against a production
    // hostname. Each test that cares sets its own value.
    vi.stubEnv("AUTH_URL", "");
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

  // Next standalone (`server.js`) binds `hostname = process.env.HOSTNAME || '0.0.0.0'`,
  // so inside a route handler `request.url` is `https://0.0.0.0:3004/...` — the BIND
  // address, not the address the browser used. Every redirect built from it sent the
  // operator to "0.0.0.0 refused to connect" after a successful Cloudflare Access
  // sign-in (2026-09-11). The public origin is AUTH_URL, which Auth.js already
  // requires in production.
  describe("public origin", () => {
    it("builds redirects from AUTH_URL, never the bind address in request.url", async () => {
      vi.stubEnv("AUTH_URL", "https://app.example.com");
      const response = await GET(
        new NextRequest("http://0.0.0.0:3004/signin/cloudflare?callbackUrl=%2F"),
      );
      const location = response.headers.get("location")!;
      expect(location.startsWith("https://app.example.com/signin?error=")).toBe(true);
    });

    // With AUTH_URL unset the proxy headers are the only remaining witness to
    // the address the browser used. They are the SAME source Auth.js already
    // trusts here — app/src/lib/auth.ts sets `trustHost: true` — so this adds
    // no trust the deployment has not already granted its proxy.
    it("falls back to the forwarded headers when AUTH_URL is unset", async () => {
      vi.stubEnv("AUTH_URL", "");
      const response = await GET(
        new NextRequest("http://0.0.0.0:3004/signin/cloudflare?callbackUrl=%2F", {
          headers: { "x-forwarded-host": "app.example.com", "x-forwarded-proto": "https" },
        }),
      );
      expect(response.headers.get("location")).toBe(
        "https://app.example.com/signin?error=CloudflareAccessUnavailable",
      );
    });

    it("falls back to the request origin only when nothing else says otherwise", async () => {
      vi.stubEnv("AUTH_URL", "");
      const response = await GET(request("/signin/cloudflare", false));
      expect(response.headers.get("location")).toBe(
        "https://genus.example/signin?error=CloudflareAccessUnavailable",
      );
    });

    it("ignores a non-absolute AUTH_URL", async () => {
      vi.stubEnv("AUTH_URL", "/not-an-origin");
      const response = await GET(request("/signin/cloudflare", false));
      expect(response.headers.get("location")).toBe(
        "https://genus.example/signin?error=CloudflareAccessUnavailable",
      );
    });

    // `new URL("javascript:alert(1)")` PARSES — protocol "javascript:", origin
    // the string "null" — so a bare try/catch is not a guard. Only http(s) may
    // become an origin.
    it.each(["javascript:alert(1)", "mailto:ops@example.com", "file:///etc/passwd"])(
      "ignores a non-http AUTH_URL (%s)",
      async (bogus) => {
        vi.stubEnv("AUTH_URL", bogus);
        const response = await GET(request("/signin/cloudflare", false));
        expect(response.headers.get("location")).toBe(
          "https://genus.example/signin?error=CloudflareAccessUnavailable",
        );
      },
    );

    // Auth.js `signIn(..., { redirect: false })` returns an ABSOLUTE URL, built
    // from ITS base URL — which, with AUTH_URL unset, is derived the same way
    // everything else here was, i.e. from the bind address. Rebasing its
    // path onto the origin we already resolved makes this route's guarantee
    // unconditional, and means an absolute target can never leave the origin.
    it("rebases an absolute signIn target onto the public origin", async () => {
      vi.stubEnv("AUTH_URL", "https://app.example.com");
      signIn.mockResolvedValue("https://0.0.0.0:3004/fleet?tab=runs#top");
      const response = await GET(request("/signin/cloudflare?callbackUrl=%2Ffleet"));
      expect(response.headers.get("location")).toBe(
        "https://app.example.com/fleet?tab=runs#top",
      );
    });

    it("rebases a relative signIn target onto the public origin", async () => {
      vi.stubEnv("AUTH_URL", "https://app.example.com");
      signIn.mockResolvedValue("/fleet");
      const response = await GET(request("/signin/cloudflare?callbackUrl=%2Ffleet"));
      expect(response.headers.get("location")).toBe("https://app.example.com/fleet");
    });
  });

  it.each([
    "https://evil.example/phish",
    "//evil.example/phish",
    "/\\evil.example/phish", // WHATWG URL treats "/\" as "//" — protocol-relative
    "/\\/evil.example",
  ])(
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
