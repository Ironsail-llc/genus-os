import { describe, expect, it } from "vitest";

import nextConfig, { SECURITY_HEADERS } from "../../next.config";

describe("dashboard response security headers", () => {
  it("denies framing and browser capabilities on every response", async () => {
    const configured = await nextConfig.headers?.();

    expect(configured).toEqual([
      { source: "/:path*", headers: [...SECURITY_HEADERS] },
    ]);
    expect(SECURITY_HEADERS).toEqual(
      expect.arrayContaining([
        { key: "X-Frame-Options", value: "DENY" },
        { key: "X-Content-Type-Options", value: "nosniff" },
        expect.objectContaining({
          key: "Content-Security-Policy",
          value: expect.stringContaining("frame-ancestors 'none'"),
        }),
      ]),
    );
  });
});
