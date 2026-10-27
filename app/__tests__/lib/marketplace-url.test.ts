import { describe, expect, it } from "vitest";

import { DEFAULT_MARKETPLACE_URL, marketplaceUrl } from "@/lib/marketplace-url";

describe("marketplaceUrl", () => {
  it("defaults to the live marketplace, not the parked domain", () => {
    expect(DEFAULT_MARKETPLACE_URL).toBe("https://programmaticresources.io");
    expect(marketplaceUrl(undefined)).toBe("https://programmaticresources.io");
    expect(marketplaceUrl("  ")).toBe("https://programmaticresources.io");
  });

  it("uses the configured host without a trailing slash", () => {
    expect(marketplaceUrl("https://hub.example/")).toBe("https://hub.example");
    expect(marketplaceUrl(" https://hub.example// ")).toBe("https://hub.example");
  });
});
