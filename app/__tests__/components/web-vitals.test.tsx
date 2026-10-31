import { describe, it, expect, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";

type Metric = { name: string; value: number; rating: string; id: string; navigationType: string };
let reporter: ((metric: Metric) => void) | null = null;
vi.mock("next/web-vitals", () => ({
  useReportWebVitals: (cb: (metric: Metric) => void) => {
    reporter = cb;
  },
}));

import { WebVitals } from "@/components/web-vitals";

describe("WebVitals", () => {
  const sendBeacon = vi.fn(() => true);

  beforeEach(() => {
    reporter = null;
    sendBeacon.mockClear();
    Object.defineProperty(navigator, "sendBeacon", { value: sendBeacon, configurable: true });
    window.history.replaceState(null, "", "/?v=settings&s=flags&token=secret");
  });

  it("beacons each metric to /api/vitals with the path and view, no query string", async () => {
    render(<WebVitals />);
    expect(reporter).not.toBeNull();

    reporter!({ name: "LCP", value: 812.4, rating: "good", id: "v1-9", navigationType: "navigate" });

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, blob] = sendBeacon.mock.calls[0] as unknown as [string, Blob];
    expect(url).toBe("/api/vitals");
    const body = JSON.parse(await blob.text());
    expect(body).toEqual({
      name: "LCP",
      value: 812.4,
      rating: "good",
      id: "v1-9",
      navigation_type: "navigate",
      path: "/",
      view: "settings",
    });
  });
});
