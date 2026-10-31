import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";
import { MAX_SEND_BODY_BYTES } from "../../src/lib/chat/attachments";

/**
 * `/api/chat/send` runs behind the proxy (`src/proxy.ts`), and Next buffers a
 * proxied body only up to `experimental.proxyClientMaxBodySize` (10 MB by
 * default), forwarding the rest truncated. A turn with three files at the limit
 * is larger than that, so the proxy's ceiling is the route's own, by
 * construction rather than by two numbers kept in step.
 */
describe("the proxy lets a full chat turn through", () => {
  it("sets the proxy body limit to the chat send ceiling", () => {
    expect(nextConfig.experimental?.proxyClientMaxBodySize).toBe(MAX_SEND_BODY_BYTES);
  });

  it("is above Next's 10 MB default, which a full turn exceeds", () => {
    expect(MAX_SEND_BODY_BYTES).toBeGreaterThan(10 * 1024 * 1024);
  });
});
