/**
 * A read is not an action. Every Helm page makes several reads through
 * `/api/actions/execute` as it mounts; on the box a five-page tour produced
 * eight 429s and empty task and agent lists. The limiter is for mutations.
 */
import { describe, expect, it } from "vitest";

import {
  ACTION_RATE_LIMIT,
  ACTION_RATE_WINDOW_MS,
  ActionRateLimiter,
  isMutation,
} from "@/lib/action-rate-limit";

describe("isMutation", () => {
  it("counts everything but GET as an action", () => {
    expect(isMutation("GET")).toBe(false);
    expect(isMutation("get")).toBe(false);
    for (const method of ["POST", "PATCH", "DELETE", "PUT"]) {
      expect(isMutation(method)).toBe(true);
    }
  });
});

describe("ActionRateLimiter", () => {
  function clock(start = 1_000_000) {
    let t = start;
    return { now: () => t, advance: (ms: number) => (t += ms) };
  }

  it("allows the budget and refuses the next one in the same window", () => {
    const c = clock();
    const limiter = new ActionRateLimiter(ACTION_RATE_LIMIT, ACTION_RATE_WINDOW_MS, c.now);

    for (let i = 0; i < ACTION_RATE_LIMIT; i++) {
      expect(limiter.allow("203.0.113.7")).toBe(true);
    }
    expect(limiter.allow("203.0.113.7")).toBe(false);
    expect(limiter.retryAfterSeconds("203.0.113.7")).toBe(60);
  });

  it("starts a fresh window once the old one has passed", () => {
    const c = clock();
    const limiter = new ActionRateLimiter(2, 1_000, c.now);

    expect(limiter.allow("k")).toBe(true);
    expect(limiter.allow("k")).toBe(true);
    expect(limiter.allow("k")).toBe(false);
    c.advance(1_001);
    expect(limiter.allow("k")).toBe(true);
    expect(limiter.retryAfterSeconds("k")).toBe(1);
  });

  it("keeps one budget per key", () => {
    const limiter = new ActionRateLimiter(1, 1_000, clock().now);

    expect(limiter.allow("a")).toBe(true);
    expect(limiter.allow("a")).toBe(false);
    expect(limiter.allow("b")).toBe(true);
  });

  it("forgets expired keys on its periodic cleanup", () => {
    const c = clock();
    const limiter = new ActionRateLimiter(1, 1_000, c.now);
    limiter.allow("old");
    c.advance(5 * 60_000 + 1);

    // The cleanup runs on the next call; the expired key is gone, so it is
    // not consulted and a fresh window is allowed for it afterwards.
    expect(limiter.allow("fresh")).toBe(true);
    expect(limiter.allow("old")).toBe(true);
  });
});
